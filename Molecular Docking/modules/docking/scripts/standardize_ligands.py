from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from rdkit import Chem
from rdkit.Chem import Descriptors, rdMolDescriptors
from rdkit import RDLogger

from modules.docking.scripts.common import ensure_dir, slugify, write_rows


IDENTITY_FIELDS = [
    "ligand_id",
    "compound_name",
    "pubchem_cid",
    "canonical_smiles",
    "isomeric_smiles",
    "inchikey",
    "molecular_weight",
    "heavy_atom_count",
    "rotatable_bonds",
    "formal_charge",
    "has_undefined_stereo",
    "has_multiple_fragments",
    "unsupported_element",
    "ligand_preparation_eligibility",
    "warning",
    "skip_reason",
    "supplied_inchikey", "structure_inchikey", "identity_status", "identity_check_detail",
]

INPUT_FIELDS = [
    "ligand_id",
    "compound_name",
    "pubchem_cid",
    "canonical_smiles",
    "isomeric_smiles",
    "inchikey",
]

ALLOWED_ATOMIC_NUMBERS = {1, 5, 6, 7, 8, 9, 12, 14, 15, 16, 17, 20, 25, 26, 27, 29, 30, 34, 35, 53}
# H  B  C  N  O  F  Mg Si P  S  Cl Ca Mn Fe Co Cu Zn Se Br I
RDLogger.DisableLog("rdApp.error")


def read_ligand_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError(f"Ligand CSV has no header: {path}")
        missing = [field for field in INPUT_FIELDS if field not in reader.fieldnames]
        if missing:
            raise ValueError(f"Ligand CSV missing required fields: {', '.join(missing)}")
        return [{key: (value or "").strip() for key, value in row.items()} for row in reader]


def fetch_pubchem_properties(pubchem_cid: str, timeout: int = 10) -> dict[str, str]:
    import requests

    cid = str(pubchem_cid).strip()
    if not cid:
        return {}
    url = (
        "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/"
        f"{cid}/property/CanonicalSMILES,IsomericSMILES,InChIKey/JSON"
    )
    response = requests.get(url, timeout=timeout, headers={"User-Agent": "NPFlow-Docking/0.1"})
    response.raise_for_status()
    data = response.json()
    props = data.get("PropertyTable", {}).get("Properties", [])
    if not props:
        return {}
    first = props[0]
    return {
        "canonical_smiles": str(first.get("CanonicalSMILES") or first.get("ConnectivitySMILES") or ""),
        "isomeric_smiles": str(first.get("IsomericSMILES") or first.get("SMILES") or ""),
        "inchikey": str(first.get("InChIKey") or ""),
    }


def choose_smiles(row: dict[str, Any]) -> str:
    return str(row.get("isomeric_smiles") or row.get("canonical_smiles") or "").strip()


def has_undefined_stereo(mol: Chem.Mol) -> bool:
    centers = Chem.FindMolChiralCenters(mol, includeUnassigned=True)
    return any(label == "?" for _, label in centers)


def unsupported_element(mol: Chem.Mol) -> str:
    unsupported = sorted(
        {
            atom.GetSymbol()
            for atom in mol.GetAtoms()
            if atom.GetAtomicNum() not in ALLOWED_ATOMIC_NUMBERS
        }
    )
    return ";".join(unsupported)


def standardize_ligand_row(row: dict[str, str], *, fetch_pubchem: bool = True) -> dict[str, Any]:
    normalized = {
        "ligand_id": row.get("ligand_id", "").strip(),
        "compound_name": row.get("compound_name", "").strip(),
        "pubchem_cid": row.get("pubchem_cid", "").strip(),
        "canonical_smiles": row.get("canonical_smiles", "").strip(),
        "isomeric_smiles": row.get("isomeric_smiles", "").strip(),
        "inchikey": row.get("inchikey", "").strip(),
        "molecular_weight": "",
        "heavy_atom_count": "",
        "rotatable_bonds": "",
        "formal_charge": "",
        "has_undefined_stereo": "",
        "has_multiple_fragments": "",
        "unsupported_element": "",
        "ligand_preparation_eligibility": "eligible",
        "warning": "",
        "skip_reason": "",
    }
    if not normalized["ligand_id"]:
        normalized["ligand_id"] = slugify(
            normalized["inchikey"] or normalized["pubchem_cid"] or normalized["compound_name"] or "ligand"
        )

    warnings: list[str] = []
    props = {}
    if normalized["pubchem_cid"] and fetch_pubchem:
        try:
            props = fetch_pubchem_properties(normalized["pubchem_cid"])
            for key in ["canonical_smiles", "isomeric_smiles", "inchikey"]:
                if props.get(key) and not normalized.get(key):
                    normalized[key] = props[key]
        except Exception:
            warnings.append("cannot_fetch_pubchem")

    smiles = choose_smiles(normalized)
    if not smiles:
        normalized["ligand_preparation_eligibility"] = "skipped"
        normalized["skip_reason"] = "missing_structure"
        normalized["warning"] = ";".join(warnings)
        return normalized

    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        normalized["ligand_preparation_eligibility"] = "failed"
        normalized["skip_reason"] = "invalid_smiles"
        normalized["warning"] = ";".join(warnings)
        return normalized

    actual_key = Chem.MolToInchiKey(mol)
    supplied_key = normalized["inchikey"]
    conflict = []
    if supplied_key and actual_key and supplied_key != actual_key:
        conflict.append("supplied_inchikey_structure_conflict")
    remote_key = props.get("inchikey") or ""
    if remote_key and actual_key and remote_key != actual_key:
        conflict.append("pubchem_cid_structure_conflict")
    # A canonical (non-isomeric) SMILES can legitimately omit stereo. Compare connectivity here.
    for field in ("canonical_smiles", "isomeric_smiles"):
        other = Chem.MolFromSmiles(str(row.get(field) or "")) if row.get(field) else None
        if other is not None and actual_key and Chem.MolToInchiKey(other).split("-")[0] != actual_key.split("-")[0]:
            conflict.append(field + "_connectivity_conflict")
    normalized.update({"supplied_inchikey": supplied_key, "structure_inchikey": actual_key,
        "inchikey": actual_key, "identity_status": "conflict" if conflict else (
            "verified_against_cid" if remote_key else "structure_derived_cid_unverified"),
        "identity_check_detail": ";".join(conflict)})
    fragments = Chem.GetMolFrags(mol)
    unsupported = unsupported_element(mol)
    heavy_atoms = int(mol.GetNumHeavyAtoms())
    rotatable_bonds = int(rdMolDescriptors.CalcNumRotatableBonds(mol))
    normalized.update(
        {
            "molecular_weight": round(float(Descriptors.MolWt(mol)), 4),
            "heavy_atom_count": heavy_atoms,
            "rotatable_bonds": rotatable_bonds,
            "formal_charge": int(Chem.GetFormalCharge(mol)),
            "has_undefined_stereo": has_undefined_stereo(mol),
            "has_multiple_fragments": len(fragments) > 1,
            "unsupported_element": unsupported,
        }
    )

    if len(fragments) > 1:
        normalized["ligand_preparation_eligibility"] = "skipped"
        normalized["skip_reason"] = "multiple_fragments"
    elif unsupported:
        normalized["ligand_preparation_eligibility"] = "skipped"
        normalized["skip_reason"] = "unsupported_element"
    elif heavy_atoms > 80:
        normalized["ligand_preparation_eligibility"] = "skipped"
        normalized["skip_reason"] = "too_large_for_mvp"
    elif conflict:
        normalized["ligand_preparation_eligibility"] = "skipped"
        normalized["skip_reason"] = ";".join(conflict)
    else:
        if heavy_atoms > 50:
            warnings.append("large_ligand_warning")
        if rotatable_bonds > 25:
            warnings.append("too_flexible_warning")
        if warnings:
            normalized["ligand_preparation_eligibility"] = "warning"

    normalized["warning"] = ";".join(dict.fromkeys(warnings))
    return normalized


def standardize_ligands(
    input_csv: Path,
    *,
    out_dir: Path | None = None,
    fetch_pubchem: bool = True,
) -> list[dict[str, Any]]:
    rows = [standardize_ligand_row(row, fetch_pubchem=fetch_pubchem) for row in read_ligand_csv(input_csv)]
    if out_dir is not None:
        ensure_dir(out_dir)
        write_rows(out_dir / "ligand_identity_map.csv", rows, IDENTITY_FIELDS)
    return rows
