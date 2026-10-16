from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

from meeko import MoleculePreparation, PDBQTWriterLegacy
from rdkit import Chem
from rdkit.Chem import AllChem, SDWriter

from .common import deterministic_seed, ensure_dir, read_rows, to_float, write_rows
from .atom_mapping import TRACE_FIELDS
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from npflow_chemistry import apply_requested_ph


VINA_SELECTION_FIELDS = [
    "candidate_id",
    "seed_pair_id",
    "round",
    "generation_backend",
    "scoring_engine",
    "execution_mode",
    "fallback_reason",
    "candidate_smiles",
    "canonical_smiles",
    "qed",
    "parent_similarity",
    "fragment_score",
    "candidate_sdf",
    "candidate_pdbqt",
    "vina_preparation_status",
    "failure_reason",
] + TRACE_FIELDS


def _embed(smiles: str, seed_text: str) -> Chem.Mol:
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise ValueError("invalid_smiles")
    mol = Chem.AddHs(mol)
    params = AllChem.ETKDGv3() if hasattr(AllChem, "ETKDGv3") else AllChem.ETKDG()
    params.randomSeed = deterministic_seed(seed_text)
    if AllChem.EmbedMolecule(mol, params) != 0:
        raise ValueError("cannot_embed_3d")
    try:
        if AllChem.MMFFHasAllMoleculeParams(mol):
            mol.SetProp("optimization_method", "MMFF94")
            result = AllChem.MMFFOptimizeMolecule(mol, mmffVariant="MMFF94")
        else:
            mol.SetProp("optimization_method", "UFF")
            result = AllChem.UFFOptimizeMolecule(mol)
        mol.SetProp("optimization_status", "converged" if result == 0 else "not_converged")
        mol.SetProp("optimization_detail", str(result))
    except Exception as exc:
        mol.SetProp("optimization_status", "exception")
        mol.SetProp("optimization_detail", str(exc))
    return mol


def _write_sdf(mol: Chem.Mol, path: Path) -> None:
    writer = SDWriter(str(path))
    try:
        writer.write(mol)
    finally:
        writer.close()


def _mol_to_pdbqt(mol: Chem.Mol) -> str:
    setups = MoleculePreparation().prepare(mol)
    if not setups:
        raise ValueError("meeko_no_setup")
    result = PDBQTWriterLegacy.write_string(setups[0])
    if isinstance(result, tuple):
        text = result[0]
        success = result[1] if len(result) > 1 else True
        message = result[2] if len(result) > 2 else ""
        if not success:
            raise ValueError(str(message or "meeko_writer_failed"))
        return str(text)
    return str(result)


def prepare_candidates_for_vina(
    *,
    filtered_csv: Path,
    out_csv: Path,
    out_dir: Path,
    limit: int = 10,
    ph: float | None = 7.4,
) -> list[dict[str, Any]]:
    ensure_dir(out_dir)
    sdf_dir = ensure_dir(out_dir / "candidate_sdf")
    pdbqt_dir = ensure_dir(out_dir / "candidate_pdbqt")
    rows = [row for row in read_rows(filtered_csv) if row.get("filter_status") == "pass"]
    rows.sort(
        key=lambda row: (
            -float(to_float(row.get("qed"), 0.0) or 0.0),
            abs(float(to_float(row.get("heavy_atom_delta"), 0.0) or 0.0)),
            -float(to_float(row.get("parent_similarity"), 0.0) or 0.0),
            -float(to_float(row.get("fragment_score"), 0.0) or 0.0),
        )
    )
    selected: list[dict[str, Any]] = []
    for row in rows[:limit]:
        candidate_id = row.get("candidate_id", "")
        smiles = row.get("canonical_smiles", "")
        sdf_path = sdf_dir / f"{candidate_id}.sdf"
        pdbqt_path = pdbqt_dir / f"{candidate_id}.pdbqt"
        status = "success"
        failure = ""
        trace = {key: row.get(key, "") for key in TRACE_FIELDS}
        try:
            mol = _embed(smiles, f"{candidate_id}|vina")
            trace.update({key: mol.GetProp(key) if mol.HasProp(key) else "unknown" for key in (
                "optimization_method", "optimization_status", "optimization_detail")})
            mol, protonation, backend, reason, state = apply_requested_ph(mol, ph)
            trace.update({"ligand_ph_requested": ph, "ligand_protonation_status": protonation,
                          "ligand_state_mode": state})
            if reason:
                trace["optimization_detail"] += ";protonation_fallback:" + reason
            _write_sdf(mol, sdf_path)
            pdbqt_path.write_text(_mol_to_pdbqt(mol), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - candidate-level QC.
            status = "failed"
            failure = str(exc)
        selected.append(
            {
                **trace,
                "candidate_id": candidate_id,
                "seed_pair_id": row.get("seed_pair_id", ""),
                "round": row.get("round", ""),
                "generation_backend": row.get("generation_backend", ""),
                "scoring_engine": row.get("scoring_engine", ""),
                "execution_mode": row.get("execution_mode", "unknown"),
                "fallback_reason": row.get("fallback_reason", ""),
                "candidate_smiles": smiles,
                "canonical_smiles": smiles,
                "qed": row.get("qed", ""),
                "parent_similarity": row.get("parent_similarity", ""),
                "fragment_score": row.get("fragment_score", ""),
                "candidate_sdf": str(sdf_path) if sdf_path.exists() else "",
                "candidate_pdbqt": str(pdbqt_path) if pdbqt_path.exists() else "",
                "vina_preparation_status": status,
                "failure_reason": failure,
            }
        )
    write_rows(out_csv, selected, VINA_SELECTION_FIELDS)
    return selected
