"""Shared small chemistry preparation/protocol contracts; no model or scoring changes."""
from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def apply_requested_ph(mol, ph, *, obabel_bin=None, run_command=None):
    from rdkit import Chem
    if ph is None:
        return mol, "skipped_not_requested", "none", "", "input_state_single"
    if not math.isfinite(float(ph)) or not 0 <= float(ph) <= 14:
        raise ValueError("pH must be finite and between 0 and 14")
    if obabel_bin is None:
        obabel_bin = shutil.which("obabel")
        if not obabel_bin:
            sibling = Path(sys.executable).with_name("obabel")
            obabel_bin = str(sibling) if sibling.is_file() else ""
    if not obabel_bin:
        return mol, "failed_fallback_input_state", "none", "openbabel_not_found", "input_state_fallback"
    try:
        from npflow_runtime import remaining_timeout
        with tempfile.TemporaryDirectory(prefix="npflow_ligand_ph_") as folder:
            source, dest = Path(folder) / "input.sdf", Path(folder) / "protonated.sdf"
            writer = Chem.SDWriter(str(source))
            writer.write(mol)
            writer.close()
            (run_command or subprocess.run)([obabel_bin, str(source), "-O", str(dest), "-p", str(ph)],
                capture_output=True, text=True, timeout=remaining_timeout(30), check=True)
            if not dest.is_file() or not dest.stat().st_size:
                raise ValueError("openbabel_output_missing")
            prepared = next((m for m in Chem.SDMolSupplier(str(dest), removeHs=False) if m is not None), None)
            if prepared is None:
                raise ValueError("openbabel_output_has_no_molecule")
            return prepared, "applied", "openbabel", "", "ph_adjusted_single"
    except Exception as exc:
        return mol, "failed_fallback_input_state", "openbabel", str(exc) or type(exc).__name__, "input_state_fallback"


def file_hash(path):
    path = Path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "missing"


def docking_protocol(receptor, box, vina, params, ph, protonation_status):
    data = {"version": "npflow_comparison.v1", "receptor_sha256": file_hash(receptor),
            "vina_sha256": file_hash(vina), "scoring_function": "vina",
            "box": {key: float(box[key]) for key in ("center_x", "center_y", "center_z", "size_x", "size_y", "size_z")},
            "vina_parameters": {key: float(params[key]) for key in ("exhaustiveness", "num_modes", "energy_range", "cpu", "seed")},
            "ph_requested": None if ph in (None, "") else float(ph),
            "protonation_rule": "openbabel_pH_single_state.v1", "protonation_status": protonation_status}
    text = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest(), data
