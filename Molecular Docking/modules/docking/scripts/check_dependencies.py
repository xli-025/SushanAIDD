from __future__ import annotations

import importlib
import platform
import shutil
import subprocess
import sys
import warnings
from pathlib import Path
from typing import Any

from modules.docking.scripts.common import DEFAULT_OUTPUT_DIR, write_json


PYTHON_IMPORTS = {
    "rdkit": "rdkit",
    "meeko": "meeko",
    "pandas": "pandas",
    "requests": "requests",
    "biopython": "Bio",
    "pdb2pqr": "pdb2pqr",
    "propka": "propka",
}


def _which_with_venv_sibling(name: str) -> str:
    sibling = Path(sys.executable).with_name(name)
    found = shutil.which(name)
    if found:
        return found
    if sibling.exists():
        return str(sibling)
    return ""


def _module_status(import_name: str) -> tuple[bool, str]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            module = importlib.import_module(import_name)
    except Exception:
        return False, ""
    version = getattr(module, "__version__", "")
    if import_name == "Bio":
        version = getattr(module, "__version__", version)
    return True, str(version)


def _run_version(command: list[str], timeout: int = 10) -> tuple[bool, str]:
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=timeout)
    except Exception:
        return False, ""
    output = (result.stdout or result.stderr or "").strip()
    first_line = output.splitlines()[0] if output else ""
    return result.returncode == 0 or bool(first_line), first_line


def diagnose(output_dir: Path | None = None, write: bool = True) -> dict[str, Any]:
    output_dir = output_dir or DEFAULT_OUTPUT_DIR
    data: dict[str, Any] = {
        "python_executable": sys.executable,
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
    }

    for label, import_name in PYTHON_IMPORTS.items():
        ok, version = _module_status(import_name)
        data[f"{label}_import_ok"] = ok
        data[f"{label}_version"] = version

    vina_bin = _which_with_venv_sibling("vina")
    data["vina_cli_path"] = vina_bin
    if vina_bin:
        ok, version = _run_version([vina_bin, "--version"])
    else:
        ok, version = False, ""
    data["vina_cli_ok"] = ok
    data["vina_version"] = version

    openbabel_bin = shutil.which("obabel") or (
        "/opt/homebrew/bin/obabel" if Path("/opt/homebrew/bin/obabel").exists() else ""
    )
    data["openbabel_cli_path"] = openbabel_bin or ""
    data["openbabel_cli_ok"] = bool(openbabel_bin)
    if openbabel_bin:
        _, data["openbabel_version"] = _run_version([openbabel_bin, "-V"])
    else:
        data["openbabel_version"] = ""

    mk_prepare_receptor = _which_with_venv_sibling("mk_prepare_receptor.py")
    data["mk_prepare_receptor_cli_path"] = mk_prepare_receptor
    data["mk_prepare_receptor_cli_ok"] = bool(mk_prepare_receptor)

    pdb2pqr_bin = _which_with_venv_sibling("pdb2pqr")
    data["pdb2pqr_cli_path"] = pdb2pqr_bin
    data["pdb2pqr_cli_ok"] = bool(pdb2pqr_bin)
    if pdb2pqr_bin:
        _, data["pdb2pqr_version"] = _run_version([pdb2pqr_bin, "--version"])
    else:
        data["pdb2pqr_version"] = ""

    propka_bin = _which_with_venv_sibling("propka3") or _which_with_venv_sibling("propka")
    data["propka_cli_path"] = propka_bin
    data["propka_cli_ok"] = bool(propka_bin)
    data["propka_available"] = bool(data["propka_import_ok"] or propka_bin)
    data["protonation_backend_available"] = bool(data["pdb2pqr_cli_ok"] and data["propka_available"])

    prepare_receptor4 = _which_with_venv_sibling("prepare_receptor4.py")
    data["prepare_receptor4_cli_path"] = prepare_receptor4
    data["prepare_receptor4_cli_ok"] = bool(prepare_receptor4)

    pythonsh = shutil.which("pythonsh")
    data["mgltools_pythonsh_path"] = pythonsh or ""
    data["mgltools_available"] = bool(pythonsh)
    data["autodocktools_available"] = bool(prepare_receptor4)

    adfr_bin = shutil.which("adfr") or shutil.which("agfr") or shutil.which("prepare_receptor")
    data["adfr_path"] = adfr_bin or ""
    data["adfr_available"] = bool(adfr_bin)

    p2rank_bin = shutil.which("prank") or shutil.which("p2rank")
    data["p2rank_path"] = p2rank_bin or ""
    data["p2rank_available"] = bool(p2rank_bin)

    java_bin = shutil.which("java")
    data["java_path"] = java_bin or ""
    if java_bin:
        _, java_version = _run_version([java_bin, "-version"])
    else:
        java_version = ""
    data["java_version"] = java_version

    blocking: list[str] = []
    if not data["rdkit_import_ok"]:
        blocking.append("rdkit: required for ligand preparation in later MVP stages")
    if not data["meeko_import_ok"]:
        blocking.append("meeko: required for PDBQT generation in later MVP stages")
    if not data["vina_cli_ok"]:
        blocking.append("vina: required to run validate-engine and docking jobs")

    optional: list[str] = []
    if not data["biopython_import_ok"]:
        optional.append("biopython: required for later receptor/PDB handling")
    if not data["openbabel_cli_ok"]:
        optional.append("openbabel: optional structure conversion fallback")
    if not data["mk_prepare_receptor_cli_ok"]:
        optional.append("mk_prepare_receptor.py: preferred receptor PDBQT preparation backend")
    if not data["pdb2pqr_cli_ok"]:
        optional.append("pdb2pqr: preferred receptor protonation backend")
    if not data["propka_available"]:
        optional.append("propka: preferred receptor titration-state backend through PDB2PQR")
    if not data["prepare_receptor4_cli_ok"]:
        optional.append("prepare_receptor4.py: optional AutoDockTools receptor preparation backend")
    if not data["mgltools_available"]:
        optional.append("MGLTools pythonsh: optional AutoDockTools runtime")
    if not data["adfr_available"]:
        optional.append("ADFR Suite: optional receptor preparation backend")
    if not data["p2rank_available"]:
        optional.append("p2rank: optional predicted pocket mode")
    if not data["java_version"]:
        optional.append("java: optional unless P2Rank is enabled")

    data["blocking_missing_dependencies"] = blocking
    data["optional_missing_dependencies"] = optional
    data["can_run_minimal_vina"] = bool(data["vina_cli_ok"])
    data["install_suggestions"] = [
        "Use the project .venv/bin/python -m pip install rdkit meeko biopython for Python dependencies.",
        "Install AutoDock Vina and ensure a 'vina' executable is on PATH, or add a VINA_BIN environment variable before running.",
        "OpenBabel, Java, and P2Rank are optional for this fixture-first MVP.",
    ]

    if write:
        write_json(output_dir / "dependency_diagnostic.json", data)
    return data


def assert_can_run_vina(output_dir: Path | None = None) -> dict[str, Any]:
    diagnostic = diagnose(output_dir=output_dir, write=True)
    if not diagnostic["can_run_minimal_vina"]:
        raise RuntimeError("AutoDock Vina executable was not found; see dependency_diagnostic.json")
    return diagnostic
