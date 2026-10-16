from __future__ import annotations

from pathlib import Path
from typing import Any

from rdkit import Chem

from .common import deterministic_seed, read_rows, write_rows
from .atom_mapping import TRACE_FIELDS, pose_to_parent


RAW_FIELDS = [
    "candidate_id",
    "seed_pair_id",
    "round",
    "parent_ligand_id",
    "attachment_id",
    "attachment_atom_index",
    "fragment_smiles",
    "fragment_rank",
    "fragment_score",
    "candidate_smiles",
    "generation_backend",
    "scoring_engine",
    "execution_mode",
    "fallback_reason",
    "status",
    "error_message",
] + TRACE_FIELDS


def _is_parent_attachable(atom: Chem.Atom) -> bool:
    if atom.GetAtomicNum() == 1:
        return False
    if atom.GetAtomicNum() not in {6, 7, 8, 16}:
        return False
    if atom.GetTotalNumHs() <= 0:
        return False
    if atom.GetFormalCharge() not in {-1, 0, 1}:
        return False
    return True


def _parent_attachable_atoms(mol: Chem.Mol, preferred_index: int | None) -> list[int]:
    candidates: list[int] = []
    if preferred_index is not None and 0 <= preferred_index < mol.GetNumAtoms():
        candidates.append(preferred_index)
    candidates.extend(atom.GetIdx() for atom in mol.GetAtoms())
    seen: set[int] = set()
    ordered: list[int] = []
    for idx in candidates:
        if idx in seen:
            continue
        seen.add(idx)
        atom = mol.GetAtomWithIdx(idx)
        if _is_parent_attachable(atom):
            ordered.append(idx)
    return ordered


def _fragment_atoms(frag: Chem.Mol) -> list[int]:
    atoms: list[int] = []
    for atom in frag.GetAtoms():
        if atom.GetAtomicNum() == 1:
            continue
        if atom.GetAtomicNum() in {6, 7, 8, 9, 16, 17, 35}:
            atoms.append(atom.GetIdx())
    return atoms


def attach_fragment_details(parent_smiles: str, atom_index: int | None, fragment_smiles: str, *, strict: bool = False) -> tuple[str, int, int]:
    parent = Chem.MolFromSmiles(parent_smiles)
    frag = Chem.MolFromSmiles(fragment_smiles)
    if parent is None:
        raise ValueError("invalid_parent_smiles")
    if frag is None:
        raise ValueError("invalid_fragment_smiles")
    parent_atoms = _parent_attachable_atoms(parent, atom_index)
    frag_atoms = _fragment_atoms(frag)
    if strict:
        parent_atoms = [idx for idx in parent_atoms if idx == atom_index]
        dummies = [atom for atom in frag.GetAtoms() if atom.GetAtomicNum() == 0]
        if dummies:
            if len(dummies) != 1 or dummies[0].GetDegree() != 1:
                raise ValueError("fragment_attachment_marker_ambiguous")
            frag_atoms = [dummies[0].GetNeighbors()[0].GetIdx()]
    if not parent_atoms or not frag_atoms:
        raise ValueError("no_attachable_atom")
    errors: list[str] = []
    for parent_atom in parent_atoms:
        for frag_atom in frag_atoms:
            combo = Chem.CombineMols(parent, frag)
            rw = Chem.RWMol(combo)
            if strict:
                for idx in (parent_atom, parent.GetNumAtoms() + frag_atom):
                    atom = rw.GetAtomWithIdx(idx)
                    if atom.GetNumExplicitHs() > 0:
                        atom.SetNumExplicitHs(atom.GetNumExplicitHs() - 1)
            rw.AddBond(parent_atom, parent.GetNumAtoms() + frag_atom, Chem.BondType.SINGLE)
            if strict:
                for idx in reversed(range(rw.GetNumAtoms())):
                    if rw.GetAtomWithIdx(idx).GetAtomicNum() == 0:
                        rw.RemoveAtom(idx)
            candidate = rw.GetMol()
            try:
                Chem.SanitizeMol(candidate)
                return Chem.MolToSmiles(candidate, canonical=True), parent_atom, frag_atom
            except Exception as exc:  # noqa: BLE001 - try next atom pair.
                errors.append(str(exc))
    raise ValueError(errors[0] if errors else "fragment_attachment_failed")


def attach_fragment(parent_smiles: str, atom_index: int | None, fragment_smiles: str) -> str:
    return attach_fragment_details(parent_smiles, atom_index, fragment_smiles)[0]


def assemble_candidates(
    *,
    seed: dict[str, Any],
    fragments_csv: Path,
    attachment_points_csv: Path,
    out_csv: Path,
) -> list[dict[str, Any]]:
    fragments = read_rows(fragments_csv)
    attachments = {row.get("attachment_id", ""): row for row in read_rows(attachment_points_csv)}
    parent_smiles = str(seed.get("smiles", ""))
    mapping, mapping_status, mapping_reason = pose_to_parent(seed)
    rows: list[dict[str, Any]] = []
    for index, fragment in enumerate(fragments, start=1):
        attachment = attachments.get(fragment.get("attachment_id", ""), {})
        atom_index_raw = attachment.get("atom_index", "")
        atom_index = int(float(atom_index_raw)) if str(atom_index_raw).strip() else None
        candidate_id = (
            f"cand_{deterministic_seed(str(seed.get('seed_pair_id')) + str(fragment.get('round')) + str(index)):010d}"
        )
        try:
            method = "legacy_candidate_extension_spatial_connection_unconfirmed"
            reason = mapping_reason
            if atom_index in mapping:
                try:
                    candidate_smiles, actual_parent, actual_fragment = attach_fragment_details(
                        parent_smiles, mapping[atom_index], str(fragment.get("fragment_smiles", "")), strict=True)
                    method = "mapped_parent_attachment_then_unconstrained_redock"
                except ValueError as exc:
                    reason = f"mapped_attachment_failed:{exc}"
            if method.startswith("legacy"):
                candidate_smiles, actual_parent, actual_fragment = attach_fragment_details(
                    parent_smiles, atom_index, str(fragment.get("fragment_smiles", "")))
            status = "success"
            error = ""
        except Exception as exc:  # noqa: BLE001 - candidate-level failure is part of QC.
            candidate_smiles = ""
            status = "failed"
            error = str(exc)
            actual_parent = actual_fragment = ""
        rows.append(
            {
                "candidate_id": candidate_id,
                "seed_pair_id": seed.get("seed_pair_id", ""),
                "round": fragment.get("round", ""),
                "parent_ligand_id": seed.get("ligand_id", ""),
                "attachment_id": fragment.get("attachment_id", ""),
                "attachment_atom_index": "" if atom_index is None else atom_index,
                "attachment_pose_atom_index": "" if atom_index is None else atom_index,
                "actual_parent_atom_index": actual_parent, "actual_fragment_atom_index": actual_fragment,
                "assembly_method": method, "atom_mapping_status": mapping_status,
                "mapping_reason": reason,
                "fragment_smiles": fragment.get("fragment_smiles", ""),
                "fragment_rank": fragment.get("fragment_rank", ""),
                "fragment_score": fragment.get("fragment_score", ""),
                "candidate_smiles": candidate_smiles,
                "generation_backend": fragment.get("generation_backend") or fragment.get("backend", ""),
                "scoring_engine": "",
                "execution_mode": fragment.get("execution_mode", "unknown"),
                "fallback_reason": fragment.get("fallback_reason", ""),
                "status": status,
                "error_message": error,
            }
        )
    write_rows(out_csv, rows, RAW_FIELDS)
    return rows
