from __future__ import annotations

import csv
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from Bio.PDB import PDBIO, PDBParser, Select

from modules.docking.scripts.common import ensure_dir, slugify, write_rows
from modules.docking.scripts.receptor_identity import IDENTITY_FIELDS, protein_signature, prepared_signature
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from npflow_runtime import remaining_timeout


RECEPTOR_FIELDS = [
    "target_gene",
    "pdb_id",
    "chain_id",
    "input_structure_file",
    "cleaned_receptor_pdb",
    "protonated_receptor_pqr",
    "prepared_receptor_pdbqt",
    "removed_waters_count",
    "removed_water_residue_names",
    "removed_solvents_buffers_salts",
    "removed_hetero_count",
    "kept_cofactors",
    "kept_metals",
    "unknown_hetatm_warning",
    "box_defining_ligands_detected",
    "box_defining_ligands_selected",
    "box_defining_ligands_removed_from_receptor",
    "reference_ligand_files",
    "cocrystal_ligand_saved",
    "cocrystal_ligand_file",
    "missing_residue_count",
    "missing_atom_count",
    "missing_residue_ratio",
    "protonation_method",
    "protonation_tool",
    "protonation_pH",
    "protonation_forcefield",
    "protonation_status",
    "protonation_warning",
    "pdb2pqr_command",
    "pdb2pqr_stdout_log",
    "pdb2pqr_stderr_log",
    "pdb2pqr_exit_code",
    "preparation_tool",
    "preparation_status",
    "failure_reason",
    "warning",
    "confidence_level",
    "receptor_prep_backend_requested",
    "backend_attempt_order",
    "backend_used",
    "meeko_attempted",
    "meeko_success",
    "meeko_command",
    "meeko_stdout_log",
    "meeko_stderr_log",
    "meeko_exit_code",
    "autodocktools_attempted",
    "autodocktools_success",
    "autodocktools_command",
    "autodocktools_stdout_log",
    "autodocktools_stderr_log",
    "autodocktools_exit_code",
    "fallback_attempted",
    "fallback_tool",
    "fallback_command",
    "fallback_stdout_log",
    "fallback_stderr_log",
    "fallback_exit_code",
    "pdbqt_file_size_bytes",
    "pdbqt_atom_count",
    "pdbqt_atom_type_missing_count",
    "pdbqt_atom_type_missing_fraction",
    "charge_zero_count",
    "charge_zero_fraction",
    "charge_sanity_status",
    "atom_type_sanity_status",
    "pdbqt_sanity_status",
    "usable_for_scientific_ranking",
] + IDENTITY_FIELDS

INPUT_FIELDS = [
    "target_gene",
    "pdb_id",
    "chain_id",
    "structure_source",
    "local_structure_file",
    "cocrystal_ligand_id",
    "notes",
]

WATER_NAMES = {"HOH", "WAT", "H2O", "DOD"}
SOLVENT_BUFFER_SALT_NAMES = {
    "SO4",
    "PO4",
    "GOL",
    "EDO",
    "PEG",
    "DMS",
    "DMSO",
    "ACT",
    "ACY",
    "TRS",
    "MES",
    "HEPES",
    "CL",
    "NA",
    "K",
    "NO3",
    "FMT",
    "IPA",
    "MPD",
}
METAL_NAMES = {"CA", "MG", "MN", "ZN", "FE", "CU", "NA", "K", "CO", "NI", "CD", "HG"}
COFACTOR_NAMES = {"HEM", "COH", "FAD", "FMN", "NAD", "NAP", "NDP", "PLP", "COA", "SAM", "SAH", "ATP", "ADP"}
BACKEND_CHOICES = {"auto", "meeko", "autodocktools", "openbabel"}
DEFAULT_PROTONATION_PH = 7.4
DEFAULT_PROTONATION_FORCEFIELD = "AMBER"


class ChainProteinSelect(Select):
    def __init__(self, chain_id: str):
        self.chain_id = chain_id

    def accept_chain(self, chain) -> bool:
        return chain.id == self.chain_id

    def accept_residue(self, residue) -> bool:
        return residue.id[0] == " "


class CleanedReceptorSelect(Select):
    def __init__(self, chain_id: str, keep_hetero_ids: set[tuple[str, int, str]]):
        self.chain_id = chain_id
        self.keep_hetero_ids = keep_hetero_ids

    def accept_chain(self, chain) -> bool:
        return chain.id == self.chain_id

    def accept_residue(self, residue) -> bool:
        if residue.id[0] == " ":
            return True
        return residue.id in self.keep_hetero_ids


class SingleResidueSelect(Select):
    def __init__(self, chain_id: str, residue):
        self.chain_id = chain_id
        self.residue_id = residue.id

    def accept_chain(self, chain) -> bool:
        return chain.id == self.chain_id

    def accept_residue(self, residue) -> bool:
        return residue.id == self.residue_id


def read_receptor_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError(f"Receptor CSV has no header: {path}")
        missing = [field for field in INPUT_FIELDS if field not in reader.fieldnames]
        if missing:
            raise ValueError(f"Receptor CSV missing required fields: {', '.join(missing)}")
        return [{key: (value or "").strip() for key, value in row.items()} for row in reader]


def base_qc(row: dict[str, str], input_file: Path | None = None, backend_requested: str = "auto", ph: float = 7.4) -> dict[str, Any]:
    return {
        "target_gene": row.get("target_gene", ""),
        "pdb_id": row.get("pdb_id", ""),
        "chain_id": row.get("chain_id", ""),
        "input_structure_file": str(input_file or row.get("local_structure_file", "")),
        "cleaned_receptor_pdb": "",
        "protonated_receptor_pqr": "",
        "prepared_receptor_pdbqt": "",
        "removed_waters_count": 0,
        "removed_water_residue_names": "",
        "removed_solvents_buffers_salts": "",
        "removed_hetero_count": 0,
        "kept_cofactors": "",
        "kept_metals": "",
        "unknown_hetatm_warning": "",
        "box_defining_ligands_detected": "",
        "box_defining_ligands_selected": "",
        "box_defining_ligands_removed_from_receptor": "",
        "reference_ligand_files": "",
        "cocrystal_ligand_saved": False,
        "cocrystal_ligand_file": "",
        "missing_residue_count": 0,
        "missing_atom_count": 0,
        "missing_residue_ratio": 0.0,
        "protonation_method": "pdb2pqr_propka",
        "protonation_tool": "pdb2pqr_propka",
        "protonation_pH": ph,
        "protonation_forcefield": DEFAULT_PROTONATION_FORCEFIELD,
        "protonation_status": "not_run",
        "protonation_warning": "",
        "pdb2pqr_command": "",
        "pdb2pqr_stdout_log": "",
        "pdb2pqr_stderr_log": "",
        "pdb2pqr_exit_code": "",
        "preparation_tool": "",
        "preparation_status": "failed",
        "failure_reason": "",
        "warning": "",
        "confidence_level": "not_applicable",
        "receptor_prep_backend_requested": backend_requested,
        "backend_attempt_order": "",
        "backend_used": "",
        "meeko_attempted": False,
        "meeko_success": False,
        "meeko_command": "",
        "meeko_stdout_log": "",
        "meeko_stderr_log": "",
        "meeko_exit_code": "",
        "autodocktools_attempted": False,
        "autodocktools_success": False,
        "autodocktools_command": "",
        "autodocktools_stdout_log": "",
        "autodocktools_stderr_log": "",
        "autodocktools_exit_code": "",
        "fallback_attempted": False,
        "fallback_tool": "",
        "fallback_command": "",
        "fallback_stdout_log": "",
        "fallback_stderr_log": "",
        "fallback_exit_code": "",
        "pdbqt_file_size_bytes": "",
        "pdbqt_atom_count": "",
        "pdbqt_atom_type_missing_count": "",
        "pdbqt_atom_type_missing_fraction": "",
        "charge_zero_count": "",
        "charge_zero_fraction": "",
        "charge_sanity_status": "not_applicable",
        "atom_type_sanity_status": "not_applicable",
        "pdbqt_sanity_status": "not_applicable",
        "usable_for_scientific_ranking": False,
    }


def resolve_structure_path(row: dict[str, str], csv_dir: Path) -> Path:
    local = Path(row.get("local_structure_file", ""))
    if local.is_absolute():
        return local
    return (csv_dir / local).resolve()


def get_mk_prepare_receptor() -> str | None:
    sibling = Path(sys.executable).with_name("mk_prepare_receptor.py")
    if sibling.exists():
        return str(sibling)
    return shutil.which("mk_prepare_receptor.py")


def get_prepare_receptor4() -> str | None:
    sibling = Path(sys.executable).with_name("prepare_receptor4.py")
    if sibling.exists():
        return str(sibling)
    return shutil.which("prepare_receptor4.py")


def get_pdb2pqr() -> str | None:
    sibling = Path(sys.executable).with_name("pdb2pqr")
    if sibling.exists():
        return str(sibling)
    return shutil.which("pdb2pqr")


def get_obabel() -> str | None:
    return shutil.which("obabel") or ("/opt/homebrew/bin/obabel" if Path("/opt/homebrew/bin/obabel").exists() else None)


def count_removed(chain) -> tuple[int, int, str]:
    waters = 0
    hetero = 0
    metals: list[str] = []
    for residue in chain:
        resname = residue.get_resname().strip()
        if residue.id[0] == "W" or resname in WATER_NAMES:
            waters += len(list(residue.get_atoms()))
        elif residue.id[0] != " ":
            hetero += len(list(residue.get_atoms()))
            if resname in METAL_NAMES:
                metals.append(resname)
    return waters, hetero, ";".join(sorted(set(metals)))


def residue_key(residue) -> str:
    return f"{residue.get_resname().strip()}:{residue.parent.id}:{residue.id[1]}{residue.id[2].strip()}"


def heavy_atom_count(residue) -> int:
    count = 0
    for atom in residue.get_atoms():
        element = (getattr(atom, "element", "") or atom.get_name()[0]).strip().upper()
        if element != "H":
            count += 1
    return count


def classify_hetero_residue(residue) -> str:
    resname = residue.get_resname().strip().upper()
    if residue.id[0] == "W" or resname in WATER_NAMES:
        return "water"
    if resname in METAL_NAMES and heavy_atom_count(residue) <= 2:
        return "metal"
    if resname in COFACTOR_NAMES:
        return "cofactor"
    if resname in SOLVENT_BUFFER_SALT_NAMES:
        return "solvent_buffer_salt"
    heavy = heavy_atom_count(residue)
    elements = {
        (getattr(atom, "element", "") or atom.get_name()[0]).strip().upper()
        for atom in residue.get_atoms()
    }
    if "C" in elements and 6 <= heavy <= 80:
        return "reference_pocket_ligand"
    return "unknown"


def analyze_heteroatoms(chain, requested_ligand_id: str = "") -> dict[str, Any]:
    classes: dict[str, list[Any]] = {
        "water": [],
        "solvent_buffer_salt": [],
        "metal": [],
        "cofactor": [],
        "reference_pocket_ligand": [],
        "unknown": [],
    }
    for residue in chain:
        if residue.id[0] == " ":
            continue
        classification = classify_hetero_residue(residue)
        classes[classification].append(residue)

    candidates = classes["reference_pocket_ligand"]
    requested = requested_ligand_id.strip().upper()
    if requested:
        selected = [residue for residue in candidates if residue.get_resname().strip().upper() == requested]
    else:
        selected = candidates
    if not selected and requested:
        selected = [residue for residue in chain if residue.id[0] != " " and residue.get_resname().strip().upper() == requested]
    keep_hetero_ids = {residue.id for residue in classes["metal"] + classes["cofactor"]}
    return {
        "classes": classes,
        "selected_reference_ligands": selected,
        "keep_hetero_ids": keep_hetero_ids,
    }


def save_cocrystal_ligand(structure, chain_id: str, ligand_id: str, output_file: Path) -> bool:
    if not ligand_id:
        return False
    chain = structure[0][chain_id]
    target_residue = None
    for residue in chain:
        if residue.get_resname().strip() == ligand_id and residue.id[0] != " ":
            target_residue = residue
            break
    if target_residue is None:
        return False
    io = PDBIO()
    io.set_structure(structure)
    ensure_dir(output_file.parent)
    io.save(str(output_file), SingleResidueSelect(chain_id, target_residue))
    return output_file.exists() and output_file.stat().st_size > 0


def save_reference_ligands(structure, chain_id: str, residues: list[Any], out_dir: Path, prefix: str) -> list[str]:
    files: list[str] = []
    ensure_dir(out_dir)
    io = PDBIO()
    io.set_structure(structure)
    for residue in residues:
        ligand_id = residue.get_resname().strip()
        residue_label = f"{ligand_id}_{residue.id[1]}{residue.id[2].strip() or ''}"
        output_file = out_dir / f"{prefix}_{slugify(residue_label)}.pdb"
        io.save(str(output_file), SingleResidueSelect(chain_id, residue))
        if output_file.exists() and output_file.stat().st_size > 0:
            files.append(str(output_file))
    return files


def write_command_logs(prefix: Path, command: list[str], stdout: str, stderr: str) -> tuple[str, str, str]:
    ensure_dir(prefix.parent)
    command_log = prefix.with_name(prefix.name + "_command.txt")
    stdout_log = prefix.with_name(prefix.name + "_stdout.txt")
    stderr_log = prefix.with_name(prefix.name + "_stderr.txt")
    command_log.write_text(" ".join(command) + "\n", encoding="utf-8")
    stdout_log.write_text(stdout or "", encoding="utf-8")
    stderr_log.write_text(stderr or "", encoding="utf-8")
    return str(command_log), str(stdout_log), str(stderr_log)


def normalize_pdb_residue_numbering(path: Path) -> None:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    mapping: dict[tuple[str, str, str, str], int] = {}
    next_resseq = 1
    output: list[str] = []
    for line in lines:
        if line.startswith(("ATOM", "HETATM", "TER")) and len(line) >= 27:
            key = (line[21:22], line[22:26], line[26:27], line[17:20])
            if key not in mapping:
                mapping[key] = next_resseq
                next_resseq += 1
            line = f"{line[:22]}{mapping[key]:4d} {line[27:]}"
        output.append(line)
    path.write_text("\n".join(output) + "\n", encoding="utf-8")


def parse_pdbqt_sanity(pdbqt_path: Path, zero_threshold: float = 0.9) -> dict[str, Any]:
    if not pdbqt_path.exists():
        return {
            "pdbqt_file_size_bytes": "",
            "pdbqt_atom_count": 0,
            "pdbqt_atom_type_missing_count": 0,
            "pdbqt_atom_type_missing_fraction": "",
            "charge_zero_count": 0,
            "charge_zero_fraction": "",
            "charge_sanity_status": "not_applicable",
            "atom_type_sanity_status": "not_applicable",
            "pdbqt_sanity_status": "failed",
            "warning": "no_receptor_pdbqt",
        }
    total = 0
    zero = 0
    missing_atom_type = 0
    for line in pdbqt_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith(("ATOM", "HETATM")):
            continue
        parts = line.split()
        atom_type = parts[-1] if parts else ""
        try:
            float(atom_type)
            atom_type = ""
        except ValueError:
            pass
        if not atom_type:
            missing_atom_type += 1
        charge = None
        for token in reversed(parts[:-1]):
            try:
                charge = float(token)
                break
            except ValueError:
                continue
        if charge is None:
            continue
        total += 1
        if abs(charge) < 1e-6:
            zero += 1
    if total == 0:
        return {
            "pdbqt_file_size_bytes": pdbqt_path.stat().st_size,
            "pdbqt_atom_count": 0,
            "pdbqt_atom_type_missing_count": 0,
            "pdbqt_atom_type_missing_fraction": "",
            "charge_zero_count": 0,
            "charge_zero_fraction": "",
            "charge_sanity_status": "not_checked",
            "atom_type_sanity_status": "not_checked",
            "pdbqt_sanity_status": "failed",
            "warning": "receptor_pdbqt_charge_parse_failed",
        }
    zero_fraction = round(zero / total, 6)
    atom_type_missing_fraction = round(missing_atom_type / total, 6)
    warnings: list[str] = []
    charge_status = "ok"
    atom_type_status = "ok"
    if zero_fraction > zero_threshold:
        charge_status = "zero_charge_warning"
        warnings.append("receptor_pdbqt_zero_charge_warning")
    if missing_atom_type:
        atom_type_status = "missing_atom_type_warning"
        warnings.append("receptor_pdbqt_atom_type_missing_warning")
    return {
        "pdbqt_file_size_bytes": pdbqt_path.stat().st_size,
        "pdbqt_atom_count": total,
        "pdbqt_atom_type_missing_count": missing_atom_type,
        "pdbqt_atom_type_missing_fraction": atom_type_missing_fraction,
        "charge_zero_count": zero,
        "charge_zero_fraction": zero_fraction,
        "charge_sanity_status": charge_status,
        "atom_type_sanity_status": atom_type_status,
        "pdbqt_sanity_status": "warning" if warnings else "ok",
        "warning": ";".join(warnings),
    }


def _run_logged_command(command: list[str], *, log_prefix: Path, timeout: int = 180) -> dict[str, Any]:
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=remaining_timeout(timeout))
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        exit_code = result.returncode
    except Exception as exc:
        output = getattr(exc, "stdout", None) or ""
        error = getattr(exc, "stderr", None) or ""
        stdout = output.decode(errors="replace") if isinstance(output, bytes) else output
        stderr = (error.decode(errors="replace") if isinstance(error, bytes) else error) + "\n" + str(exc)
        exit_code = -1
    _, stdout_log, stderr_log = write_command_logs(log_prefix, command, stdout, stderr)
    return {
        "command": " ".join(command),
        "stdout_log": stdout_log,
        "stderr_log": stderr_log,
        "exit_code": exit_code,
    }


def run_pdb2pqr_protonation(cleaned_pdb: Path, pqr_path: Path, *, log_prefix: Path, ph: float = 7.4) -> dict[str, Any]:
    pdb2pqr = get_pdb2pqr()
    if not pdb2pqr:
        return {
            "ok": False,
            "protonated_receptor_pqr": "",
            "protonation_status": "failed",
            "protonation_warning": "pdb2pqr_not_available",
            "pdb2pqr_command": "",
            "pdb2pqr_stdout_log": "",
            "pdb2pqr_stderr_log": "",
            "pdb2pqr_exit_code": "",
        }
    ensure_dir(pqr_path.parent)
    cmd = [
        pdb2pqr,
        "--ff",
        DEFAULT_PROTONATION_FORCEFIELD,
        "--titration-state-method",
        "propka",
        "--with-ph",
        str(ph),
        "--drop-water",
        "--keep-chain",
        str(cleaned_pdb),
        str(pqr_path),
    ]
    result = _run_logged_command(cmd, log_prefix=log_prefix.with_name(log_prefix.name + "_pdb2pqr"), timeout=300)
    ok = result["exit_code"] == 0 and pqr_path.exists() and pqr_path.stat().st_size > 0
    return {
        "ok": ok,
        "protonated_receptor_pqr": str(pqr_path) if ok else "",
        "protonation_status": "success" if ok else "failed",
        "protonation_warning": "" if ok else "receptor_protonation_failed",
        "pdb2pqr_command": result["command"],
        "pdb2pqr_stdout_log": result["stdout_log"],
        "pdb2pqr_stderr_log": result["stderr_log"],
        "pdb2pqr_exit_code": result["exit_code"],
    }


def run_receptor_converter(
    cleaned_pdb: Path,
    pdbqt_path: Path,
    *,
    log_prefix: Path,
    backend_requested: str = "auto",
    protonated_pqr: Path | None = None,
) -> dict[str, Any]:
    warnings: list[str] = []
    backend_attempt_order = ["meeko", "autodocktools", "openbabel"] if backend_requested == "auto" else [backend_requested]
    result_data: dict[str, Any] = {
        "ok": False,
        "tool": "failed",
        "failure_reason": "",
        "warning": "",
        "receptor_prep_backend_requested": backend_requested,
        "backend_attempt_order": ">".join(backend_attempt_order),
        "backend_used": "",
        "meeko_attempted": False,
        "meeko_success": False,
        "meeko_command": "",
        "meeko_stdout_log": "",
        "meeko_stderr_log": "",
        "meeko_exit_code": "",
        "autodocktools_attempted": False,
        "autodocktools_success": False,
        "autodocktools_command": "",
        "autodocktools_stdout_log": "",
        "autodocktools_stderr_log": "",
        "autodocktools_exit_code": "",
        "fallback_attempted": False,
        "fallback_tool": "",
        "fallback_command": "",
        "fallback_stdout_log": "",
        "fallback_stderr_log": "",
        "fallback_exit_code": "",
    }
    if backend_requested not in BACKEND_CHOICES:
        result_data["failure_reason"] = "unsupported_receptor_prep_backend"
        return result_data

    if "meeko" in backend_attempt_order:
        mk_prepare = get_mk_prepare_receptor()
        if not mk_prepare:
            if backend_requested == "meeko":
                result_data["failure_reason"] = "requested_backend_not_available"
                return result_data
        else:
            if protonated_pqr and protonated_pqr.exists() and protonated_pqr.stat().st_size > 0:
                cmd = [
                    mk_prepare,
                    "--read_pqr",
                    str(protonated_pqr),
                    "--charge_model",
                    "read",
                    "-o",
                    str(pdbqt_path.with_suffix("")),
                    "-p",
                    str(pdbqt_path),
                ]
            else:
                cmd = [
                    mk_prepare,
                    "--read_pdb",
                    str(cleaned_pdb),
                    "-o",
                    str(pdbqt_path.with_suffix("")),
                    "-p",
                    str(pdbqt_path),
                ]
            result_data["meeko_attempted"] = True
            result = _run_logged_command(cmd, log_prefix=log_prefix.with_name(log_prefix.name + "_meeko"))
            result_data["meeko_command"] = result["command"]
            result_data["meeko_stdout_log"] = result["stdout_log"]
            result_data["meeko_stderr_log"] = result["stderr_log"]
            result_data["meeko_exit_code"] = result["exit_code"]
            if result["exit_code"] == 0 and pdbqt_path.exists() and pdbqt_path.stat().st_size > 0:
                result_data.update(
                    {"ok": True, "tool": "meeko", "backend_used": "meeko", "meeko_success": True}
                )
                return result_data
            warnings.append("meeko_receptor_conversion_failed")
            if backend_requested == "meeko":
                result_data["warning"] = ";".join(warnings)
                result_data["failure_reason"] = "meeko_receptor_conversion_failed"
                return result_data

    if "autodocktools" in backend_attempt_order:
        prepare_receptor4 = get_prepare_receptor4()
        if not prepare_receptor4:
            if backend_requested == "autodocktools":
                result_data["failure_reason"] = "requested_backend_not_available"
                result_data["warning"] = ";".join(warnings)
                return result_data
        else:
            cmd = [prepare_receptor4, "-r", str(cleaned_pdb), "-o", str(pdbqt_path), "-A", "checkhydrogens"]
            result_data["autodocktools_attempted"] = True
            result = _run_logged_command(cmd, log_prefix=log_prefix.with_name(log_prefix.name + "_autodocktools"))
            result_data["autodocktools_command"] = result["command"]
            result_data["autodocktools_stdout_log"] = result["stdout_log"]
            result_data["autodocktools_stderr_log"] = result["stderr_log"]
            result_data["autodocktools_exit_code"] = result["exit_code"]
            if result["exit_code"] == 0 and pdbqt_path.exists() and pdbqt_path.stat().st_size > 0:
                result_data.update(
                    {
                        "ok": True,
                        "tool": "autodocktools",
                        "backend_used": "autodocktools",
                        "autodocktools_success": True,
                    }
                )
                return result_data
            warnings.append("autodocktools_receptor_conversion_failed")
            if backend_requested == "autodocktools":
                result_data["warning"] = ";".join(warnings)
                result_data["failure_reason"] = "autodocktools_receptor_conversion_failed"
                return result_data

    if "openbabel" in backend_attempt_order:
        obabel = get_obabel()
        if not obabel:
            if backend_requested == "openbabel":
                result_data["failure_reason"] = "requested_backend_not_available"
                result_data["warning"] = ";".join(warnings)
                return result_data
        else:
            cmd = [obabel, str(cleaned_pdb), "-O", str(pdbqt_path), "-xr"]
            result_data["fallback_attempted"] = True
            result_data["fallback_tool"] = "openbabel"
            result = _run_logged_command(cmd, log_prefix=log_prefix.with_name(log_prefix.name + "_openbabel"))
            result_data["fallback_command"] = result["command"]
            result_data["fallback_stdout_log"] = result["stdout_log"]
            result_data["fallback_stderr_log"] = result["stderr_log"]
            result_data["fallback_exit_code"] = result["exit_code"]
            if result["exit_code"] == 0 and pdbqt_path.exists() and pdbqt_path.stat().st_size > 0:
                warnings.append("openbabel_fallback_used")
                result_data.update(
                    {
                        "ok": True,
                        "tool": "openbabel_fallback",
                        "backend_used": "openbabel",
                        "warning": ";".join(dict.fromkeys(warnings)),
                    }
                )
                return result_data
            warnings.append("openbabel_receptor_conversion_failed")
            if backend_requested == "openbabel":
                result_data["warning"] = ";".join(dict.fromkeys(warnings))
                result_data["failure_reason"] = "openbabel_receptor_conversion_failed"
                return result_data

    result_data["warning"] = ";".join(dict.fromkeys(warnings))
    result_data["failure_reason"] = "no_receptor_pdbqt"
    return result_data


def prepare_receptor_row(
    row: dict[str, str],
    *,
    csv_dir: Path,
    out_dir: Path,
    min_protein_atoms: int,
    receptor_prep_backend: str = "auto",
    ph: float = 7.4,
) -> dict[str, Any]:
    input_file = resolve_structure_path(row, csv_dir)
    qc = base_qc(row, input_file, backend_requested=receptor_prep_backend, ph=ph)
    qc.update({key: row.get(key, "") for key in IDENTITY_FIELDS})
    target_gene = slugify(row.get("target_gene") or "target")
    pdb_id = slugify(row.get("pdb_id") or "pdb")
    chain_id = row.get("chain_id", "")
    ligand_id = row.get("cocrystal_ligand_id", "")

    if row.get("structure_source") != "local_pdb":
        qc["failure_reason"] = "unsupported_structure_type"
        return qc
    if not input_file.exists():
        qc["failure_reason"] = "missing_structure_file"
        return qc
    if not chain_id:
        qc["failure_reason"] = "missing_chain"
        return qc
    if len(chain_id) != 1:
        qc["failure_reason"] = "invalid_chain_id"
        return qc

    parser = PDBParser(QUIET=True)
    try:
        structure = parser.get_structure(pdb_id, str(input_file))
    except Exception:
        qc["failure_reason"] = "structure_file_parse_failed"
        return qc

    model = structure[0]
    if chain_id not in {chain.id for chain in model.get_chains()}:
        qc["failure_reason"] = "no_selected_chain"
        return qc
    chain = model[chain_id]
    if row.get("target_identity_status"):
        if (row.get("target_identity_status") != "verified_rcsb_entity_instance"
                or row.get("auth_asym_id") != chain_id
                or not row.get("selected_chain_sequence")
                or protein_signature(chain) != row["selected_chain_sequence"]):
            qc["failure_reason"] = "target_chain_identity_mismatch"
            return qc
    protein_atoms = [atom for residue in chain if residue.id[0] == " " for atom in residue.get_atoms()]
    if len(protein_atoms) < min_protein_atoms:
        qc["failure_reason"] = "chain_too_short"
        return qc

    hetero_analysis = analyze_heteroatoms(chain, ligand_id)
    classes = hetero_analysis["classes"]
    selected_reference_ligands = hetero_analysis["selected_reference_ligands"]
    qc["removed_waters_count"] = sum(len(list(residue.get_atoms())) for residue in classes["water"])
    qc["removed_water_residue_names"] = ";".join(sorted({residue.get_resname().strip() for residue in classes["water"]}))
    qc["removed_solvents_buffers_salts"] = ";".join(residue_key(residue) for residue in classes["solvent_buffer_salt"])
    qc["removed_hetero_count"] = sum(
        len(list(residue.get_atoms()))
        for residue in classes["water"] + classes["solvent_buffer_salt"] + selected_reference_ligands
    )
    qc["kept_metals"] = ";".join(residue_key(residue) for residue in classes["metal"])
    qc["kept_cofactors"] = ";".join(residue_key(residue) for residue in classes["cofactor"])
    qc["unknown_hetatm_warning"] = (
        "unknown_hetatm_warning:" + ";".join(residue_key(residue) for residue in classes["unknown"])
        if classes["unknown"]
        else ""
    )
    qc["box_defining_ligands_detected"] = ";".join(residue_key(residue) for residue in classes["reference_pocket_ligand"])
    qc["box_defining_ligands_selected"] = ";".join(residue_key(residue) for residue in selected_reference_ligands)
    qc["box_defining_ligands_removed_from_receptor"] = qc["box_defining_ligands_selected"]

    prefix = f"{target_gene}_{pdb_id}_{slugify(chain_id)}"
    cleaned_pdb = out_dir / "cleaned_pdb" / f"{prefix}.pdb"
    reference_dir = out_dir / "reference_ligands"
    ligand_file = reference_dir / f"{prefix}_{slugify(ligand_id or 'ligand')}.pdb"
    pqr_path = out_dir / "protonated_pqr" / f"{prefix}.pqr"
    pdbqt_path = out_dir / "pdbqt" / f"{prefix}.pdbqt"
    log_prefix = out_dir / "logs" / prefix
    ensure_dir(cleaned_pdb.parent)
    ensure_dir(ligand_file.parent)
    ensure_dir(pqr_path.parent)
    ensure_dir(pdbqt_path.parent)

    try:
        reference_files = save_reference_ligands(structure, chain_id, selected_reference_ligands, reference_dir, prefix)
        ligand_saved = bool(reference_files)
        io = PDBIO()
        io.set_structure(structure)
        io.save(str(cleaned_pdb), CleanedReceptorSelect(chain_id, hetero_analysis["keep_hetero_ids"]))
        normalize_pdb_residue_numbering(cleaned_pdb)
    except Exception:
        qc["failure_reason"] = "structure_file_parse_failed"
        return qc

    qc["cleaned_receptor_pdb"] = str(cleaned_pdb)
    qc["reference_ligand_files"] = ";".join(reference_files)
    qc["cocrystal_ligand_saved"] = ligand_saved
    qc["cocrystal_ligand_file"] = reference_files[0] if reference_files else ""

    protonation = run_pdb2pqr_protonation(cleaned_pdb, pqr_path, log_prefix=log_prefix, ph=ph)
    qc["protonated_receptor_pqr"] = protonation["protonated_receptor_pqr"]
    qc["protonation_status"] = protonation["protonation_status"]
    qc["protonation_warning"] = protonation["protonation_warning"]
    qc["pdb2pqr_command"] = protonation["pdb2pqr_command"]
    qc["pdb2pqr_stdout_log"] = protonation["pdb2pqr_stdout_log"]
    qc["pdb2pqr_stderr_log"] = protonation["pdb2pqr_stderr_log"]
    qc["pdb2pqr_exit_code"] = protonation["pdb2pqr_exit_code"]

    conversion = run_receptor_converter(
        cleaned_pdb,
        pdbqt_path,
        log_prefix=log_prefix,
        backend_requested=receptor_prep_backend,
        protonated_pqr=Path(qc["protonated_receptor_pqr"]) if qc["protonated_receptor_pqr"] else None,
    )
    qc["preparation_tool"] = conversion["tool"]
    qc["warning"] = conversion["warning"]
    qc["receptor_prep_backend_requested"] = conversion["receptor_prep_backend_requested"]
    qc["backend_attempt_order"] = conversion["backend_attempt_order"]
    qc["backend_used"] = conversion["backend_used"]
    qc["meeko_attempted"] = conversion["meeko_attempted"]
    qc["meeko_success"] = conversion["meeko_success"]
    qc["meeko_command"] = conversion["meeko_command"]
    qc["meeko_stdout_log"] = conversion["meeko_stdout_log"]
    qc["meeko_stderr_log"] = conversion["meeko_stderr_log"]
    qc["meeko_exit_code"] = conversion["meeko_exit_code"]
    qc["autodocktools_attempted"] = conversion["autodocktools_attempted"]
    qc["autodocktools_success"] = conversion["autodocktools_success"]
    qc["autodocktools_command"] = conversion["autodocktools_command"]
    qc["autodocktools_stdout_log"] = conversion["autodocktools_stdout_log"]
    qc["autodocktools_stderr_log"] = conversion["autodocktools_stderr_log"]
    qc["autodocktools_exit_code"] = conversion["autodocktools_exit_code"]
    qc["fallback_attempted"] = conversion["fallback_attempted"]
    qc["fallback_tool"] = conversion["fallback_tool"]
    qc["fallback_command"] = conversion["fallback_command"]
    qc["fallback_stdout_log"] = conversion["fallback_stdout_log"]
    qc["fallback_stderr_log"] = conversion["fallback_stderr_log"]
    qc["fallback_exit_code"] = conversion["fallback_exit_code"]
    pre_warnings = [w for w in str(qc["warning"]).split(";") if w]
    if qc["protonation_warning"]:
        pre_warnings.append(str(qc["protonation_warning"]))
    if qc["unknown_hetatm_warning"]:
        pre_warnings.append("unknown_hetatm_warning")
    qc["warning"] = ";".join(dict.fromkeys(pre_warnings))
    if not conversion["ok"]:
        qc["failure_reason"] = conversion["failure_reason"] or (
            "meeko_receptor_conversion_failed" if "meeko" in str(qc["warning"]) else "no_receptor_pdbqt"
        )
        return qc
    pdbqt_sanity = parse_pdbqt_sanity(pdbqt_path)
    for field in [
        "pdbqt_file_size_bytes",
        "pdbqt_atom_count",
        "pdbqt_atom_type_missing_count",
        "pdbqt_atom_type_missing_fraction",
        "charge_zero_count",
        "charge_zero_fraction",
        "charge_sanity_status",
        "atom_type_sanity_status",
        "pdbqt_sanity_status",
    ]:
        qc[field] = pdbqt_sanity[field]
    warnings = [w for w in str(qc["warning"]).split(";") if w]
    if pdbqt_sanity["warning"]:
        warnings.extend(str(pdbqt_sanity["warning"]).split(";"))
    qc["warning"] = ";".join(dict.fromkeys(warnings))
    qc["prepared_receptor_pdbqt"] = str(pdbqt_path)
    if row.get("target_identity_status"):
        if prepared_signature(pdbqt_path) != row.get("selected_chain_sequence"):
            qc["prepared_identity_status"] = "failed_sequence_check"
            qc["failure_reason"] = "prepared_receptor_identity_mismatch"
            return qc
        qc["prepared_identity_status"] = "verified_selected_chain_sequence"
    else:
        qc["prepared_identity_status"] = "legacy_input_unverified"
    qc["preparation_status"] = "success"
    qc["failure_reason"] = ""
    qc["usable_for_scientific_ranking"] = (
        qc["backend_used"] in {"meeko", "autodocktools"}
        and qc["pdbqt_sanity_status"] == "ok"
        and qc["protonation_status"] == "success"
    )
    if qc["preparation_tool"] == "openbabel_fallback" or qc["pdbqt_sanity_status"] != "ok":
        qc["confidence_level"] = "medium"
    else:
        qc["confidence_level"] = "high" if ligand_saved else "medium"
    return qc


def prepare_receptors(
    input_csv: Path,
    *,
    out_dir: Path,
    min_protein_atoms: int = 20,
    receptor_prep_backend: str = "auto",
    ph: float = 7.4,
) -> list[dict[str, Any]]:
    if receptor_prep_backend not in BACKEND_CHOICES:
        raise ValueError(f"Unsupported receptor preparation backend: {receptor_prep_backend}")
    out_dir = ensure_dir(out_dir)
    rows = read_receptor_csv(input_csv)
    qc_rows = [
        prepare_receptor_row(
            row,
            csv_dir=input_csv.parent,
            out_dir=out_dir,
            min_protein_atoms=min_protein_atoms,
            receptor_prep_backend=receptor_prep_backend,
            ph=ph,
        )
        for row in rows
    ]
    write_rows(out_dir / "receptor_preparation_qc.csv", qc_rows, RECEPTOR_FIELDS)
    return qc_rows
