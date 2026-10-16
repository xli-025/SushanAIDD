from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

from .common import ensure_dir, find_executable, write_json

PYTHON_PACKAGES = ["pandas", "numpy", "yaml", "rdkit", "meeko"]
COMMANDS = ["vina", "obabel", "deepfrag2", "deepfrag2full", "deepfrag2_test"]

# Standalone DeepFrag2 env path (relative to this file's location)
# scripts/ → reverse_design_after_docking/ → modules/ → Reverse Design/
_DEEPFRAG2_ENV_BIN = Path(__file__).resolve().parents[3] / ".deepfrag2_env" / "bin"


def import_ok(package: str) -> bool:
    return importlib.util.find_spec(package) is not None


def _find_deepfrag2_bin(name: str) -> str:
    """Search for deepfrag2 binaries in the dedicated env first, then PATH."""
    candidate = _DEEPFRAG2_ENV_BIN / name
    if candidate.exists():
        return str(candidate)
    return find_executable(name) or ""


def check_environment(out_dir: Path, *, run_deepfrag2_test: bool = True) -> dict[str, Any]:
    out_dir = ensure_dir(out_dir)
    package_status = {package: import_ok(package) for package in PYTHON_PACKAGES}
    command_paths = {cmd: find_executable(cmd) for cmd in COMMANDS}

    # DeepFrag2 has a dedicated env — check that too
    deepfrag2_bin = _find_deepfrag2_bin("deepfrag2")
    deepfrag2full_bin = _find_deepfrag2_bin("deepfrag2full")
    deepfrag2_test_bin = _find_deepfrag2_bin("deepfrag2_test")
    command_paths["deepfrag2"] = deepfrag2_bin
    command_paths["deepfrag2full"] = deepfrag2full_bin
    command_paths["deepfrag2_test"] = deepfrag2_test_bin

    # Check deepfrag2 env Python version
    deepfrag2_python = _DEEPFRAG2_ENV_BIN / "python3"
    if not deepfrag2_python.exists():
        deepfrag2_python = _DEEPFRAG2_ENV_BIN / "python"
    deepfrag2_python_version = ""
    if deepfrag2_python.exists():
        try:
            r = subprocess.run(
                [str(deepfrag2_python), "--version"],
                capture_output=True, text=True, timeout=10,
            )
            deepfrag2_python_version = r.stdout.strip() or r.stderr.strip()
        except Exception:
            pass

    deepfrag2_env_ok = False
    deepfrag2_test_status = "not_found"
    deepfrag2_test_output = ""

    test_command = deepfrag2_test_bin
    if test_command and run_deepfrag2_test:
        try:
            # Invoke via the standalone env's python3 to avoid shebang
            # issues when the env path contains spaces ("Reverse Design").
            df2_py = _DEEPFRAG2_ENV_BIN / "python3"
            if df2_py.exists() and test_command.startswith(str(_DEEPFRAG2_ENV_BIN)):
                cmd = [str(df2_py), test_command]
            else:
                cmd = [test_command]
            result = subprocess.run(
                cmd,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=300,
                cwd=str(out_dir),
            )
            deepfrag2_test_status = "passed" if result.returncode == 0 else "failed"
            deepfrag2_test_output = result.stdout[-6000:]
            deepfrag2_env_ok = result.returncode == 0
        except subprocess.TimeoutExpired:
            deepfrag2_test_status = "timeout"
            deepfrag2_test_output = "deepfrag2_test timed out (>300s)"
        except Exception as exc:
            deepfrag2_test_status = "failed"
            deepfrag2_test_output = str(exc)
    elif deepfrag2_bin:
        # User explicitly skipped the test (--skip-deepfrag2-test).
        # Trust that the env is usable if the binary exists.
        deepfrag2_test_status = "test_skipped"
        deepfrag2_env_ok = True

    blocking_missing = [
        package for package, ok in package_status.items() if package in {"pandas", "numpy", "rdkit", "meeko"} and not ok
    ]
    if not command_paths.get("vina"):
        blocking_missing.append("vina")

    report = {
        "module": "reverse_design_after_docking",
        "python_packages": package_status,
        "commands": command_paths,
        "deepfrag2_env_path": str(_DEEPFRAG2_ENV_BIN),
        "deepfrag2_python_version": deepfrag2_python_version,
        "blocking_missing_dependencies": blocking_missing,
        "can_run_dry_backend": all(package_status.get(pkg, False) for pkg in ["pandas", "numpy", "rdkit", "meeko"]),
        "can_run_vina_redocking": bool(command_paths.get("vina")),
        "deepfrag2_env_ok": deepfrag2_env_ok,
        "deepfrag2_test_status": deepfrag2_test_status,
        "deepfrag2_test_output_tail": deepfrag2_test_output,
        "no_training": True,
    }
    write_json(out_dir / "environment_check_report.json", report)
    lines = [
        "reverse_design_after_docking environment check",
        "",
        "DeepFrag2 standalone env: " + str(_DEEPFRAG2_ENV_BIN),
        "DeepFrag2 Python version: " + (deepfrag2_python_version or "not found"),
        "",
        "Main project Python packages:",
        *[f"- {pkg}: {'ok' if ok else 'missing'}" for pkg, ok in package_status.items()],
        "",
        "Commands (standalone env / PATH):",
        *[f"- {cmd}: {path or 'missing'}" for cmd, path in command_paths.items()],
        "",
        f"DeepFrag2 test status: {deepfrag2_test_status}",
        "DeepFrag2 training: disabled / not used",
    ]
    if deepfrag2_test_output:
        lines.extend(["", "DeepFrag2 test output tail:", deepfrag2_test_output])
    (out_dir / "environment_check_report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report
