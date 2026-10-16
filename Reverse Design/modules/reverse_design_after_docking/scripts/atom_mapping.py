"""Pose-to-SMILES mapping; a graph mapping is not a constrained final pose."""
from __future__ import annotations

import os
from pathlib import Path

from rdkit import Chem

TRACE_FIELDS = ["assembly_method", "atom_mapping_status", "mapping_reason",
                "attachment_pose_atom_index", "actual_parent_atom_index",
                "actual_fragment_atom_index", "candidate_inchikey",
                "optimization_method", "optimization_status", "optimization_detail",
                "ligand_ph_requested", "ligand_protonation_status", "ligand_state_mode"]


def pose_to_parent(seed: dict) -> tuple[dict[int, int], str, str]:
    if os.environ.get("NPFLOW_ATOM_MAPPING_MODE", "mapped").lower() == "legacy":
        return {}, "legacy_disabled", "new_mapping_disabled_by_configuration"
    if seed.get("pose_sdf_source") != "obabel_pose_pdbqt_to_sdf":
        return {}, "legacy_fallback", "spatial_pose_not_confirmed"
    path = Path(str(seed.get("parent_best_pose_sdf") or ""))
    if not path.is_file():
        return {}, "legacy_fallback", "pose_file_missing"
    parent = Chem.MolFromSmiles(str(seed.get("smiles") or ""))
    if parent is None:
        raise ValueError("invalid_parent_structure")
    try:
        pose = next((m for m in Chem.SDMolSupplier(str(path), removeHs=False) if m is not None), None)
        if pose is None:
            return {}, "legacy_fallback", "pose_parse_failed"
        for atom in pose.GetAtoms():
            atom.SetIntProp("original_pose_index", atom.GetIdx())
        pose = Chem.RemoveHs(pose)
        if pose.GetNumAtoms() != parent.GetNumAtoms() or pose.GetNumBonds() != parent.GetNumBonds():
            return {}, "legacy_fallback", "complete_graph_mapping_unavailable"
        matches = parent.GetSubstructMatches(pose, useChirality=True, uniquify=False, maxMatches=128)
        if not matches:
            return {}, "legacy_fallback", "complete_stereo_graph_mapping_unavailable"
        # Multiple full graph automorphisms are chemically equivalent, not different identities.
        chosen = min(matches)
        return ({atom.GetIntProp("original_pose_index"): chosen[atom.GetIdx()] for atom in pose.GetAtoms()},
                "verified_equivalent_symmetry" if len(matches) > 1 else "verified_full_graph", "")
    except Exception as exc:
        return {}, "legacy_fallback", f"mapping_error:{type(exc).__name__}:{exc}"
