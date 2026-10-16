from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from modules.docking.scripts.build_fixture_docking_manifest import build_fixture_docking_manifest
from modules.docking.scripts.check_dependencies import diagnose
from modules.docking.scripts.common import DEFAULT_OUTPUT_DIR, DockingError, ensure_dir, write_json, write_rows
from modules.docking.scripts.define_binding_sites import define_binding_sites
from modules.docking.scripts.generate_docking_report import generate_report
from modules.docking.scripts.ligand_state_contract import validate_ligand_ph
from modules.docking.scripts.network_ligand_contract import network_ligands_to_input
from modules.docking.scripts.prepare_ligands import prepare_ligands
from modules.docking.scripts.prepare_receptors import prepare_receptors
from modules.docking.scripts.run_vina_batch import run_pairs
from modules.docking.scripts.select_receptor_structures import select_receptor_structures
from modules.docking.scripts.receptor_identity import IDENTITY_FIELDS


SELECTED_TARGET_FIELDS = [
    "target_gene",
    "uniprot_id",
    "original_rank",
    "original_score",
    "source_file",
    "selected_for_docking",
    "selection_reason",
    "max_targets",
    "max_candidates_per_target",
]


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return [{key: (value or "").strip() for key, value in row.items()} for row in csv.DictReader(f)]


def resolve_network_run_dir(run_dir: Path | None) -> Path:
    if run_dir:
        return run_dir.resolve()
    latest = Path(__file__).resolve().parents[4] / "Network Pharmacology" / "results" / "network_pharmacology_runs" / "latest_run.json"
    if not latest.exists():
        raise DockingError("Network pharmacology latest_run.json not found; pass --network-run-dir explicitly.")
    data = json.loads(latest.read_text(encoding="utf-8"))
    path = Path(data.get("output_dir", ""))
    if not path.exists():
        alt = Path(__file__).resolve().parents[4] / "Network Pharmacology" / path
        path = alt if alt.exists() else path
    if not path.exists():
        raise DockingError(f"Network pharmacology run_dir not found: {path}")
    return path.resolve()


def write_selected_targets(run_dir: Path, out_csv: Path, max_targets: int, max_candidates_per_target: int) -> None:
    source = run_dir / "07_core_targets" / "docking_targets.csv"
    rows = []
    for index, row in enumerate(read_rows(source), start=1):
        if len(rows) >= max_targets:
            break
        if row.get("recommended_for_docking", "true").lower() != "true":
            continue
        rows.append(
            {
                "target_gene": row.get("target_gene", ""),
                "uniprot_id": row.get("uniprot_id", ""),
                "original_rank": row.get("core_rank") or index,
                "original_score": row.get("disease_association_score", ""),
                "source_file": str(source),
                "selected_for_docking": True,
                "selection_reason": "top_recommended_targets",
                "max_targets": max_targets,
                "max_candidates_per_target": max_candidates_per_target,
            }
        )
    write_rows(out_csv, rows, SELECTED_TARGET_FIELDS)
    selected_ids = {row["target_gene"] for row in rows}
    exclusions = []
    for row in read_rows(source):
        if row.get("target_gene") not in selected_ids:
            reason = "above_existing_max_targets" if row.get("recommended_for_docking", "true").lower() == "true" else "not_recommended"
            exclusions.append({"target_gene": row.get("target_gene", ""), "reason": reason})
            if "explicit_user_request" in row.get("selection_reason", ""):
                print(f"WARNING: user target not selected: {row.get('target_gene')}; {reason}", flush=True)
    write_json(out_csv.with_name("target_selection_summary.json"), {"selected": len(rows), "max_targets": max_targets, "exclusions": exclusions})


def truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}


def prepare_candidate_receptor(
    *,
    candidate: dict[str, str],
    out_dir: Path,
    ph: float = 7.4,
) -> dict[str, Any]:
    selected_csv = out_dir / "candidate_input.csv"
    write_rows(
        selected_csv,
        [
            {
                **{key: candidate.get(key, "") for key in IDENTITY_FIELDS},
                "target_gene": candidate.get("target_gene", ""),
                "pdb_id": candidate.get("pdb_id", ""),
                "chain_id": candidate.get("chain_id", ""),
                "structure_source": "local_pdb",
                "local_structure_file": candidate.get("local_structure_file", ""),
                "cocrystal_ligand_id": candidate.get("reference_ligand_id", ""),
                "notes": f"candidate_index={candidate.get('candidate_index', '')}",
            }
        ],
        ["target_gene", "pdb_id", "chain_id", "structure_source", "local_structure_file", "cocrystal_ligand_id", "notes"] + IDENTITY_FIELDS,
    )
    receptor_rows = prepare_receptors(selected_csv, out_dir=out_dir / "receptor", ph=ph)
    receptor = receptor_rows[0] if receptor_rows else {}
    box_rows = []
    if receptor.get("preparation_status") == "success":
        box_rows = define_binding_sites(out_dir / "receptor" / "receptor_preparation_qc.csv", out_dir=out_dir / "boxes")
    box_success = any(row.get("binding_site_status") == "defined" for row in box_rows)
    return {
        "receptor": receptor,
        "boxes": box_rows,
        "binding_site_status": "defined" if box_success else "failed",
        "usable_for_scientific_ranking": truthy(receptor.get("usable_for_scientific_ranking")) and box_success,
    }


def select_scientific_receptors_from_candidates(
    *,
    structure_qc: Path,
    out_dir: Path,
    max_candidates_per_target: int,
    ph: float = 7.4,
) -> list[dict[str, Any]]:
    raw_rows = read_rows(structure_qc)
    candidates = [row for row in raw_rows if row.get("selection_status") == "candidate_has_reference_ligand"]
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in raw_rows:
        grouped.setdefault(row.get("target_gene", ""), []).append(row)
    selected: list[dict[str, Any]] = []
    all_attempt_rows: list[dict[str, Any]] = []
    for target_gene, rows in grouped.items():
        candidate_rows = [row for row in rows if row.get("selection_status") == "candidate_has_reference_ligand"]
        target_attempts = sorted(candidate_rows, key=lambda row: int(row.get("candidate_index") or 999999))[:max_candidates_per_target]
        fallback: dict[str, Any] | None = None
        evaluated_indexes: set[str] = set()
        for row in target_attempts:
            evaluated_indexes.add(str(row.get("candidate_index", "")))
            candidate_dir = out_dir / "candidate_eval" / target_gene / f"candidate_{row.get('candidate_index')}_{row.get('pdb_id')}"
            result = prepare_candidate_receptor(candidate=row, out_dir=candidate_dir, ph=ph)
            receptor = result["receptor"]
            attempt = {
                **row,
                "receptor_preparation_status": receptor.get("preparation_status", "failed"),
                "protonation_status": receptor.get("protonation_status", ""),
                "backend_used": receptor.get("backend_used", ""),
                "charge_sanity_status": receptor.get("charge_sanity_status", ""),
                "binding_site_status": result["binding_site_status"],
                "usable_for_scientific_ranking": receptor.get("usable_for_scientific_ranking", False),
                "selected_final": False,
                "rejection_reason": "",
            }
            if result["usable_for_scientific_ranking"]:
                attempt["selected_final"] = True
                all_attempt_rows.append(attempt)
                selected.append(
                    {
                        **{key: row.get(key, "") for key in IDENTITY_FIELDS},
                        "target_gene": row.get("target_gene", ""),
                        "pdb_id": row.get("pdb_id", ""),
                        "chain_id": row.get("chain_id", ""),
                        "structure_source": "local_pdb",
                        "local_structure_file": row.get("local_structure_file", ""),
                        "cocrystal_ligand_id": row.get("reference_ligand_id", ""),
                        "notes": f"selected_scientific_usable_candidate_index={row.get('candidate_index')}",
                    }
                )
                break
            attempt["rejection_reason"] = "not_scientific_usable"
            all_attempt_rows.append(attempt)
            if fallback is None and receptor.get("preparation_status") == "success" and result["binding_site_status"] == "defined":
                fallback = {"row": row, "attempt": attempt}
        else:
            if fallback:
                fallback["attempt"]["selected_final"] = True
                fallback["attempt"]["rejection_reason"] = "selected_engineering_fallback_after_candidate_retry"
                row = fallback["row"]
                selected.append(
                    {
                        **{key: row.get(key, "") for key in IDENTITY_FIELDS},
                        "target_gene": row.get("target_gene", ""),
                        "pdb_id": row.get("pdb_id", ""),
                        "chain_id": row.get("chain_id", ""),
                        "structure_source": "local_pdb",
                        "local_structure_file": row.get("local_structure_file", ""),
                        "cocrystal_ligand_id": row.get("reference_ligand_id", ""),
                        "notes": f"selected_low_confidence_fallback_after_{len(target_attempts)}_candidates",
                    }
                )
        for row in rows:
            candidate_index = str(row.get("candidate_index", ""))
            if candidate_index and candidate_index not in evaluated_indexes and row.get("selection_status") != "candidate_has_reference_ligand":
                all_attempt_rows.append(
                    {
                        **row,
                        "receptor_preparation_status": "",
                        "protonation_status": "",
                        "backend_used": "",
                        "charge_sanity_status": "",
                        "binding_site_status": "",
                        "usable_for_scientific_ranking": False,
                        "selected_final": False,
                        "rejection_reason": row.get("failure_reason", "not_evaluated_for_receptor_preparation"),
                    }
                )
    write_rows(out_dir / "01_target_structures" / "target_structure_selection_qc.csv", all_attempt_rows, [
        "target_gene", "uniprot_id", "original_rank", "original_score", "candidate_index", "pdb_id", "chain_id",
        "candidate_source", "local_structure_file", "reference_ligand_id", "reference_ligand_count",
        "receptor_preparation_status", "protonation_status", "backend_used", "charge_sanity_status",
        "binding_site_status", "usable_for_scientific_ranking", "selected_final", "rejection_reason",
        "selection_status", "failure_reason", "warning",
    ] + [field for field in IDENTITY_FIELDS if field != "uniprot_id"])
    write_rows(out_dir / "01_target_structures" / "target_structure_selection.csv", selected, [
        "target_gene", "pdb_id", "chain_id", "structure_source", "local_structure_file", "cocrystal_ligand_id", "notes",
    ] + IDENTITY_FIELDS)
    write_rows(out_dir / "01_target_structures" / "selected_receptors.csv", selected, [
        "target_gene", "pdb_id", "chain_id", "structure_source", "local_structure_file", "cocrystal_ligand_id", "notes",
    ] + IDENTITY_FIELDS)
    return selected


def run_from_network(
    *,
    network_run_dir: Path | None = None,
    out_dir: Path | None = None,
    max_targets: int = 3,
    max_ligands: int = 5,
    max_candidates_per_target: int = 15,
    ph: float = 7.4,
) -> dict[str, Any]:
    try:
        ph = validate_ligand_ph(ph)
    except ValueError as exc:
        raise DockingError(str(exc)) from exc
    network_dir = resolve_network_run_dir(network_run_dir)
    if out_dir is None:
        out_dir = network_dir / "09_docking" if network_run_dir else DEFAULT_OUTPUT_DIR / "network_bridge"
    out_dir = ensure_dir(out_dir)
    diagnostic = diagnose(output_dir=out_dir, write=True)
    if diagnostic.get("blocking_missing_dependencies"):
        raise DockingError("docking_dependency_failure: " + "; ".join(diagnostic["blocking_missing_dependencies"]))

    manifest_dir = ensure_dir(out_dir / "00_input_manifest")
    selected_targets_path = manifest_dir / "selected_targets.csv"
    selected_ligands_path = manifest_dir / "selected_ligands.csv"
    write_selected_targets(network_dir, selected_targets_path, max_targets, max_candidates_per_target)
    ligand_input = network_ligands_to_input(
        network_dir,
        manifest_dir / "network_ligands_input.csv",
        selected_ligands_path,
        max_ligands=max_ligands,
    )
    ligand_qc_rows = prepare_ligands(ligand_input, out_dir=out_dir / "03_ligand_prepared", ph=ph)

    def no_result(reason):
        outcome = {"status": "no_result", "reason": reason, "pairs_scored": 0,
                   "quantity_policy": "upper_limits_only_no_padding", "out_dir": str(out_dir),
                   "ligands_prepared": sum(row.get("pdbqt_status") == "success" for row in ligand_qc_rows)}
        write_json(out_dir / "docking_no_result.json", outcome)
        print("Docking no-result: " + json.dumps(outcome, ensure_ascii=False), flush=True)
        return outcome

    if not any(row.get("pdbqt_status") == "success" for row in ligand_qc_rows):
        return no_result("no_qualified_ligand_after_preparation")

    docking_targets = network_dir / "07_core_targets" / "docking_targets.csv"
    receptor_rows, _ = select_receptor_structures(
        docking_targets,
        out_dir=out_dir / "01_target_structures",
        max_targets=max_targets,
        max_candidates_per_target=max_candidates_per_target,
    )
    receptor_rows = select_scientific_receptors_from_candidates(
        structure_qc=out_dir / "01_target_structures" / "target_structure_selection_qc.csv",
        out_dir=out_dir,
        max_candidates_per_target=max_candidates_per_target,
        ph=ph,
    )
    if not receptor_rows:
        return no_result("no_verified_target_with_valid_reference_pocket")
    receptor_qc_rows = prepare_receptors(
        out_dir / "01_target_structures" / "selected_receptors.csv",
        out_dir=out_dir / "02_receptor_prepared",
        ph=ph,
    )
    box_rows = define_binding_sites(
        out_dir / "02_receptor_prepared" / "receptor_preparation_qc.csv",
        out_dir=out_dir / "04_binding_sites",
    )
    if not any(row.get("preparation_status") == "success" for row in receptor_qc_rows) or not any(row.get("binding_site_status") == "defined" for row in box_rows):
        return no_result("no_qualified_prepared_receptor_and_pocket")
    manifest = build_fixture_docking_manifest(
        receptor_qc=out_dir / "02_receptor_prepared" / "receptor_preparation_qc.csv",
        ligand_qc=out_dir / "03_ligand_prepared" / "ligand_preparation_qc.csv",
        box_csv=out_dir / "04_binding_sites" / "binding_site_boxes.csv",
        out_manifest=out_dir / "00_input_manifest" / "docking_input_manifest.json",
        selected_targets=selected_targets_path,
        selected_ligands=selected_ligands_path,
    )
    write_json(out_dir / "00_input_manifest" / "network_docking_manifest.json", manifest)
    pair_rows, _ = run_pairs(
        manifest,
        output_dir=out_dir,
        manifest_base=(out_dir / "00_input_manifest"),
        exhaustiveness=16,
        num_modes=9,
        energy_range=3,
        num_replicates=1,
    )
    # run_pairs writes the effective numeric parameters, not placeholder defaults.
    write_json(out_dir / "00_input_manifest" / "docking_input_manifest.json", manifest)
    write_json(out_dir / "00_input_manifest" / "network_docking_manifest.json", manifest)
    report = generate_report(
        pair_rows,
        dependency_diagnostic=diagnostic,
        output_dir=out_dir,
        project_name=f"network_to_docking_{network_dir.name}",
    )
    coverage = {"max_targets": max_targets, "max_ligands": max_ligands,
        "targets_selected": len(read_rows(selected_targets_path)), "ligands_selected": len(read_rows(selected_ligands_path)),
        "receptors_prepared": sum(row.get("preparation_status") == "success" for row in receptor_qc_rows),
        "ligands_prepared": sum(row.get("pdbqt_status") == "success" for row in ligand_qc_rows),
        "pairs_planned": len(manifest.get("pairs", [])), "pairs_scored": sum(row.get("status") == "success" for row in pair_rows),
        "ligand_exclusions": [{"ligand_id": row.get("ligand_id"), "reason": row.get("failure_reason")} for row in ligand_qc_rows if row.get("pdbqt_status") != "success"],
        "quantity_policy": "upper_limits_only_no_padding", "pair_evidence": "see_each_manifest_pair"}
    write_json(manifest_dir / "actual_coverage.json", coverage)
    print("Actual docking coverage: " + json.dumps(coverage, ensure_ascii=False), flush=True)
    return {
        "network_run_dir": str(network_dir),
        "out_dir": str(out_dir),
        "ligands_prepared": sum(1 for row in ligand_qc_rows if row.get("pdbqt_status") == "success"),
        "receptors_prepared": sum(1 for row in receptor_qc_rows if row.get("preparation_status") == "success"),
        "boxes_defined": sum(1 for row in box_rows if row.get("binding_site_status") == "defined"),
        "pairs": len(manifest.get("pairs", [])),
        "successful_pairs": report["summary"]["successful_pairs"],
        "report": str(out_dir / "08_docking_report" / "docking_report.html"),
    }
