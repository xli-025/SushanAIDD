from __future__ import annotations

import io
import ipaddress
import json
import sqlite3
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.message import Message
from http import HTTPStatus
from pathlib import Path

import pytest

from webapp import sanitize_access_logs, server


@pytest.fixture()
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "aidd"
    web_root = tmp_path / "web"
    monkeypatch.setattr(server, "ROOT", root)
    monkeypatch.setattr(server, "WEB_ROOT", web_root)
    monkeypatch.setattr(server, "STATIC_ROOT", web_root / "static")
    monkeypatch.setattr(server, "DB_PATH", web_root / "test.sqlite3")
    monkeypatch.setattr(server, "ARTIFACT_ROOT", web_root / "artifacts")
    monkeypatch.setattr(server, "LOG_ROOT", web_root / "logs")
    monkeypatch.setattr(server, "PYTHON", root / ".venv/bin/python")
    monkeypatch.setattr(server, "NPFLOW_AGENT", root / "npflow_agent.py")
    server.init_db()
    return root


def add_user(username: str = "user") -> int:
    with server.db() as conn:
        cur = conn.execute(
            "INSERT INTO users(username, password_hash, email, created_at) VALUES(?, ?, ?, ?)",
            (username, "unused", f"{username}@example.test", server.now()),
        )
        return int(cur.lastrowid)


def add_task(
    user_id: int,
    *,
    status: str = "queued",
    requested_mode: str = "network",
    run_id: str = "run-1",
    result_dir: str = "",
    log_path: str = "",
    expires_at: str | None = None,
    pid: int | None = None,
    process_identity: str = "",
) -> int:
    with server.db() as conn:
        cur = conn.execute(
            """
            INSERT INTO tasks(
                user_id, email, herbs, disease, ph, input_text, requested_mode,
                status, run_id, result_dir, log_path, expires_at, created_at, pid, process_identity
            ) VALUES(?, ?, 'herb', 'disease', '7.4', 'input', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                f"user-{user_id}@example.test",
                requested_mode,
                status,
                run_id,
                result_dir,
                log_path,
                expires_at,
                server.now(),
                pid,
                process_identity,
            ),
        )
        return int(cur.lastrowid)


def task_row(task_id: int) -> sqlite3.Row:
    with server.db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row is not None
    return row


def write_manifest(task_id: int, mode: str, status: str, stages: dict[str, dict], run_id: str = "run-1") -> Path:
    result_dir = server.expected_web_result_dir(task_id)
    result_dir.mkdir(parents=True, exist_ok=True)
    package_marker = result_dir / "说明书.txt"
    package_marker.write_text("fixture package\n", encoding="utf-8")
    stages = {
        **stages,
        "packaging": {
            "requested": False,
            "status": "completed",
            "reason": "",
            "required_artifacts": ["说明书.txt"],
            "artifacts": [{"path": "说明书.txt"}],
        },
    }
    payload = {
        "schema_version": 1,
        "kind": "npflow_pipeline_status",
        "task_id": f"task_{task_id}",
        "run_id": run_id,
        "requested_mode": mode,
        "status": status,
        "result_dir": str(result_dir.resolve()),
        "stages": stages,
    }
    path = result_dir / server.PIPELINE_STATUS_FILENAME
    path.write_text(json.dumps(payload), encoding="utf-8")
    return result_dir


def write_network_run_binding(
    path: Path,
    task_id: int,
    run_id: str,
    *,
    stage: str = "network",
    output_dir: Path | None = None,
    status: str = "provisional",
) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "kind": "npflow_network_run_binding",
        "task_id": f"task_{task_id}",
        "run_id": run_id,
        "stage": stage,
        "status": status,
        "output_dir": str((output_dir or path).resolve()),
    }
    (path / server.NETWORK_RUN_MANIFEST_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    return path.resolve()


def completed_stage(path: str) -> dict:
    return {
        "requested": True,
        "status": "completed",
        "reason": "",
        "required_artifacts": [path],
        "artifacts": [path],
    }


def handler_with_capture() -> tuple[server.Handler, list[tuple]]:
    handler = object.__new__(server.Handler)
    captured: list[tuple] = []
    handler._json = lambda status, data, headers=None: captured.append((status, data, headers))
    return handler, captured


def test_delete_targets_are_fixed_and_logs_never_grant_paths(isolated: Path) -> None:
    user_id = add_user()
    task_id = add_task(user_id)
    expected_log = server.LOG_ROOT / f"task_{task_id}.log"
    victim = isolated / "Network Pharmacology/results/network_pharmacology_runs/other_user_run"
    victim.mkdir(parents=True)
    expected_log.write_text(f"启动参数\nNPFLOW_NETWORK_RUN_DIR={victim}\n", encoding="utf-8")
    with server.db() as conn:
        conn.execute("UPDATE tasks SET log_path = ? WHERE id = ?", (str(expected_log), task_id))

    targets = server.task_paths_for_delete(task_row(task_id))

    assert expected_log.resolve() in targets
    assert victim.resolve() not in targets
    assert not server.is_task_owned_delete_path(victim)
    assert not server.is_task_owned_delete_path(server.LOG_ROOT)
    assert not server.is_task_owned_delete_path(server.ARTIFACT_ROOT)


def test_network_resources_require_exact_task_run_stage_and_output_binding(isolated: Path) -> None:
    user_id = add_user()
    task_id = add_task(user_id, run_id="server-issued-run-id-000001")
    root = server.network_runs_root()
    own_source = write_network_run_binding(root / "own-source", task_id, "server-issued-run-id-000001")
    own_formula = write_network_run_binding(
        root / "own-formula", task_id, "server-issued-run-id-000001", status="claimed"
    )
    other_task = write_network_run_binding(root / "other-task", task_id + 1, "server-issued-run-id-000001")
    wrong_run = write_network_run_binding(root / "wrong-run", task_id, "server-issued-run-id-999999")
    wrong_stage = write_network_run_binding(
        root / "wrong-stage", task_id, "server-issued-run-id-000001", stage="docking"
    )
    wrong_output = write_network_run_binding(
        root / "wrong-output",
        task_id,
        "server-issued-run-id-000001",
        output_dir=root / "somewhere-else",
    )

    targets = server.task_paths_for_delete(task_row(task_id))

    assert own_source in targets
    assert own_formula in targets
    assert other_task not in targets
    assert wrong_run not in targets
    assert wrong_stage not in targets
    assert wrong_output not in targets
    with server.db() as conn:
        resources = conn.execute(
            "SELECT path FROM task_resources WHERE task_id = ? ORDER BY path", (task_id,)
        ).fetchall()
    assert {Path(row["path"]) for row in resources} == {own_source, own_formula}


def test_persisted_network_resource_is_revalidated_before_delete(isolated: Path) -> None:
    user_id = add_user()
    task_id = add_task(user_id, run_id="server-issued-run-id-000002")
    path = write_network_run_binding(
        server.network_runs_root() / "owned", task_id, "server-issued-run-id-000002"
    )
    server.discover_task_resources(task_row(task_id))
    manifest_path = path / server.NETWORK_RUN_MANIFEST_FILENAME
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["stage"] = "docking"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported contract"):
        server.task_paths_for_delete(task_row(task_id))

    assert path.is_dir()


def test_task_delete_removes_all_bound_network_runs_but_not_other_runs(isolated: Path) -> None:
    user_id = add_user()
    task_id = add_task(user_id, status="failed", run_id="server-issued-run-id-000003")
    root = server.network_runs_root()
    own_source = write_network_run_binding(root / "source", task_id, "server-issued-run-id-000003")
    own_formula = write_network_run_binding(root / "formula", task_id, "server-issued-run-id-000003")
    other = write_network_run_binding(root / "other", task_id + 1, "server-issued-run-id-000003")

    server.delete_task_record(task_row(task_id))

    assert not own_source.exists()
    assert not own_formula.exists()
    assert other.exists()
    with server.db() as conn:
        assert conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone() is None
        assert conn.execute("SELECT 1 FROM task_resources WHERE task_id = ?", (task_id,)).fetchone() is None


def test_completed_requires_matching_manifest_and_artifacts(isolated: Path) -> None:
    user_id = add_user()
    task_id = add_task(user_id, status="running")
    artifact = server.expected_web_result_dir(task_id) / "01_network_pharmacology/qc_report.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{}", encoding="utf-8")
    result_dir = write_manifest(
        task_id,
        "network",
        "completed",
        {"network": completed_stage("01_network_pharmacology/qc_report.json")},
    )

    status, reason, stages = server.validate_pipeline_status(task_row(task_id), result_dir)

    assert status == "completed"
    assert reason == ""
    assert stages["network"]["status"] == "completed"

    (result_dir / "01_network_pharmacology/qc_report.json").unlink()
    status, reason, _ = server.validate_pipeline_status(task_row(task_id), result_dir)
    assert status == "failed"
    assert "必需产物" in reason


def test_manifest_rejects_stale_run_and_mode(isolated: Path) -> None:
    user_id = add_user()
    task_id = add_task(user_id, status="running", requested_mode="network+docking")
    result_dir = write_manifest(
        task_id,
        "network",
        "completed",
        {"network": completed_stage("anything.txt")},
        run_id="old-run",
    )
    (result_dir / "anything.txt").write_text("data", encoding="utf-8")

    status, reason, _ = server.validate_pipeline_status(task_row(task_id), result_dir)

    assert status == "failed"
    assert "run_id" in reason

    payload = json.loads((result_dir / server.PIPELINE_STATUS_FILENAME).read_text(encoding="utf-8"))
    payload["run_id"] = "run-1"
    (result_dir / server.PIPELINE_STATUS_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    status, reason, _ = server.validate_pipeline_status(task_row(task_id), result_dir)
    assert status == "failed"
    assert "requested_mode" in reason


@pytest.mark.parametrize(
    "mutation,expected",
    [
        (lambda payload: payload.update(kind="wrong_kind"), "kind"),
        (lambda payload: payload.update(result_dir=str(Path(payload["result_dir"]).parent)), "result_dir"),
        (
            lambda payload: payload["stages"].update(
                mystery={"requested": True, "status": "failed", "reason": "must not be discarded"}
            ),
            "未知请求阶段",
        ),
        (lambda payload: payload["stages"].pop("packaging"), "packaging"),
        (lambda payload: payload["stages"]["packaging"].update(status="skipped"), "packaging"),
        (lambda payload: payload["stages"]["packaging"].update(required_artifacts=[]), "required_artifacts"),
    ],
)
def test_success_manifest_fails_closed_on_identity_unknown_stage_and_packaging(
    isolated: Path, mutation, expected: str
) -> None:
    user_id = add_user()
    task_id = add_task(user_id, status="running")
    artifact = server.expected_web_result_dir(task_id) / "network/qc.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{}", encoding="utf-8")
    result_dir = write_manifest(
        task_id,
        "network",
        "completed",
        {"network": completed_stage("network/qc.json")},
    )
    manifest_path = result_dir / server.PIPELINE_STATUS_FILENAME
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    mutation(payload)
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    status, reason, _ = server.validate_pipeline_status(task_row(task_id), result_dir)

    assert status == "failed"
    assert expected in reason


def test_no_result_is_explicit_terminal_contract(isolated: Path) -> None:
    user_id = add_user()
    task_id = add_task(user_id, status="running", requested_mode="full")
    result_dir = server.expected_web_result_dir(task_id)
    for relative in ("network/qc.json", "docking/validation.json"):
        path = result_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    write_manifest(
        task_id,
        "full",
        "no_result",
        {
            "network": completed_stage("network/qc.json"),
            "docking": completed_stage("docking/validation.json"),
            "reverse": {
                "requested": True,
                "status": "no_result",
                "reason": "没有候选满足预先声明的筛选条件",
                "required_artifacts": [],
                "artifacts": [],
            },
        },
    )

    status, reason, _ = server.validate_pipeline_status(task_row(task_id), result_dir)

    assert status == "no_result"
    assert "没有产生" in reason


def test_zero_exit_without_contract_is_failed_and_gets_ttl(isolated: Path) -> None:
    user_id = add_user()
    task_id = add_task(user_id, status="running")
    server.expected_web_result_dir(task_id).mkdir(parents=True)

    server.scheduler._finalize_task_record(task_id, exit_code=0)

    row = task_row(task_id)
    assert row["status"] == "failed"
    assert row["exit_code"] == 0
    assert row["expires_at"]
    assert "无法验证流水线完成状态" in row["completion_reason"]


def test_valid_contract_finalizes_and_packages(isolated: Path) -> None:
    user_id = add_user()
    task_id = add_task(user_id, status="running")
    result_dir = server.expected_web_result_dir(task_id)
    artifact = result_dir / "network/qc.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{}", encoding="utf-8")
    write_manifest(task_id, "network", "completed", {"network": completed_stage("network/qc.json")})

    server.scheduler._finalize_task_record(task_id, exit_code=0)

    row = task_row(task_id)
    assert row["status"] == "completed"
    assert row["expires_at"]
    assert Path(row["zip_path"]).is_file()


def test_stale_queued_snapshot_cannot_launch_after_cancel(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    user_id = add_user()
    task_id = add_task(user_id)
    stale = task_row(task_id)
    with server.db() as conn:
        conn.execute(
            "UPDATE tasks SET status = 'cancelled', finished_at = ?, expires_at = ? WHERE id = ?",
            (server.now(), server.terminal_expiry(), task_id),
        )
    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **k: pytest.fail("cancelled task was launched"))

    server.scheduler._launch_task(stale, "0-23")

    assert task_row(task_id)["status"] == "cancelled"


def test_launch_failure_is_terminal_and_gets_ttl(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    user_id = add_user()
    task_id = add_task(user_id)
    queued = task_row(task_id)

    def fail_launch(*args, **kwargs):
        raise OSError("fixture launch failure")

    monkeypatch.setattr(server.subprocess, "Popen", fail_launch)

    server.scheduler._launch_task(queued, "0-23")

    row = task_row(task_id)
    assert row["status"] == "failed"
    assert row["expires_at"]
    assert "fixture launch failure" in row["completion_reason"]


def test_init_db_backfills_ttl_for_legacy_terminal_rows(isolated: Path) -> None:
    user_id = add_user("legacy-ttl")
    task_id = add_task(user_id, status="failed", expires_at=None)
    with server.db() as conn:
        conn.execute("UPDATE tasks SET expires_at = NULL WHERE id = ?", (task_id,))

    server.init_db()

    row = task_row(task_id)
    assert row["expires_at"]
    assert server.parse_time(row["expires_at"]) is not None


def test_spawned_process_rollback_stops_after_term(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[object] = []

    class FakeProcess:
        pid = 12345
        returncode: int | None = None

        def poll(self):
            return self.returncode

        def wait(self, timeout):
            events.append(("wait", timeout))
            self.returncode = -int(server.signal.SIGTERM)
            return self.returncode

        def terminate(self):
            events.append("terminate")

        def kill(self):
            events.append("kill")

    if server.os.name == "posix":
        monkeypatch.setattr(
            server.os,
            "killpg",
            lambda pid, sig: events.append(("signal", pid, sig)),
        )

    server.terminate_spawned_process(FakeProcess(), term_timeout=0.1, kill_timeout=0.1)

    if server.os.name == "posix":
        assert ("signal", 12345, server.signal.SIGTERM) in events
        assert not any(event == ("signal", 12345, server.signal.SIGKILL) for event in events)
    else:
        assert "terminate" in events
        assert "kill" not in events


def test_spawned_process_rollback_escalates_to_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[object] = []

    class FakeProcess:
        pid = 23456
        returncode: int | None = None
        waits = 0

        def poll(self):
            return self.returncode

        def wait(self, timeout):
            self.waits += 1
            events.append(("wait", timeout))
            if self.waits == 1:
                raise server.subprocess.TimeoutExpired(cmd="fixture", timeout=timeout)
            self.returncode = -1
            return self.returncode

        def terminate(self):
            events.append("terminate")

        def kill(self):
            events.append("kill")

    if server.os.name == "posix":
        monkeypatch.setattr(
            server.os,
            "killpg",
            lambda pid, sig: events.append(("signal", pid, sig)),
        )

    server.terminate_spawned_process(FakeProcess(), term_timeout=0.1, kill_timeout=0.1)

    if server.os.name == "posix":
        assert ("signal", 23456, server.signal.SIGTERM) in events
        assert ("signal", 23456, server.signal.SIGKILL) in events
    else:
        assert "terminate" in events
        assert "kill" in events


def test_post_spawn_claim_failure_reaps_before_marking_failed(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id = add_user()
    task_id = add_task(user_id)
    events: list[str] = []

    class FakeProcess:
        pid = 34567

    class FakeThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(server.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(
        server,
        "process_identity",
        lambda pid: (_ for _ in ()).throw(RuntimeError("fixture identity failure")),
    )

    def reap_before_db_release(proc) -> None:
        assert task_row(task_id)["status"] == "queued"
        events.append("reaped")

    monkeypatch.setattr(server, "terminate_spawned_process", reap_before_db_release)
    monkeypatch.setattr(server.threading, "Thread", FakeThread)

    server.scheduler._launch_task(task_row(task_id), "0-23")

    row = task_row(task_id)
    assert events == ["reaped"]
    assert row["status"] == "failed"
    assert row["pid"] is None
    assert row["cpu_range"] is None


def test_post_spawn_unreaped_process_remains_stopping_and_holds_slot(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id = add_user()
    task_id = add_task(user_id)

    class FakeProcess:
        pid = 45678

    monkeypatch.setattr(server.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    identity_calls = 0

    def fail_claim_then_recover_identity(pid: int) -> str:
        nonlocal identity_calls
        identity_calls += 1
        if identity_calls == 1:
            raise RuntimeError("fixture identity failure")
        return "linux-starttime:fixture"

    monkeypatch.setattr(server, "process_identity", fail_claim_then_recover_identity)
    monkeypatch.setattr(
        server,
        "terminate_spawned_process",
        lambda proc: (_ for _ in ()).throw(OSError("fixture process still alive")),
    )

    with pytest.raises(RuntimeError, match="未确认退出"):
        server.scheduler._launch_task(task_row(task_id), "0-23")

    row = task_row(task_id)
    assert row["status"] == "stopping"
    assert row["pid"] == 45678
    assert row["process_identity"] == "linux-starttime:fixture"
    assert row["cpu_range"] == "0-23"
    assert row["run_id"]
    assert "未确认退出" in row["completion_reason"]


def test_launch_passes_validated_structured_fields(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    user_id = add_user()
    task_id = add_task(user_id, requested_mode="network+docking")
    with server.db() as conn:
        conn.execute(
            "UPDATE tasks SET herbs = ?, disease = ?, ph = ? WHERE id = ?",
            ("herb-a+herb-b，herb-c", "disease-x", "6.75", task_id),
        )
    captured: dict = {}

    class FakeProcess:
        pid = 12345

    class FakeThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    def fake_popen(*args, **kwargs):
        captured.update(kwargs["env"])
        return FakeProcess()

    monkeypatch.setattr(server.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(server, "process_identity", lambda pid: "linux-starttime:9")
    monkeypatch.setattr(server.threading, "Thread", FakeThread)

    server.scheduler._launch_task(task_row(task_id), "0-23")

    assert captured["NPFLOW_REQUESTED_MODE"] == "network+docking"
    assert captured["NPFLOW_REQUESTED_PH"] == "6.75"
    assert json.loads(captured["NPFLOW_REQUESTED_HERBS_JSON"]) == ["herb-a", "herb-b", "herb-c"]
    assert captured["NPFLOW_REQUESTED_DISEASE"] == "disease-x"
    assert json.loads(captured["NPFLOW_REQUESTED_EXTRA_TARGETS_JSON"]) == []
    assert json.loads(captured["NPFLOW_REQUESTED_EXTRA_LIGANDS_JSON"]) == []
    assert captured["NPFLOW_WEB_RUN_ID"] == task_row(task_id)["run_id"]


@pytest.mark.parametrize("free_gb", [5.0, 10.0, 100.0])
def test_submit_persists_explicit_extras_as_canonical_json(isolated: Path, monkeypatch: pytest.MonkeyPatch, free_gb: float) -> None:
    user_id = add_user()
    with server.db() as conn:
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    handler, captured = handler_with_capture()
    handler._require_user = lambda: user
    handler._rate_limit = lambda *args: True
    handler._client_ip = lambda: "192.0.2.2"
    handler._read_json = lambda *args: {
        "herbs": "herb-a",
        "disease": "disease-x",
        "ph": "7.4",
        "task_mode": "network",
        "extra_targets": "egfr, AKT1, egfr",
        "extra_ligands": "aspirin；quercetin; aspirin",
    }
    monkeypatch.setattr(server, "disk_free_gb", lambda *args: free_gb)

    handler.api_submit_task()

    assert captured[-1][0] == HTTPStatus.CREATED
    row = task_row(captured[-1][1]["task_id"])
    assert json.loads(row["extra_targets_json"]) == ["EGFR", "AKT1"]
    assert json.loads(row["extra_ligands_json"]) == ["aspirin", "quercetin"]


def test_disk_threshold_migrates_legacy_setting(isolated: Path) -> None:
    with server.db() as conn:
        conn.execute("UPDATE settings SET value = '15.0' WHERE key = 'min_free_gb'")
    server.init_db()
    assert float(server.setting("min_free_gb")) == 5.0


@pytest.mark.parametrize("free_bytes", [5 * 1024**3 - 1, 5 * 1024**3, 5 * 1024**3 + 1, 10 * 1024**3])
def test_disk_byte_boundary_controls_submission_and_scheduler(isolated: Path, monkeypatch: pytest.MonkeyPatch, free_bytes: int) -> None:
    user_id = add_user()
    with server.db() as conn:
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    handler, captured = handler_with_capture()
    handler._require_user = lambda: user
    handler._rate_limit = lambda *args: True
    handler._client_ip = lambda: "192.0.2.2"
    handler._read_json = lambda *args: {"herbs": "金银花", "disease": "influenza", "task_mode": "network"}
    monkeypatch.setattr(server.shutil, "disk_usage", lambda path: SimpleNamespace(free=free_bytes))
    handler.api_submit_task()
    allowed = free_bytes >= 5 * 1024**3
    assert captured[-1][0] == (HTTPStatus.CREATED if allowed else HTTPStatus.INSUFFICIENT_STORAGE)
    if not allowed:
        add_task(user_id)  # A previously queued task must also wait for disk recovery.
    launched = []
    monkeypatch.setattr(server.scheduler, "_launch_task", lambda task, slot: launched.append(task["id"]))
    server.scheduler._launch_available()
    assert len(launched) == int(allowed)


def test_disk_setting_migration_preserves_custom_value(isolated: Path) -> None:
    with server.db() as conn:
        conn.execute("UPDATE settings SET value = '8.0' WHERE key = 'min_free_gb'")
    server.init_db()
    server.init_db()
    assert float(server.setting("min_free_gb")) == 8.0


def test_submit_below_five_gb_is_rejected(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    handler, captured = handler_with_capture()
    handler._require_user = lambda: {"id": add_user()}
    handler._rate_limit = lambda *args: True
    handler._client_ip = lambda: "192.0.2.2"
    handler._read_json = lambda *args: pytest.fail("Low disk submission must stop before parsing")
    monkeypatch.setattr(server, "disk_free_gb", lambda *args: 4.99)
    handler.api_submit_task()
    assert captured[-1][0] == HTTPStatus.INSUFFICIENT_STORAGE
    assert "5.0GB" in captured[-1][1]["error"]


def test_reconcile_stopping_task_releases_slot_only_after_exit(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    user_id = add_user()
    task_id = add_task(user_id, status="stopping", pid=43210, process_identity="linux-starttime:1")
    monkeypatch.setattr(server, "process_is_alive", lambda pid, identity="": False)

    server.scheduler._reconcile()

    row = task_row(task_id)
    assert row["status"] == "cancelled"
    assert row["expires_at"]


def test_queued_cancel_is_conditional_and_sets_ttl(isolated: Path) -> None:
    user_id = add_user()
    task_id = add_task(user_id)
    handler, captured = handler_with_capture()

    handler.api_admin_stop(task_id)

    row = task_row(task_id)
    assert captured[-1][0] == HTTPStatus.OK
    assert row["status"] == "cancelled"
    assert row["expires_at"]


def test_cleanup_failure_preserves_references_for_retry(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    user_id = add_user()
    old = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    task_id = add_task(user_id, status="failed", expires_at=old)
    result_dir = server.expected_web_result_dir(task_id)
    result_dir.mkdir(parents=True)
    (result_dir / "partial.txt").write_text("partial", encoding="utf-8")
    log_path = server.LOG_ROOT / f"task_{task_id}.log"
    log_path.write_text("log", encoding="utf-8")
    with server.db() as conn:
        conn.execute(
            "UPDATE tasks SET result_dir = ?, log_path = ? WHERE id = ?",
            (str(result_dir), str(log_path), task_id),
        )
    real_remove = server.remove_task_path

    def fail_result(path: Path) -> None:
        if path == result_dir:
            raise PermissionError("fixture refusal")
        real_remove(path)

    monkeypatch.setattr(server, "remove_task_path", fail_result)

    server.scheduler._cleanup_expired()

    row = task_row(task_id)
    assert row["result_dir"] == str(result_dir)
    assert row["log_path"] is None
    assert "fixture refusal" in row["cleanup_error"]
    assert row["cleanup_retry_at"]
    assert row["cleanup_completed_at"] is None


def test_failed_explicit_delete_is_retried_and_releases_slot(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    user_id = add_user()
    task_id = add_task(user_id, status="running", pid=None)
    result_dir = server.expected_web_result_dir(task_id)
    result_dir.mkdir(parents=True)
    with server.db() as conn:
        conn.execute(
            "UPDATE tasks SET result_dir = ?, cpu_range = '0-23' WHERE id = ?",
            (str(result_dir), task_id),
        )
    real_remove = server.remove_task_path
    monkeypatch.setattr(
        server,
        "remove_task_path",
        lambda path: (_ for _ in ()).throw(PermissionError("fixture refusal")),
    )

    with pytest.raises(PermissionError, match="fixture refusal"):
        server.delete_task_record(task_row(task_id))
    failed = task_row(task_id)
    assert failed["status"] == "deleting"
    assert failed["cleanup_retry_at"]
    assert failed["cpu_range"] is None

    monkeypatch.setattr(server, "remove_task_path", real_remove)
    with server.db() as conn:
        conn.execute("UPDATE tasks SET cleanup_retry_at = ? WHERE id = ?", (server.now(), task_id))
    server.scheduler._retry_deleting()

    with server.db() as conn:
        assert conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone() is None
    assert not result_dir.exists()


def test_deleting_old_terminal_task_does_not_conflict_with_new_active_task(isolated: Path) -> None:
    user_id = add_user()
    old_task = add_task(user_id, status="completed", run_id="old-terminal-run")
    active_task = add_task(user_id, status="queued", run_id="new-active-run")

    server.delete_task_record(task_row(old_task))

    with server.db() as conn:
        assert conn.execute("SELECT 1 FROM tasks WHERE id = ?", (old_task,)).fetchone() is None
        active = conn.execute("SELECT status FROM tasks WHERE id = ?", (active_task,)).fetchone()
    assert active and active["status"] == "queued"


def test_short_rate_window_does_not_delete_long_window_history(isolated: Path) -> None:
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    with server.db() as conn:
        conn.executemany(
            "INSERT INTO rate_events(kind, identity, created_at) VALUES('register', '198.51.100.2', ?)",
            [(old,), (old,), (old,)],
        )
    handler = object.__new__(server.Handler)

    assert handler._rate_limit("login", "198.51.100.2:user", 12, 15 * 60)
    assert not handler._rate_limit("register", "198.51.100.2", 3, 24 * 60 * 60)
    with server.db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM rate_events WHERE kind = 'register'").fetchone()[0]
    assert count == 3


def test_rate_limit_check_and_insert_are_atomic(isolated: Path) -> None:
    handler = object.__new__(server.Handler)
    with ThreadPoolExecutor(max_workers=8) as pool:
        allowed = list(pool.map(lambda _: handler._rate_limit("login", "same-identity", 5, 900), range(20)))

    assert sum(allowed) == 5
    with server.db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM rate_events WHERE kind = 'login' AND identity = 'same-identity'"
        ).fetchone()[0]
    assert count == 5


def test_xff_is_ignored_unless_peer_is_trusted(monkeypatch: pytest.MonkeyPatch) -> None:
    handler = object.__new__(server.Handler)
    handler.headers = Message()
    handler.headers["X-Forwarded-For"] = "203.0.113.10"
    handler.client_address = ("198.51.100.20", 12345)
    handler.connection = object()
    monkeypatch.setattr(server, "TRUSTED_PROXY_NETWORKS", ())
    assert handler._client_ip() == "198.51.100.20"
    assert not handler._is_secure_request()

    handler.client_address = ("127.0.0.1", 12345)
    handler.headers["X-Forwarded-Proto"] = "https"
    monkeypatch.setattr(server, "TRUSTED_PROXY_NETWORKS", (ipaddress.ip_network("127.0.0.1/32"),))
    assert handler._client_ip() == "203.0.113.10"
    assert handler._is_secure_request()


def test_cleartext_auth_is_rejected_and_cookie_is_secure(monkeypatch: pytest.MonkeyPatch) -> None:
    handler, captured = handler_with_capture()
    monkeypatch.setattr(server, "ALLOW_INSECURE_AUTH", False)
    handler._is_secure_request = lambda: False

    assert not handler._require_secure_auth()
    assert captured[-1][0] == HTTPStatus.UPGRADE_REQUIRED
    assert "Secure" in handler._session_cookie("value", 60)
    admin_cookie = handler._admin_session_cookie("value", 60)
    assert "Secure" in admin_cookie
    assert "HttpOnly" in admin_cookie
    assert "SameSite=Strict" in admin_cookie


def test_admin_login_exchanges_posted_token_for_cookie(isolated: Path) -> None:
    handler, captured = handler_with_capture()
    handler._require_secure_auth = lambda: True
    handler._send = lambda status, body=b"", content_type="application/json", headers=None: captured.append(
        (status, body, headers)
    )
    handler._read_json = lambda *args: {"token": server.setting("admin_token")}
    handler._rate_limit = lambda *args: True
    handler._client_ip = lambda: "192.0.2.1"

    handler.api_admin_login()

    status, payload, headers = captured[-1]
    assert status == HTTPStatus.OK
    assert payload == {"ok": True}
    assert headers and headers["Set-Cookie"].startswith("npflow_admin=")
    assert server.setting("admin_token") not in headers["Set-Cookie"]


def test_admin_cookie_authenticates_and_rotation_invalidates_session(isolated: Path) -> None:
    session = server.sign_admin_session()
    handler = object.__new__(server.Handler)
    handler.headers = Message()
    handler.headers["Cookie"] = f"npflow_admin={session}"

    assert handler._admin_authenticated()
    with server.db() as conn:
        conn.execute("UPDATE settings SET value = ? WHERE key = 'admin_token'", ("rotated-admin-token",))
    assert not handler._admin_authenticated()


def test_admin_cookie_authorizes_download_lookup(isolated: Path) -> None:
    user_id = add_user()
    task_id = add_task(user_id, status="completed")
    missing_zip = server.ARTIFACT_ROOT / f"task_{task_id}" / "npflow_result.zip"
    with server.db() as conn:
        conn.execute("UPDATE tasks SET zip_path = ? WHERE id = ?", (str(missing_zip), task_id))
    handler, captured = handler_with_capture()
    handler.headers = Message()
    handler.headers["Cookie"] = f"npflow_admin={server.sign_admin_session()}"
    handler._require_secure_auth = lambda: True
    handler._send = lambda status, body=b"", content_type="application/json", headers=None: captured.append(
        (status, body, headers)
    )

    handler.download(task_id)

    assert captured[-1][0] == HTTPStatus.GONE


def test_admin_page_uses_fixed_path_and_rejects_legacy_token_path(isolated: Path) -> None:
    handler, captured = handler_with_capture()
    handler._require_secure_auth = lambda: True
    handler._send = lambda status, body=b"", content_type="application/json", headers=None: captured.append(
        (status, body, headers)
    )
    served: list[Path] = []
    handler._static_file = lambda path: served.append(path)

    handler.path = "/admin"
    handler.do_GET()
    assert served == [server.STATIC_ROOT / "admin.html"]

    handler.path = f"/admin/{server.setting('admin_token')}"
    handler.do_GET()
    assert captured[-1][0] == HTTPStatus.NOT_FOUND


def test_admin_frontend_never_reads_or_sends_url_token() -> None:
    static = Path(server.__file__).parent / "static"
    javascript = (static / "admin.js").read_text(encoding="utf-8")
    html = (static / "admin.html").read_text(encoding="utf-8")
    assert "location.pathname" not in javascript
    assert "X-Admin-Token" not in javascript
    assert 'action="/api/admin/login"' in html
    assert 'method="post"' in html


def test_read_json_rejects_arrays_and_invalid_json() -> None:
    handler = object.__new__(server.Handler)
    handler.headers = Message()
    handler.headers["Content-Type"] = "application/json"
    handler.headers["Content-Length"] = "2"
    handler.rfile = io.BytesIO(b"[]")
    with pytest.raises(server.RequestError) as exc_info:
        handler._read_json()
    assert exc_info.value.status == HTTPStatus.BAD_REQUEST

    handler.headers.replace_header("Content-Length", "1")
    handler.rfile = io.BytesIO(b"{")
    with pytest.raises(server.RequestError) as exc_info:
        handler._read_json()
    assert exc_info.value.status == HTTPStatus.BAD_REQUEST


@pytest.mark.parametrize("value", ["nan", "NaN", "inf", "-inf", "-0.1", "14.1", "not-a-number"])
def test_ph_must_be_finite_and_in_scientific_range(value: str) -> None:
    with pytest.raises(ValueError):
        server.normalize_ph(value)


def test_ph_is_normalized() -> None:
    assert server.normalize_ph("07.4000") == "7.4"
    assert server.normalize_ph("-0") == "0"


def test_queued_log_returns_empty_json_contract(isolated: Path) -> None:
    user_id = add_user()
    task_id = add_task(user_id)
    handler, captured = handler_with_capture()
    with server.db() as conn:
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    handler._require_user = lambda: user

    handler.api_task_log(task_id)

    assert captured[-1][0] == HTTPStatus.OK
    assert captured[-1][1]["log"] == ""


def test_admin_restart_conflict_returns_409(isolated: Path) -> None:
    user_id = add_user()
    old_task = add_task(user_id, status="completed")
    add_task(user_id, status="queued", run_id="run-2")
    handler, captured = handler_with_capture()

    handler.api_admin_restart(old_task)

    assert captured[-1][0] == HTTPStatus.CONFLICT


def test_sensitive_log_fields_are_redacted_and_legacy_file_is_rewritten(tmp_path: Path) -> None:
    original = (
        '"GET /api/login?username=u&password=swordfish&invite_code=invite HTTP/1.1" 404\n'
        '{"password":"another secret with spaces","token":"header-secret"} Authorization: Bearer bearer-secret\n'
        'Cookie: npflow_admin=admin-session-secret; npflow_session=user-session-secret\n'
        'GET /admin/legacy-url-token HTTP/1.1\n'
    )
    assert "swordfish" not in server.redact_sensitive_text(original)
    assert "another secret with spaces" not in server.redact_sensitive_text(original)
    assert "bearer-secret" not in server.redact_sensitive_text(original)
    assert "admin-session-secret" not in server.redact_sensitive_text(original)
    assert "legacy-url-token" not in server.redact_sensitive_text(original)
    path = tmp_path / "access.log"
    path.write_text(original, encoding="utf-8")

    changed = sanitize_access_logs.sanitize_file(path)
    text = path.read_text(encoding="utf-8")

    assert changed == 4
    assert "swordfish" not in text
    assert "another secret with spaces" not in text
    assert "bearer-secret" not in text
    assert "admin-session-secret" not in text
    assert "legacy-url-token" not in text
    assert text.count("[REDACTED]") == 8


def test_auth_form_has_explicit_post_method() -> None:
    html = (Path(server.__file__).parent / "static/index.html").read_text(encoding="utf-8")
    assert '<form id="authForm" class="auth-card" method="post"' in html
    assert 'action="/api/login"' in html


def test_first_init_generates_invite_and_query_indexes(isolated: Path) -> None:
    invite = server.setting("invite_code")
    assert len(invite) >= 20
    with server.db() as conn:
        indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {
        "idx_tasks_user_created",
        "idx_task_events_task_id",
        "idx_task_artifacts_task_id",
        "idx_task_artifacts_unique",
        "idx_task_resources_task_id",
    } <= indexes


def test_only_known_legacy_invite_is_rotated(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    legacy = "fixture-known-default"
    custom = "fixture-custom-value"
    monkeypatch.setattr(server, "LEGACY_INVITE_CODE_SHA256", server.hashlib.sha256(legacy.encode()).hexdigest())
    with server.db() as conn:
        conn.execute("UPDATE settings SET value = ? WHERE key = 'invite_code'", (legacy,))
        assert server.migrate_legacy_invite_code(conn)
        rotated = conn.execute("SELECT value FROM settings WHERE key = 'invite_code'").fetchone()[0]
        assert rotated != legacy

        conn.execute("UPDATE settings SET value = ? WHERE key = 'invite_code'", (custom,))
        assert not server.migrate_legacy_invite_code(conn)
        preserved = conn.execute("SELECT value FROM settings WHERE key = 'invite_code'").fetchone()[0]
    assert preserved == custom


def test_url_era_admin_token_is_rotated_exactly_once(isolated: Path) -> None:
    with server.db() as conn:
        conn.execute("DELETE FROM settings WHERE key = ?", (server.ADMIN_COOKIE_MIGRATION_KEY,))
        conn.execute("UPDATE settings SET value = 'url-era-token' WHERE key = 'admin_token'")
        assert server.migrate_admin_token_for_cookie_auth(conn)
        first = conn.execute("SELECT value FROM settings WHERE key = 'admin_token'").fetchone()[0]
        assert first != "url-era-token"
        assert not server.migrate_admin_token_for_cookie_auth(conn)
        second = conn.execute("SELECT value FROM settings WHERE key = 'admin_token'").fetchone()[0]
    assert second == first


def test_pre_https_user_sessions_are_invalidated_exactly_once(isolated: Path) -> None:
    with server.db() as conn:
        conn.execute("DELETE FROM settings WHERE key = ?", (server.SESSION_COOKIE_MIGRATION_KEY,))
        conn.execute("UPDATE settings SET value = 'pre-https-secret' WHERE key = 'session_secret'")
        assert server.migrate_session_secret_for_secure_auth(conn)
        first = conn.execute("SELECT value FROM settings WHERE key = 'session_secret'").fetchone()[0]
        assert first != "pre-https-secret"
        assert not server.migrate_session_secret_for_secure_auth(conn)
        second = conn.execute("SELECT value FROM settings WHERE key = 'session_secret'").fetchone()[0]
    assert second == first
