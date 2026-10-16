from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from rdkit import Chem
from rdkit.Chem import AllChem, SDWriter

from .common import copy_if_exists, deterministic_seed, ensure_dir, find_executable, read_json, resolve_path, slugify, write_json
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from npflow_runtime import remaining_timeout


def _sdf_has_mol(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        supplier = Chem.SDMolSupplier(str(path), removeHs=False)
        return any(mol is not None for mol in supplier)
    except Exception:
        return False


def _write_smiles_sdf(smiles: str, path: Path, seed_text: str) -> bool:
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return False
    mol = Chem.AddHs(mol)
    params = AllChem.ETKDGv3() if hasattr(AllChem, "ETKDGv3") else AllChem.ETKDG()
    params.randomSeed = deterministic_seed(seed_text)
    if AllChem.EmbedMolecule(mol, params) != 0:
        return False
    try:
        if AllChem.MMFFHasAllMoleculeParams(mol):
            AllChem.MMFFOptimizeMolecule(mol, mmffVariant="MMFF94")
        else:
            AllChem.UFFOptimizeMolecule(mol)
    except Exception:
        pass
    ensure_dir(path.parent)
    writer = SDWriter(str(path))
    try:
        writer.write(mol)
    finally:
        writer.close()
    return True


def _convert_pose_to_sdf(pdbqt_path: Path, sdf_path: Path) -> tuple[bool, str]:
    obabel = find_executable("obabel")
    if not obabel or not pdbqt_path.exists() or not pdbqt_path.is_file():
        return False, "obabel_or_pose_missing"
    result = subprocess.run(
        [obabel, "-ipdbqt", str(pdbqt_path), "-osdf", "-O", str(sdf_path)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=remaining_timeout(120),
    )
    sdf_path.with_suffix(".conversion.log").write_text(result.stdout or "", encoding="utf-8")
    if result.returncode == 0 and _sdf_has_mol(sdf_path):
        return True, "obabel_pose_pdbqt_to_sdf"
    return False, f"obabel_failed: {result.stdout[-500:]}"


def prepare_complex(seed: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    pair_dir = ensure_dir(out_dir / f"pair_{slugify(seed.get('target_id'))}_{slugify(seed.get('ligand_id'))}")
    input_dir = ensure_dir(pair_dir / "input")
    dock_dir = Path(str(seed.get("docking_run_dir", "")))
    copied: dict[str, str] = {}
    warnings: list[str] = []

    file_map = {
        "receptor_pdb": ("receptor.pdb", seed.get("receptor_pdb")),
        "receptor_pdbqt": ("receptor.pdbqt", seed.get("receptor_pdbqt")),
        "parent_ligand_pdbqt": ("parent_ligand.pdbqt", seed.get("parent_ligand_pdbqt")),
        "parent_best_pose_pdbqt": ("parent_best_pose.pdbqt", seed.get("best_pose_pdbqt")),
    }
    for key, (name, raw) in file_map.items():
        src = resolve_path(raw, dock_dir=dock_dir)
        dst = input_dir / name
        if copy_if_exists(src, dst):
            copied[key] = str(dst)
        else:
            copied[key] = ""
            warnings.append(f"missing_{key}")

    box_src = resolve_path(seed.get("box_json"), dock_dir=dock_dir)
    box_dst = input_dir / "pocket_box.json"
    if copy_if_exists(box_src, box_dst):
        copied["pocket_box_json"] = str(box_dst)
    else:
        write_json(box_dst, {})
        copied["pocket_box_json"] = str(box_dst)
        warnings.append("missing_box_json")

    parent_sdf = input_dir / "parent_ligand.sdf"
    parent_best_pose_sdf = input_dir / "parent_best_pose.sdf"
    pose_sdf_source = ""
    source_sdf = resolve_path(seed.get("best_pose_sdf"), dock_dir=dock_dir)
    if copy_if_exists(source_sdf, parent_sdf) and _sdf_has_mol(parent_sdf):
        copied["parent_ligand_sdf"] = str(parent_sdf)
    else:
        copied["parent_ligand_sdf"] = ""
        warnings.append("missing_parent_ligand_sdf")

    pose_pdbqt = Path(copied.get("parent_best_pose_pdbqt") or "")
    converted, reason = _convert_pose_to_sdf(pose_pdbqt, parent_best_pose_sdf)
    if converted:
        pose_sdf_source = reason
    elif copied.get("parent_ligand_sdf") and copy_if_exists(parent_sdf, parent_best_pose_sdf):
        pose_sdf_source = "fallback_original_ligand_sdf_not_docking_pose"
        warnings.append("best_pose_sdf_fallback_to_original_ligand_sdf")
    elif _write_smiles_sdf(str(seed.get("smiles", "")), parent_best_pose_sdf, str(seed.get("seed_pair_id", ""))):
        pose_sdf_source = "fallback_embedded_from_smiles_not_docking_pose"
        warnings.append("best_pose_sdf_fallback_to_smiles_embedding")
    else:
        pose_sdf_source = reason
        warnings.append("cannot_prepare_parent_best_pose_sdf")
    copied["parent_best_pose_sdf"] = str(parent_best_pose_sdf) if parent_best_pose_sdf.exists() else ""

    metadata = {
        **seed,
        "prepared_input_dir": str(input_dir),
        "pair_dir": str(pair_dir),
        "pose_sdf_source": pose_sdf_source,
        "warnings": warnings,
        **copied,
    }
    write_json(input_dir / "parent_metadata.json", metadata)
    return metadata
