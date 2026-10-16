from __future__ import annotations

import csv
from pathlib import Path

from modules.docking.scripts.common import DockingError, ensure_dir, write_json, write_rows


DOCKING_LIGAND_CONTRACT_VERSION = "docking_ligands.v2"
SELECTED_STATUS = "selected"
SELECTED_EXCEPTION_STATUS = "selected_exception"
NON_SELECTED_STATUSES = {"rejected", "excluded", "skipped", "failed", "not_selected"}

LIGAND_INPUT_FIELDS = [
    "ligand_id",
    "compound_name",
    "pubchem_cid",
    "canonical_smiles",
    "isomeric_smiles",
    "inchikey",
]

SELECTED_LIGAND_FIELDS = [
    *LIGAND_INPUT_FIELDS,
    "original_rank",
    "original_score",
    "source_file",
    "contract_version",
    "selection_status",
    "source_contract_mode",
    "selected_for_docking",
    "selection_reason",
    "max_ligands",
    "linked_docking_targets", "source_list",
]


def _read_source(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = [str(field or "").strip() for field in (reader.fieldnames or []) if str(field or "").strip()]
        rows = [
            {str(key): ("" if value is None else str(value).strip()) for key, value in row.items()}
            for row in reader
        ]
    return fields, rows


def _validate_headers(source: Path, fields: list[str]) -> str:
    field_set = set(fields)
    has_version = "contract_version" in field_set
    has_status = "selection_status" in field_set
    if has_version != has_status:
        raise DockingError(
            f"Incomplete docking ligand contract in {source}: contract_version and selection_status must appear together."
        )
    if not field_set.intersection({"canonical_smiles", "isomeric_smiles", "smiles"}):
        raise DockingError(f"Invalid docking ligand contract in {source}: a SMILES column is required.")
    if not field_set.intersection({"ligand_id", "inchikey", "pubchem_cid"}):
        raise DockingError(f"Invalid docking ligand contract in {source}: a ligand identity column is required.")
    if not has_version:
        raise DockingError(
            f"Unversioned docking ligand input is not accepted in {source}; "
            f"regenerate it with contract_version={DOCKING_LIGAND_CONTRACT_VERSION}."
        )
    missing = {"ligand_id", "contract_version", "selection_status"} - field_set
    if missing:
        raise DockingError(f"Invalid {DOCKING_LIGAND_CONTRACT_VERSION} headers in {source}: missing {sorted(missing)}")
    return "versioned"


def _is_explicit_user_request_reason(value: str) -> bool:
    reason = value.strip().lower()
    return reason == "explicit_user_request" or reason.startswith("explicit_user_request_")


def network_ligands_to_input(
    run_dir: Path,
    out_csv: Path,
    selected_csv: Path,
    max_ligands: int = 5,
) -> Path:
    if max_ligands < 1:
        raise DockingError("max_ligands must be at least 1")
    source = run_dir / "07_core_targets" / "docking_ligands.csv"
    if not source.exists():
        raise DockingError(f"Network docking ligands not found: {source}")
    fields, source_rows = _read_source(source)
    contract_mode = _validate_headers(source, fields)

    rows: list[dict[str, str]] = []
    selected_rows: list[dict[str, object]] = []
    rejected_by_status = 0
    selected_by_explicit_exception = 0
    eligible_beyond_limit = 0
    versions: set[str] = set()
    exclusions = []
    warnings = []
    for index, row in enumerate(source_rows, start=1):
        is_explicit_exception = False
        version = row.get("contract_version", "")
        status = row.get("selection_status", "").lower()
        source_selection_reason = row.get("selection_reason", "")
        versions.add(version)
        if version != DOCKING_LIGAND_CONTRACT_VERSION:
            raise DockingError(
                f"Unsupported docking ligand contract_version at row {index}: {version or '<empty>'}"
            )
        if status in NON_SELECTED_STATUSES:
            rejected_by_status += 1
            exclusions.append({"ligand_id": row.get("ligand_id"), "reason": "upstream_" + status})
            continue
        if status == SELECTED_EXCEPTION_STATUS:
            if not _is_explicit_user_request_reason(source_selection_reason):
                raise DockingError(
                    f"Invalid selected_exception at row {index}: selection_reason must explicitly identify "
                    "explicit_user_request."
                )
            is_explicit_exception = True
        elif status != SELECTED_STATUS:
            raise DockingError(
                f"Invalid selection_status at row {index}: {row.get('selection_status') or '<empty>'}"
            )
        contract_version = version
        output_status = status
        selection_reason = (
            source_selection_reason
            if status == SELECTED_EXCEPTION_STATUS
            else "upstream_contract_selected_structure_ready_ligand"
        )

        smiles = row.get("canonical_smiles") or row.get("smiles") or row.get("isomeric_smiles")
        ligand_id = row.get("ligand_id") or row.get("inchikey") or row.get("pubchem_cid")
        if not smiles or not ligand_id:
            raise DockingError(
                f"Selected {DOCKING_LIGAND_CONTRACT_VERSION} row {index} lacks ligand_id or SMILES."
            )
        if len(rows) >= max_ligands:
            eligible_beyond_limit += 1
            exclusions.append({"ligand_id": ligand_id, "reason": "above_existing_max_ligands"})
            if is_explicit_exception or "explicit_user_request" in source_selection_reason:
                message = f"User ligand not selected (existing cap={max_ligands}): {ligand_id}"
                warnings.append(message)
                print("WARNING: " + message, flush=True)
            continue
        ligand = {
            "ligand_id": ligand_id,
            "compound_name": row.get("source_ingredient_names") or row.get("ingredient_name") or ligand_id,
            "pubchem_cid": row.get("pubchem_cid", ""),
            "canonical_smiles": row.get("canonical_smiles") or smiles,
            "isomeric_smiles": row.get("isomeric_smiles") or smiles,
            "inchikey": row.get("inchikey", ""),
        }
        rows.append(ligand)
        if is_explicit_exception:
            selected_by_explicit_exception += 1
        selected_rows.append(
            {
                **ligand,
                "original_rank": index,
                "original_score": row.get("ligand_score") or row.get("compound_target_count") or row.get("docking_target_count") or "",
                "source_file": str(source),
                "contract_version": contract_version,
                "selection_status": output_status,
                "source_contract_mode": contract_mode,
                "selected_for_docking": True,
                "selection_reason": selection_reason,
                "max_ligands": max_ligands,
                "linked_docking_targets": row.get("linked_docking_targets", ""),
                "source_list": row.get("source_list", ""),
            }
        )
    if not rows:
        raise DockingError("No contract-eligible, structure-ready ligands found in network docking_ligands.csv.")

    ensure_dir(out_csv.parent)
    write_rows(out_csv, rows, LIGAND_INPUT_FIELDS)
    write_rows(selected_csv, selected_rows, SELECTED_LIGAND_FIELDS)
    write_json(
        selected_csv.with_name("docking_ligand_contract.json"),
        {
            "contract_validation_status": "passed",
            "supported_contract_version": DOCKING_LIGAND_CONTRACT_VERSION,
            "input_contract_mode": contract_mode,
            "input_contract_versions": sorted(versions),
            "source_file": str(source),
            "rows_read": len(source_rows),
            "rows_selected": len(rows),
            "rows_selected_by_explicit_user_exception": selected_by_explicit_exception,
            "rows_contract_eligible_beyond_max_ligands": eligible_beyond_limit,
            "rows_rejected_by_selection_status": rejected_by_status,
            "warnings": warnings,
            "exclusions": exclusions,
        },
    )
    return out_csv
