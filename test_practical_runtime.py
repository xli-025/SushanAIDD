import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from npflow_runtime import remaining_timeout, run_logged, stage_budget
from npflow_evidence import collect_evidence


def test_full_log_preserves_first_error_and_exit(tmp_path, monkeypatch):
    monkeypatch.setenv("NPFLOW_STAGE_NAME", "test")
    result = run_logged([sys.executable, "-c", "import sys; print('FIRST_REAL_ERROR',flush=True); print('detail'*3000); sys.exit(7)"],
                        cwd=str(tmp_path), timeout=5, log_dir=tmp_path / "logs")
    assert result.returncode == 7
    assert result.stdout.startswith("FIRST_REAL_ERROR")
    assert "FIRST_REAL_ERROR" in next((tmp_path / "logs").glob("*.log")).read_text()


def test_timeout_keeps_partial_output(tmp_path):
    result = run_logged([sys.executable, "-c", "import time; print('BEFORE_TIMEOUT',flush=True); time.sleep(10)"],
                        cwd=str(tmp_path), timeout=.2, log_dir=tmp_path / "logs")
    assert result.returncode == 124
    assert "BEFORE_TIMEOUT" in result.stdout and "STAGE_TIMEOUT" in result.stdout


def test_deadline_restores_environment(monkeypatch):
    monkeypatch.delenv("NPFLOW_STAGE_DEADLINE", raising=False)
    @stage_budget("probe", 3)
    def call():
        assert 0 < remaining_timeout(30) <= 3
        return remaining_timeout(.1)
    assert call() == .1
    assert "NPFLOW_STAGE_DEADLINE" not in os.environ


def test_evidence_deduplicates_and_replay_is_portable(tmp_path):
    run = tmp_path / "owned"
    dock = run / "09_docking"
    (dock / "smoke_vina").mkdir(parents=True)
    receptor = dock / "smoke_vina" / "receptor.pdbqt"
    ligand = dock / "smoke_vina" / "ligand.pdbqt"
    receptor.write_text("RECEPTOR")
    ligand.write_text("LIGAND")
    (dock / "smoke_vina" / "duplicate.pdbqt").write_text("RECEPTOR")
    (dock / "smoke_vina" / "config.txt").write_text(f"receptor = {receptor}\nligand = {ligand}\nout = /old/pose.pdbqt\nexhaustiveness = 16\n")
    dest = tmp_path / "package"
    manifest = collect_evidence(run, dock, None, dest)
    assert manifest["unique_files"] == 3
    config = (dest / "evidence" / manifest["replay_configs"][0]["config"]).read_text()
    assert str(tmp_path) not in config and "/old" not in config
    for line in config.splitlines():
        if line.startswith(("receptor", "ligand")):
            assert (dest / "evidence" / line.split("=", 1)[1].strip()).is_file()
