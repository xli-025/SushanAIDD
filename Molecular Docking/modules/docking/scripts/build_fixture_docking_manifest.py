from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from modules.docking.scripts.common import ensure_dir, write_json
from modules.docking.scripts.receptor_identity import IDENTITY_FIELDS


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        return [{key: (value or "").strip() for key, value in row.items()} for row in csv.DictReader(f)]


def matching(rows: list[dict[str, str]], key: str, value: str) -> list[dict[str, str]]:
    return [row for row in rows if row.get(key) == value]


def build_fixture_docking_manifest(
    *,
    receptor_qc: Path,
    ligand_qc: Path,
    box_csv: Path,
    out_manifest: Path,
    selected_targets: Path | None = None,
    selected_ligands: Path | None = None,
) -> dict[str, Any]:
    receptors = matching(read_rows(receptor_qc), "preparation_status", "success")
    ligands = matching(read_rows(ligand_qc), "pdbqt_status", "success")
    boxes = matching(read_rows(box_csv), "binding_site_status", "defined")
    selected_target_rows = read_rows(selected_targets) if selected_targets and selected_targets.exists() else []
    selected_ligand_rows = read_rows(selected_ligands) if selected_ligands and selected_ligands.exists() else []
    if not receptors:
        raise ValueError("No successful receptors found in receptor QC.")
    if not ligands:
        raise ValueError("No successful ligands found in ligand QC.")
    if not boxes:
        raise ValueError("No defined binding site boxes found.")
    pairs: list[dict[str, Any]] = []
    for receptor in receptors:
        target_meta = next((row for row in selected_target_rows if row.get("target_gene") == receptor.get("target_gene")), {})
        receptor_boxes = [
            box
            for box in boxes
            if box.get("target_gene") == receptor.get("target_gene")
            and box.get("pdb_id") == receptor.get("pdb_id")
            and box.get("chain_id") == receptor.get("chain_id")
        ]
        for ligand in ligands:
            ligand_meta = next((row for row in selected_ligand_rows if row.get("ligand_id") == ligand.get("ligand_id")), {})
            for box in receptor_boxes:
                pair_id = f"{receptor.get('target_gene')}__{ligand.get('ligand_id')}__{box.get('box_id')}"
                pairs.append(
                    {
                        "pair_id": pair_id,
                        "target_identity": {key: receptor.get(key, "") for key in IDENTITY_FIELDS},
                        "pair_evidence_type": "database_linked_pair" if receptor.get("target_gene") in {
                            value.strip() for value in ligand_meta.get("linked_docking_targets", "").split(";") if value.strip()
                        } else "exploratory_cross_docking_not_database_pair_evidence",
                        "ligand_source_list": ligand_meta.get("source_list", ""),
                        "job_id": pair_id,
                        "target_gene": receptor.get("target_gene"),
                        "target_source_rank": target_meta.get("original_rank", ""),
                        "target_source_score": target_meta.get("original_score", ""),
                        "pdb_id": receptor.get("pdb_id"),
                        "chain_id": receptor.get("chain_id"),
                        "ligand_id": ligand.get("ligand_id"),
                        "compound_name": ligand.get("compound_name") or ligand_meta.get("compound_name", ""),
                        "pubchem_cid": ligand.get("pubchem_cid") or ligand_meta.get("pubchem_cid", ""),
                        "canonical_smiles": ligand.get("canonical_smiles") or ligand_meta.get("canonical_smiles", ""),
                        "isomeric_smiles": ligand.get("isomeric_smiles") or ligand_meta.get("isomeric_smiles", ""),
                        "receptor_pdbqt": str(Path(receptor.get("prepared_receptor_pdbqt", "")).resolve()),
                        "ligand_pdbqt": str(Path(ligand.get("pdbqt_path", "")).resolve()),
                        "box_id": box.get("box_id"),
                        "box_source": box.get("box_source", ""),
                        "reference_pocket_ligand_id": box.get("box_defining_ligand_id") or box.get("cocrystal_ligand_id", ""),
                        "box": {
                            "box_id": box.get("box_id"),
                            "center_x": float(box.get("center_x")),
                            "center_y": float(box.get("center_y")),
                            "center_z": float(box.get("center_z")),
                            "size_x": float(box.get("size_x")),
                            "size_y": float(box.get("size_y")),
                            "size_z": float(box.get("size_z")),
                        },
                        "vina_params": {
                            "exhaustiveness": 8,
                            "num_modes": 9,
                            "energy_range": 3,
                            "num_replicates": 1,
                            "seed": "deterministic_pair_seed",
                            "cpu": 0,
                        },
                        "receptor_qc_flags": {
                            "usable_for_scientific_ranking": receptor.get("usable_for_scientific_ranking"),
                            "warning": receptor.get("warning"),
                            "backend_used": receptor.get("backend_used"),
                            "charge_sanity_status": receptor.get("charge_sanity_status"),
                            "protonation_status": receptor.get("protonation_status"),
                        },
                        "ligand_qc_flags": {
                            "pdbqt_status": ligand.get("pdbqt_status"),
                            "warning": ligand.get("warning"),
                            "confidence_level": ligand.get("confidence_level"),
                            "ligand_state_mode": ligand.get("ligand_state_mode"),
                            "tautomer_enumeration": ligand.get("tautomer_enumeration"),
                            "protomer_enumeration": ligand.get("protomer_enumeration"),
                            "ph_requested": ligand.get("ph_requested"),
                            "ph_protonation_status": ligand.get("ph_protonation_status"),
                            "ph_protonation_backend": ligand.get("ph_protonation_backend"),
                            "ph_protonation_fallback_reason": ligand.get("ph_protonation_fallback_reason"),
                        },
                    }
                )
    manifest = {
        "project_name": "real_fixture_docking",
        "default_profile": "npflow_docking_default_v1",
        "pairs": pairs,
    }
    ensure_dir(out_manifest.parent)
    write_json(out_manifest, manifest)
    return manifest
