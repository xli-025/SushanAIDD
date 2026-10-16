from __future__ import annotations

import csv
import time
from pathlib import Path
from typing import Any

import requests
from Bio.PDB import PDBParser

from modules.docking.scripts.common import ensure_dir, write_rows
from modules.docking.scripts.prepare_receptors import classify_hetero_residue, heavy_atom_count
from modules.docking.scripts.receptor_identity import IDENTITY_FIELDS, protein_signature, target_chains


RECEPTOR_INPUT_FIELDS = [
    "target_gene",
    "pdb_id",
    "chain_id",
    "structure_source",
    "local_structure_file",
    "cocrystal_ligand_id",
    "notes",
] + IDENTITY_FIELDS

STRUCTURE_SELECTION_FIELDS = [
    "target_gene",
    "uniprot_id",
    "original_rank",
    "original_score",
    "candidate_index",
    "pdb_id",
    "chain_id",
    "candidate_source",
    "local_structure_file",
    "reference_ligand_id",
    "reference_ligand_count",
    "receptor_preparation_status",
    "protonation_status",
    "backend_used",
    "charge_sanity_status",
    "binding_site_status",
    "usable_for_scientific_ranking",
    "selected_final",
    "rejection_reason",
    "selection_status",
    "failure_reason",
    "warning",
] + [field for field in IDENTITY_FIELDS if field != "uniprot_id"]


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return [{key: (value or "").strip() for key, value in row.items()} for row in csv.DictReader(f)]


def recommended_targets(path: Path, max_targets: int) -> list[dict[str, str]]:
    rows = []
    for index, row in enumerate(read_rows(path), start=1):
        if row.get("recommended_for_docking", "true").lower() == "true":
            enriched = dict(row)
            enriched["_original_index"] = str(index)
            rows.append(enriched)
    return rows[:max_targets]


def search_rcsb_entries(uniprot_id: str, max_candidates: int = 15) -> list[str]:
    query = {
        "query": {
            "type": "terminal",
            "service": "text",
            "parameters": {
                "attribute": "rcsb_polymer_entity_container_identifiers.reference_sequence_identifiers.database_accession",
                "operator": "exact_match",
                "value": uniprot_id,
            },
        },
        "return_type": "entry",
        "request_options": {
            "results_content_type": ["experimental"],
            "paginate": {"start": 0, "rows": max_candidates},
            "sort": [{"sort_by": "score", "direction": "desc"}],
        },
    }
    response = requests.post("https://search.rcsb.org/rcsbsearch/v2/query", json=query, timeout=30)
    response.raise_for_status()
    data = response.json()
    return [item["identifier"] for item in data.get("result_set", []) if item.get("identifier")]


def download_pdb(pdb_id: str, out_dir: Path) -> Path:
    path = out_dir / f"{pdb_id.lower()}.pdb"
    if path.exists() and path.stat().st_size > 0:
        return path
    urls = [
        f"https://files.rcsb.org/download/{pdb_id}.pdb",
        f"https://files.rcsb.org/view/{pdb_id}.pdb",
    ]
    errors = []
    for attempt in range(1, 4):
        for url in urls:
            try:
                response = requests.get(url, timeout=45, headers={"User-Agent": "NPFlow-Docking/1.8"})
                response.raise_for_status()
                text = response.text
                if "ATOM" not in text and "HETATM" not in text:
                    raise ValueError("downloaded file does not contain ATOM/HETATM records")
                path.write_text(text, encoding="utf-8")
                return path
            except Exception as exc:
                errors.append(f"{url}: {exc}")
        time.sleep(1.0 * attempt)
    raise RuntimeError("; ".join(errors[-4:]))


def inspect_pdb_for_reference_ligand(path: Path, pdb_id: str, allowed_chains: dict | None = None) -> dict[str, Any]:
    parser = PDBParser(QUIET=True)
    structure = parser.get_structure(pdb_id, str(path))
    best: dict[str, Any] = {"chain_id": "", "ligands": []}
    for chain in structure[0]:
        if allowed_chains is not None and chain.id not in allowed_chains:
            continue
        protein_atoms = [atom for residue in chain if residue.id[0] == " " for atom in residue.get_atoms()]
        ligands = []
        for residue in chain:
            if residue.id[0] == " ":
                continue
            if classify_hetero_residue(residue) == "reference_pocket_ligand":
                ligands.append((residue.get_resname().strip(), heavy_atom_count(residue)))
        if len(protein_atoms) > 20 and ligands:
            if not best["ligands"] or len(ligands) > len(best["ligands"]):
                best = {"chain_id": chain.id, "ligands": ligands,
                        **((allowed_chains or {}).get(chain.id, {})),
                        "selected_chain_sequence": protein_signature(chain)}
    return best


def select_receptor_structures(
    docking_targets_csv: Path,
    *,
    out_dir: Path,
    max_targets: int = 3,
    max_candidates_per_target: int = 15,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    out_dir = ensure_dir(out_dir)
    raw_pdb_dir = ensure_dir(out_dir / "raw_pdb")
    receptor_rows: list[dict[str, Any]] = []
    qc_rows: list[dict[str, Any]] = []
    for target in recommended_targets(docking_targets_csv, max_targets=max_targets):
        target_gene = target.get("target_gene", "")
        uniprot_id = target.get("uniprot_id", "")
        original_rank = target.get("core_rank") or target.get("_original_index", "")
        original_score = target.get("disease_association_score", "")
        if not uniprot_id:
            qc_rows.append(
                {
                    "target_gene": target_gene,
                    "uniprot_id": uniprot_id,
                    "original_rank": original_rank,
                    "original_score": original_score,
                    "selection_status": "failed",
                    "failure_reason": "missing_uniprot_id",
                }
            )
            continue
        try:
            candidates = search_rcsb_entries(uniprot_id, max_candidates=max_candidates_per_target)
        except Exception as exc:
            qc_rows.append(
                {
                    "target_gene": target_gene,
                    "uniprot_id": uniprot_id,
                    "original_rank": original_rank,
                    "original_score": original_score,
                    "selection_status": "failed",
                    "failure_reason": "rcsb_search_failed",
                    "warning": str(exc)[:200],
                }
            )
            continue
        for candidate_index, pdb_id in enumerate(candidates, start=1):
            try:
                pdb_file = download_pdb(pdb_id, raw_pdb_dir)
                verified = target_chains(pdb_id, uniprot_id, out_dir / "identity_evidence")
                info = inspect_pdb_for_reference_ligand(pdb_file, pdb_id, verified)
            except Exception as exc:
                qc_rows.append(
                    {
                        "target_gene": target_gene,
                        "uniprot_id": uniprot_id,
                        "original_rank": original_rank,
                        "original_score": original_score,
                        "candidate_index": candidate_index,
                        "pdb_id": pdb_id,
                        "candidate_source": "rcsb_uniprot_search",
                        "selection_status": "failed",
                        "failure_reason": "candidate_download_or_parse_failed",
                        "warning": str(exc)[:200],
                    }
                )
                continue
            if info.get("chain_id") and info.get("ligands"):
                qc_rows.append(
                    {
                        **{key: info.get(key, "") for key in IDENTITY_FIELDS},
                        "target_gene": target_gene,
                        "uniprot_id": uniprot_id,
                        "original_rank": original_rank,
                        "original_score": original_score,
                        "candidate_index": candidate_index,
                        "pdb_id": pdb_id,
                        "chain_id": info.get("chain_id", ""),
                        "candidate_source": "rcsb_uniprot_search",
                        "local_structure_file": str(pdb_file.resolve()),
                        "reference_ligand_id": sorted(info["ligands"], key=lambda item: item[1], reverse=True)[0][0],
                        "reference_ligand_count": len(info["ligands"]),
                        "selection_status": "candidate_has_reference_ligand",
                        "failure_reason": "",
                        "warning": "",
                    }
                )
            else:
                qc_rows.append(
                    {
                        "target_gene": target_gene,
                        "uniprot_id": uniprot_id,
                        "original_rank": original_rank,
                        "original_score": original_score,
                        "candidate_index": candidate_index,
                        "pdb_id": pdb_id,
                        "candidate_source": "rcsb_uniprot_search",
                        "local_structure_file": str(pdb_file.resolve()),
                        "reference_ligand_count": 0,
                        "selection_status": "rejected",
                        "failure_reason": "no_valid_reference_pocket_ligand",
                        "warning": "",
                    }
                )
        target_candidates = [
            row
            for row in qc_rows
            if row.get("target_gene") == target_gene
            and row.get("selection_status") == "candidate_has_reference_ligand"
        ]
        if not target_candidates:
            qc_rows.append(
                {
                    "target_gene": target_gene,
                    "uniprot_id": uniprot_id,
                    "original_rank": original_rank,
                    "original_score": original_score,
                    "selection_status": "skipped",
                    "failure_reason": "no_valid_reference_pocket_ligand",
                    "warning": f"searched_candidates={';'.join(candidates[:max_candidates_per_target])}",
                }
            )
            continue
        selected_candidate = sorted(target_candidates, key=lambda row: int(row.get("candidate_index") or 999999))[0]
        receptor_rows.append(
            {
                **{key: selected_candidate.get(key, "") for key in IDENTITY_FIELDS},
                "target_gene": target_gene,
                "pdb_id": selected_candidate["pdb_id"],
                "chain_id": selected_candidate["chain_id"],
                "structure_source": "local_pdb",
                "local_structure_file": selected_candidate["local_structure_file"],
                "cocrystal_ligand_id": selected_candidate["reference_ligand_id"],
                "notes": f"selected_from_rcsb_by_uniprot:{uniprot_id}",
            }
        )
    write_rows(out_dir / "selected_receptors.csv", receptor_rows, RECEPTOR_INPUT_FIELDS)
    write_rows(out_dir / "target_structure_selection.csv", receptor_rows, RECEPTOR_INPUT_FIELDS)
    write_rows(out_dir / "target_structure_selection_qc.csv", qc_rows, STRUCTURE_SELECTION_FIELDS)
    return receptor_rows, qc_rows
