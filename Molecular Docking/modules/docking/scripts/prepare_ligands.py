from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from meeko import MoleculePreparation, PDBQTWriterLegacy
from rdkit import Chem
from rdkit.Chem import AllChem, SDWriter

from modules.docking.scripts.common import ensure_dir, slugify, write_rows
from modules.docking.scripts.ligand_state_contract import LigandPDBQTConversion, validate_ligand_ph
from modules.docking.scripts.standardize_ligands import choose_smiles, standardize_ligands
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from npflow_chemistry import apply_requested_ph


LIGAND_QC_FIELDS = [
    "ligand_id",
    "compound_name",
    "pubchem_cid",
    "canonical_smiles",
    "isomeric_smiles",
    "inchikey",
    "heavy_atom_count",
    "molecular_weight",
    "rotatable_bonds",
    "formal_charge",
    "sdf_source",
    "conformer_method",
    "optimization_method",
    "optimization_status",
    "pdbqt_status",
    "pdbqt_path",
    "ligand_state_mode",
    "tautomer_enumeration",
    "protomer_enumeration",
    "ph_requested",
    "ph_protonation_status",
    "ph_protonation_backend",
    "ph_protonation_fallback_reason",
    "failure_reason",
    "warning",
    "confidence_level",
]


def fetch_pubchem_3d_sdf(pubchem_cid: str, timeout: int = 20) -> str:
    import requests

    cid = str(pubchem_cid).strip()
    if not cid:
        return ""
    url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/{cid}/SDF"
    response = requests.get(
        url,
        params={"record_type": "3d"},
        timeout=timeout,
        headers={"User-Agent": "NPFlow-Docking/0.1"},
    )
    response.raise_for_status()
    text = response.text
    if "$$$$" not in text:
        return ""
    return text


def mol_from_pubchem_sdf(sdf_text: str) -> Chem.Mol | None:
    supplier = Chem.SDMolSupplier()
    supplier.SetData(sdf_text, removeHs=False)
    for mol in supplier:
        if mol is not None and mol.GetNumConformers() > 0:
            return mol
    return None


def embed_and_minimize(smiles: str, random_seed: int) -> tuple[Chem.Mol | None, str, str, str]:
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None, "none", "skipped", "failed"
    mol = Chem.AddHs(mol)
    params = AllChem.ETKDGv3() if hasattr(AllChem, "ETKDGv3") else AllChem.ETKDG()
    params.randomSeed = int(random_seed)
    embed_status = AllChem.EmbedMolecule(mol, params)
    if embed_status != 0:
        return None, "ETKDGv3" if hasattr(AllChem, "ETKDGv3") else "ETKDG", "skipped", "failed"

    if AllChem.MMFFHasAllMoleculeParams(mol):
        try:
            opt_status = AllChem.MMFFOptimizeMolecule(mol, mmffVariant="MMFF94")
            return mol, "ETKDGv3" if hasattr(AllChem, "ETKDGv3") else "ETKDG", "MMFF94", (
                "converged" if opt_status == 0 else "not_converged"
            )
        except Exception:
            pass

    try:
        opt_status = AllChem.UFFOptimizeMolecule(mol)
        return mol, "ETKDGv3" if hasattr(AllChem, "ETKDGv3") else "ETKDG", "UFF", (
            "converged" if opt_status == 0 else "not_converged"
        )
    except Exception:
        return mol, "ETKDGv3" if hasattr(AllChem, "ETKDGv3") else "ETKDG", "UFF", "failed"


def write_mol_sdf(path: Path, mol: Chem.Mol) -> None:
    writer = SDWriter(str(path))
    try:
        writer.write(mol)
    finally:
        writer.close()


def _openbabel_executable() -> str:
    found = shutil.which("obabel")
    if found:
        return found
    for name in ("obabel.exe", "obabel"):
        sibling = Path(sys.executable).with_name(name)
        if sibling.exists() and sibling.is_file():
            return str(sibling)
    return ""


def _apply_requested_ph(mol: Chem.Mol, ph: float | None) -> tuple[Chem.Mol, str, str, str, str]:
    return apply_requested_ph(mol, ph, obabel_bin=_openbabel_executable(), run_command=subprocess.run)


def mol_to_pdbqt(mol: Chem.Mol, ph: float | None = 7.4) -> LigandPDBQTConversion:
    requested_ph = validate_ligand_ph(ph)
    prepared_mol, protonation_status, backend, fallback_reason, state_mode = _apply_requested_ph(mol, requested_ph)
    try:
        setups = MoleculePreparation().prepare(prepared_mol)
        if not setups:
            raise ValueError("Meeko did not produce a molecule setup.")
        writer_result = PDBQTWriterLegacy.write_string(setups[0])
        if isinstance(writer_result, tuple):
            pdbqt_text = writer_result[0]
            success = writer_result[1] if len(writer_result) > 1 else True
            message = writer_result[2] if len(writer_result) > 2 else ""
            if not success:
                raise ValueError(str(message or "Meeko PDBQT writer failed."))
            text = str(pdbqt_text)
        else:
            text = str(writer_result)
        return LigandPDBQTConversion(
            pdbqt_text=text,
            pdbqt_status="success",
            failure_reason="",
            ph_requested=requested_ph,
            ph_protonation_status=protonation_status,
            ph_protonation_backend=backend,
            ph_protonation_fallback_reason=fallback_reason,
            ligand_state_mode=state_mode,
        )
    except Exception as exc:  # noqa: BLE001 - return the actual state even when Meeko fails.
        return LigandPDBQTConversion(
            pdbqt_text="",
            pdbqt_status="failed",
            failure_reason=str(exc).strip() or "meeko_conversion_failed",
            ph_requested=requested_ph,
            ph_protonation_status=protonation_status,
            ph_protonation_backend=backend,
            ph_protonation_fallback_reason=fallback_reason,
            ligand_state_mode=state_mode,
        )


def base_qc(row: dict[str, Any], ph: float | None = 7.4) -> dict[str, Any]:
    requested_ph = validate_ligand_ph(ph)
    return {
        "ligand_id": row.get("ligand_id", ""),
        "compound_name": row.get("compound_name", ""),
        "pubchem_cid": row.get("pubchem_cid", ""),
        "canonical_smiles": row.get("canonical_smiles", ""),
        "isomeric_smiles": row.get("isomeric_smiles", ""),
        "inchikey": row.get("inchikey", ""),
        "heavy_atom_count": row.get("heavy_atom_count", ""),
        "molecular_weight": row.get("molecular_weight", ""),
        "rotatable_bonds": row.get("rotatable_bonds", ""),
        "formal_charge": row.get("formal_charge", ""),
        "sdf_source": "failed",
        "conformer_method": "none",
        "optimization_method": "skipped",
        "optimization_status": "skipped",
        "pdbqt_status": "skipped",
        "pdbqt_path": "",
        "ligand_state_mode": "input_state_single",
        "tautomer_enumeration": False,
        "protomer_enumeration": False,
        "ph_requested": "" if requested_ph is None else requested_ph,
        "ph_protonation_status": "not_attempted",
        "ph_protonation_backend": "none",
        "ph_protonation_fallback_reason": "",
        "failure_reason": "",
        "warning": str(row.get("warning", "")),
        "confidence_level": "not_applicable",
    }


def prepare_ligands(
    input_csv: Path,
    *,
    out_dir: Path,
    fetch_pubchem: bool = True,
    random_seed: int = 61453,
    ph: float | None = 7.4,
) -> list[dict[str, Any]]:
    ph = validate_ligand_ph(ph)
    out_dir = ensure_dir(out_dir)
    raw_sdf_dir = ensure_dir(out_dir / "raw_sdf")
    pdbqt_dir = ensure_dir(out_dir / "pdbqt")
    identity_rows = standardize_ligands(input_csv, out_dir=out_dir, fetch_pubchem=fetch_pubchem)

    qc_rows: list[dict[str, Any]] = []
    combined_writer = SDWriter(str(out_dir / "ligands.sdf"))
    try:
        for row in identity_rows:
            qc = base_qc(row, ph=ph)
            eligibility = str(row.get("ligand_preparation_eligibility", ""))
            skip_reason = str(row.get("skip_reason", ""))
            if eligibility in {"skipped", "failed"}:
                qc["pdbqt_status"] = eligibility
                qc["failure_reason"] = skip_reason or "missing_structure"
                qc_rows.append(qc)
                continue

            ligand_id = slugify(str(row.get("ligand_id") or "ligand"))
            mol: Chem.Mol | None = None
            warnings = [w for w in str(qc.get("warning", "")).split(";") if w]

            if fetch_pubchem and row.get("pubchem_cid"):
                try:
                    sdf_text = fetch_pubchem_3d_sdf(str(row["pubchem_cid"]))
                    mol = mol_from_pubchem_sdf(sdf_text)
                    if mol is not None:
                        qc["sdf_source"] = "pubchem_3d"
                        qc["conformer_method"] = "pubchem_3d"
                        qc["optimization_method"] = "skipped"
                        qc["optimization_status"] = "skipped"
                    else:
                        warnings.append("no_pubchem_3d_sdf")
                except Exception:
                    warnings.append("cannot_fetch_pubchem")

            if mol is None:
                smiles = choose_smiles(row)
                if not smiles:
                    qc["pdbqt_status"] = "skipped"
                    qc["failure_reason"] = "missing_structure"
                    qc["warning"] = ";".join(dict.fromkeys(warnings))
                    qc_rows.append(qc)
                    continue
                mol, conformer_method, optimization_method, optimization_status = embed_and_minimize(
                    smiles,
                    random_seed=random_seed,
                )
                qc["conformer_method"] = conformer_method
                qc["optimization_method"] = optimization_method
                qc["optimization_status"] = optimization_status
                if mol is None:
                    qc["pdbqt_status"] = "failed"
                    qc["failure_reason"] = "cannot_embed_3d"
                    qc["warning"] = ";".join(dict.fromkeys(warnings))
                    qc_rows.append(qc)
                    continue
                if optimization_status == "failed":
                    qc["pdbqt_status"] = "failed"
                    qc["failure_reason"] = "minimization_failed"
                    qc["warning"] = ";".join(dict.fromkeys(warnings))
                    qc_rows.append(qc)
                    continue
                qc["sdf_source"] = "rdkit_generated"

            actual_key = Chem.MolToInchiKey(Chem.RemoveHs(mol))
            if row.get("structure_inchikey") and actual_key != row["structure_inchikey"]:
                qc["pdbqt_status"] = "skipped"
                qc["failure_reason"] = "prepared_sdf_identity_conflict"
                qc_rows.append(qc)
                continue
            sdf_path = raw_sdf_dir / f"{ligand_id}.sdf"
            pdbqt_path = pdbqt_dir / f"{ligand_id}.pdbqt"
            try:
                write_mol_sdf(sdf_path, mol)
                combined_writer.write(mol)
                conversion = mol_to_pdbqt(mol, ph=ph)
                qc.update(conversion.qc_fields())
                if conversion.ph_protonation_status == "applied":
                    warnings.append(f"ligand_ph{ph}_protonation_applied")
                elif conversion.ph_protonation_status == "failed_fallback_input_state":
                    warnings.append(
                        "ligand_ph_protonation_not_applied:"
                        + (conversion.ph_protonation_fallback_reason or "unknown_reason")
                    )
                if conversion.pdbqt_status != "success":
                    qc["failure_reason"] = conversion.failure_reason or "meeko_conversion_failed"
                    qc["confidence_level"] = "not_applicable"
                else:
                    pdbqt_path.write_text(conversion.pdbqt_text, encoding="utf-8")
                    qc["pdbqt_path"] = str(pdbqt_path)
                if conversion.pdbqt_status == "success" and conversion.ph_protonation_status == "failed_fallback_input_state":
                    qc["confidence_level"] = "low"
                elif conversion.pdbqt_status == "success" and any(
                    w in warnings for w in ["large_ligand_warning", "too_flexible_warning"]
                ):
                    qc["confidence_level"] = "low"
                elif conversion.pdbqt_status == "success" and qc["sdf_source"] == "pubchem_3d":
                    qc["confidence_level"] = "high"
                elif conversion.pdbqt_status == "success":
                    qc["confidence_level"] = "medium"
            except Exception as exc:
                qc["pdbqt_status"] = "failed"
                qc["failure_reason"] = str(exc).strip() or "meeko_conversion_failed"
                qc["confidence_level"] = "not_applicable"
            qc["warning"] = ";".join(dict.fromkeys(warnings))
            qc_rows.append(qc)
    finally:
        combined_writer.close()

    write_rows(out_dir / "ligand_preparation_qc.csv", qc_rows, LIGAND_QC_FIELDS)
    return qc_rows
