from __future__ import annotations

from pathlib import Path
from typing import Any

from rdkit import Chem, DataStructs
from rdkit.Chem import Crippen, Descriptors, QED, rdFingerprintGenerator, rdMolDescriptors
from rdkit.Chem.FilterCatalog import FilterCatalog, FilterCatalogParams

from .common import read_rows, to_float, write_rows
from .atom_mapping import TRACE_FIELDS


FILTERED_FIELDS = [
    "candidate_id",
    "seed_pair_id",
    "round",
    "generation_backend",
    "scoring_engine",
    "execution_mode",
    "fallback_reason",
    "canonical_smiles",
    "mol_weight",
    "logp",
    "qed",
    "tpsa",
    "hbd",
    "hba",
    "rotatable_bonds",
    "heavy_atoms",
    "parent_similarity",
    "heavy_atom_delta",
    "pains_flag",
    "fragment_smiles",
    "fragment_rank",
    "fragment_score",
    "filter_status",
    "filter_reason",
] + TRACE_FIELDS


def _pains_catalog() -> FilterCatalog:
    params = FilterCatalogParams()
    params.AddCatalog(FilterCatalogParams.FilterCatalogs.PAINS)
    return FilterCatalog(params)


def _strip_dummy_atoms(mol: Chem.Mol) -> Chem.Mol | None:
    """Remove dummy atoms (``*``, atomic num 0) left by DeepFrag2 attachment
    point markers.  Returns the cleaned molecule, or ``None`` if sanitization
    fails after removal.
    """
    has_dummy = any(a.GetAtomicNum() == 0 for a in mol.GetAtoms())
    if not has_dummy:
        return mol
    dummy_q = Chem.MolFromSmiles("*")
    cleaned = Chem.DeleteSubstructs(mol, dummy_q)
    if cleaned.GetNumAtoms() == 0:
        return None
    try:
        Chem.SanitizeMol(cleaned)
    except Exception:
        return None
    return cleaned


def _fingerprint(mol: Chem.Mol):
    generator = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048)
    return generator.GetFingerprint(mol)


def sanitize_candidates(
    *,
    seed: dict[str, Any],
    generated_raw_csv: Path,
    out_csv: Path,
    qed_min: float = 0.30,
    max_mw: float = 650.0,
    min_parent_similarity: float = 0.35,
    max_heavy_atom_delta: int = 8,
    reject_pains: bool = True,
    deduplicate: bool = True,
) -> list[dict[str, Any]]:
    raw_rows = read_rows(generated_raw_csv)
    parent = Chem.MolFromSmiles(str(seed.get("smiles", "")))
    parent_fp = _fingerprint(parent) if parent is not None else None
    parent_heavy = parent.GetNumHeavyAtoms() if parent is not None else 0
    catalog = _pains_catalog() if reject_pains else None
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        reasons: list[str] = []
        smiles = raw.get("candidate_smiles", "")
        mol = Chem.MolFromSmiles(smiles) if raw.get("status") == "success" else None
        if mol is None:
            reasons.append(raw.get("error_message") or "invalid_smiles")
            rows.append(
                {
                    "candidate_id": raw.get("candidate_id", ""),
                    "seed_pair_id": raw.get("seed_pair_id", ""),
                    "round": raw.get("round", ""),
                    "generation_backend": raw.get("generation_backend", ""),
                    "scoring_engine": raw.get("scoring_engine", ""),
                    "execution_mode": raw.get("execution_mode", "unknown"),
                    "fallback_reason": raw.get("fallback_reason", ""),
                    "fragment_smiles": raw.get("fragment_smiles", ""),
                    "fragment_rank": raw.get("fragment_rank", ""),
                    "fragment_score": raw.get("fragment_score", ""),
                    "filter_status": "fail",
                    "filter_reason": ";".join(reasons),
                }
            )
            continue
        # Strip dummy atoms (*) left by DeepFrag2 attachment-point markers
        # so that downstream Meeko PDBQT conversion succeeds.
        mol = _strip_dummy_atoms(mol)
        if mol is None:
            reasons.append("dummy_atom_cleanup_failed")
            rows.append(
                {
                    "candidate_id": raw.get("candidate_id", ""),
                    "seed_pair_id": raw.get("seed_pair_id", ""),
                    "round": raw.get("round", ""),
                    "generation_backend": raw.get("generation_backend", ""),
                    "scoring_engine": raw.get("scoring_engine", ""),
                    "execution_mode": raw.get("execution_mode", "unknown"),
                    "fallback_reason": raw.get("fallback_reason", ""),
                    "fragment_smiles": raw.get("fragment_smiles", ""),
                    "fragment_rank": raw.get("fragment_rank", ""),
                    "fragment_score": raw.get("fragment_score", ""),
                    "filter_status": "fail",
                    "filter_reason": ";".join(reasons),
                }
            )
            continue
        canonical = Chem.MolToSmiles(mol, canonical=True)
        if deduplicate and canonical in seen:
            reasons.append("duplicate")
        seen.add(canonical)
        mw = float(Descriptors.MolWt(mol))
        qed = float(QED.qed(mol))
        logp = float(Crippen.MolLogP(mol))
        tpsa = float(rdMolDescriptors.CalcTPSA(mol))
        hbd = int(rdMolDescriptors.CalcNumHBD(mol))
        hba = int(rdMolDescriptors.CalcNumHBA(mol))
        rot = int(rdMolDescriptors.CalcNumRotatableBonds(mol))
        heavy = int(mol.GetNumHeavyAtoms())
        heavy_delta = heavy - parent_heavy if parent_heavy else 0
        similarity = 0.0
        if parent_fp is not None:
            similarity = float(DataStructs.TanimotoSimilarity(parent_fp, _fingerprint(mol)))
        pains = bool(catalog.HasMatch(mol)) if catalog is not None else False
        if qed < qed_min:
            reasons.append(f"qed_below_{qed_min}")
        if mw > max_mw:
            reasons.append(f"mw_above_{max_mw}")
        if similarity < min_parent_similarity:
            reasons.append(f"parent_similarity_below_{min_parent_similarity}")
        if heavy_delta > max_heavy_atom_delta:
            reasons.append(f"heavy_atom_delta_above_{max_heavy_atom_delta}")
        if reject_pains and pains:
            reasons.append("pains_match")
        rows.append(
            {
                "candidate_id": raw.get("candidate_id", ""),
                "seed_pair_id": raw.get("seed_pair_id", ""),
                "round": raw.get("round", ""),
                "generation_backend": raw.get("generation_backend", ""),
                "scoring_engine": raw.get("scoring_engine", ""),
                "execution_mode": raw.get("execution_mode", "unknown"),
                "fallback_reason": raw.get("fallback_reason", ""),
                "canonical_smiles": canonical,
                "mol_weight": round(mw, 4),
                "logp": round(logp, 4),
                "qed": round(qed, 6),
                "tpsa": round(tpsa, 4),
                "hbd": hbd,
                "hba": hba,
                "rotatable_bonds": rot,
                "heavy_atoms": heavy,
                "parent_similarity": round(similarity, 6),
                "heavy_atom_delta": heavy_delta,
                "pains_flag": pains,
                "fragment_smiles": raw.get("fragment_smiles", ""),
                "fragment_rank": raw.get("fragment_rank", ""),
                "fragment_score": to_float(raw.get("fragment_score"), 0.0),
                "filter_status": "fail" if reasons else "pass",
                "filter_reason": ";".join(reasons),
            }
        )
    raw_by_id = {row.get("candidate_id"): row for row in raw_rows}
    for row in rows:
        row.update({key: raw_by_id.get(row.get("candidate_id"), {}).get(key, "") for key in TRACE_FIELDS})
        if row.get("canonical_smiles"):
            row["candidate_inchikey"] = Chem.MolToInchiKey(Chem.MolFromSmiles(row["canonical_smiles"]))
    write_rows(out_csv, rows, FILTERED_FIELDS)
    return rows
