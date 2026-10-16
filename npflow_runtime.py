"""Stage deadlines and complete logs, preserving the Web-owned process group."""
from __future__ import annotations

import functools
import json
import os
import signal
import subprocess
import time
import uuid
from pathlib import Path


def remaining_timeout(limit):
    deadline = os.environ.get("NPFLOW_STAGE_DEADLINE")
    if not deadline:
        return limit
    remaining = float(deadline) - time.time()
    if remaining <= 0:
        raise TimeoutError("stage_deadline_exhausted:" + os.environ.get("NPFLOW_STAGE_NAME", "unknown"))
    return min(float(limit), remaining)


def stage_budget(name, seconds):
    def decorate(function):
        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            keys = ("NPFLOW_STAGE_NAME", "NPFLOW_STAGE_DEADLINE")
            old = {key: os.environ.get(key) for key in keys}
            deadline = time.time() + seconds
            inherited = (kwargs.get("context") or {}).get(name + "_deadline")
            if inherited is not None:
                deadline = min(deadline, float(inherited))
            os.environ.update({keys[0]: name, keys[1]: str(deadline)})
            try:
                return function(*args, **kwargs)
            finally:
                for key, value in old.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
        return wrapped
    return decorate


def _process_start(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def _signal_descendant(pid, start, signum):
    if start is not None and _process_start(pid) == start:
        try:
            os.kill(pid, signum)
        except ProcessLookupError:
            pass


def _descendants(pid):
    result = []
    try:
        children = Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
        for child in children:
            child = int(child)
            result.extend(_descendants(child))
            result.append((child, _process_start(child)))
    except OSError:
        pass
    return result


def run_logged(argv, *, cwd, timeout, log_dir):
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    stem = os.environ.get("NPFLOW_STAGE_NAME", "command") + "_" + uuid.uuid4().hex[:10]
    path = log_dir / (stem + ".log")
    limit = remaining_timeout(timeout)
    timed_out = False
    started = time.time()
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    root = str(Path(__file__).resolve().parent)
    env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
    print(f"NPFLOW_STAGE_LOG={path}", flush=True)
    with path.open("w", encoding="utf-8") as handle:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=handle, stderr=subprocess.STDOUT)
        try:
            process.wait(timeout=limit)
        except subprocess.TimeoutExpired:
            timed_out = True
            descendants = _descendants(process.pid) if os.name == "posix" else []
            for pid, start in descendants:
                _signal_descendant(pid, start, signal.SIGTERM)
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            for pid, start in descendants:
                _signal_descendant(pid, start, signal.SIGKILL)
            handle.write(f"\nSTAGE_TIMEOUT: hard limit {limit:.1f}s exceeded; partial results retained\n")
    output = path.read_text(encoding="utf-8", errors="replace")
    code = 124 if timed_out else process.returncode
    metadata = {"stage": os.environ.get("NPFLOW_STAGE_NAME"), "exit_code": code,
                "timeout": timed_out, "runtime_seconds": time.time() - started, "full_log": str(path)}
    path.with_suffix(".json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return subprocess.CompletedProcess(argv, code, output, "")
