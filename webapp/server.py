#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import math
import mimetypes
import os
import re
import secrets
import shutil
import signal
import sqlite3
import ssl
import subprocess
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(os.environ.get("NPFLOW_ROOT", Path(__file__).resolve().parents[1])).resolve()
WEB_ROOT = Path(__file__).resolve().parent
STATIC_ROOT = WEB_ROOT / "static"
DB_PATH = Path(os.environ.get("NPFLOW_WEB_DB", WEB_ROOT / "npflow_web.sqlite3"))
ARTIFACT_ROOT = Path(os.environ.get("NPFLOW_WEB_ARTIFACTS", WEB_ROOT / "artifacts"))
LOG_ROOT = Path(os.environ.get("NPFLOW_WEB_LOGS", WEB_ROOT / "logs"))
PYTHON = ROOT / ".venv/bin/python"
NPFLOW_AGENT = ROOT / "npflow_agent.py"

DEFAULT_PORT = int(os.environ.get("NPFLOW_WEB_PORT", "18088"))
DEFAULT_HTTPS_PORT = int(os.environ.get("NPFLOW_WEB_HTTPS_PORT", "18443"))
HTTPS_CERT = Path(os.environ.get("NPFLOW_TLS_CERT", WEB_ROOT / "certs" / "npflow-ip.crt"))
HTTPS_KEY = Path(os.environ.get("NPFLOW_TLS_KEY", WEB_ROOT / "certs" / "npflow-ip.key"))
DEFAULT_CONCURRENCY = int(os.environ.get("NPFLOW_MAX_CONCURRENCY", "4"))
DEFAULT_RETENTION_DAYS = int(os.environ.get("NPFLOW_RETENTION_DAYS", "2"))
DEFAULT_MIN_FREE_GB = float(os.environ.get("NPFLOW_MIN_FREE_GB", "5"))
CPU_SLOTS = ["0-23", "24-47", "48-71", "72-95"]
TASK_CPU_LIMIT = int(os.environ.get("NPFLOW_TASK_CPU_LIMIT", "24"))
TASK_MEMORY_GB = int(os.environ.get("NPFLOW_TASK_MEMORY_GB", "36"))
TASK_RESOURCE_LABEL = "12 核 / 24 线程 / 内存不做硬限制"
INVITE_CODE = os.environ.get("NPFLOW_INVITE_CODE", "").strip()
LEGACY_INVITE_CODE_SHA256 = "a586d9fd4b6bcf75f5a6a0c2a0efd3bab706632af00d3f6922859fba50deea60"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 7
ADMIN_SESSION_TTL_SECONDS = 60 * 60 * 8
ADMIN_COOKIE_MIGRATION_KEY = "admin_cookie_auth_v1_rotated"
SESSION_COOKIE_MIGRATION_KEY = "secure_user_cookie_v1_rotated"
MAX_JSON_BODY_BYTES = 24_000
MAX_SUBMIT_BODY_BYTES = 16_000
FIELD_LIMITS = {
    "herbs": 300,
    "disease": 160,
    "ph": 16,
    "task_mode": 32,
    "extra_targets": 600,
    "extra_ligands": 600,
    "task_notes": 1200,
}
VALID_TASK_MODES = {"auto", "network", "network+docking", "full"}
PIPELINE_TASK_MODES = {"auto", "network", "network+docking", "full", "docking", "docking+reverse", "reverse"}
PIPELINE_STATUS_FILENAME = "pipeline_status.json"
PIPELINE_STATUS_MAX_BYTES = 1_000_000
NETWORK_RUN_MANIFEST_FILENAME = "npflow_run_manifest.json"
NETWORK_RUN_MANIFEST_MAX_BYTES = 128_000
TERMINAL_STATUSES = ("completed", "no_result", "failed", "cancelled")
SUCCESS_STATUSES = ("completed", "no_result")
STOP_GRACE_SECONDS = int(os.environ.get("NPFLOW_STOP_GRACE_SECONDS", "30"))
CLEANUP_RETRY_SECONDS = int(os.environ.get("NPFLOW_CLEANUP_RETRY_SECONDS", "300"))
RATE_EVENT_RETENTION_SECONDS = 48 * 60 * 60
HTTP_BIND_HOST = os.environ.get("NPFLOW_HTTP_BIND_HOST", "127.0.0.1")
HTTPS_BIND_HOST = os.environ.get("NPFLOW_HTTPS_BIND_HOST", "0.0.0.0")


def env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


ALLOW_INSECURE_AUTH = env_flag("NPFLOW_ALLOW_INSECURE_AUTH", False)


def trusted_proxy_networks() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for item in os.environ.get("NPFLOW_TRUSTED_PROXIES", "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError as exc:
            raise RuntimeError(f"Invalid NPFLOW_TRUSTED_PROXIES entry: {item}") from exc
    return tuple(networks)


TRUSTED_PROXY_NETWORKS = trusted_proxy_networks()
TASK_LIFECYCLE_LOCK = threading.RLock()


class RequestError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


_SENSITIVE_QUERY_RE = re.compile(
    r"(?i)([?&](?:password|passwd|pwd|token|admin_token|access_token|invite_code|session|npflow_session|npflow_admin)=)[^&\s\"']*"
)
_SENSITIVE_QUOTED_RE = re.compile(
    r'''(?is)(["']?(?:password|passwd|pwd|token|admin_token|access_token|invite_code|session|npflow_session|npflow_admin)["']?\s*[:=]\s*)(["'])(.*?)(?<!\\)\2'''
)
_SENSITIVE_BODY_RE = re.compile(
    r'''(?ix)(\b(?:password|passwd|pwd|token|admin_token|access_token|invite_code|session|npflow_session|npflow_admin)\s*[:=]\s*)[^\s,&}\"']*'''
)
_COOKIE_RE = re.compile(r"(?i)((?:npflow_session|npflow_admin)=)[^;\s\"']+")
_LEGACY_ADMIN_PATH_RE = re.compile(r"(?i)(?<!/api)(/admin/)[^/?\s\"']+")
_AUTHORIZATION_RE = re.compile(r"(?i)(authorization\s*:\s*(?:basic|bearer)\s+)[^\s\"']+")


def redact_sensitive_text(value: Any) -> str:
    text = str(value)
    text = _SENSITIVE_QUERY_RE.sub(r"\1[REDACTED]", text)
    text = _SENSITIVE_QUOTED_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]{match.group(2)}", text)
    text = _SENSITIVE_BODY_RE.sub(r"\1[REDACTED]", text)
    text = _COOKIE_RE.sub(r"\1[REDACTED]", text)
    text = _LEGACY_ADMIN_PATH_RE.sub(r"\1[REDACTED]", text)
    return _AUTHORIZATION_RE.sub(r"\1[REDACTED]", text)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def terminal_expiry() -> str:
    retention_days = int(setting("retention_days", str(DEFAULT_RETENTION_DAYS)))
    return (datetime.now(timezone.utc) + timedelta(days=retention_days)).isoformat(timespec="seconds")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def json_dumps(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def absolute_path(path: Path) -> Path:
    """Return a normalized absolute path without following the final symlink."""
    return Path(os.path.abspath(os.fspath(path)))


def normalize_ph(value: str) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("pH 必须是数字") from exc
    if not math.isfinite(number) or not 0 <= number <= 14:
        raise ValueError("pH 必须是 0 到 14 之间的有限数值")
    if number == 0:
        number = 0.0
    return format(number, ".12g")


def requested_herbs(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[+,，、;；\r\n]+", value) if item.strip()]


def requested_extras(value: str, *, uppercase: bool = False) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    for raw in re.split(r"[,，、;；\r\n]+", value):
        item = raw.strip()
        if not item:
            continue
        if uppercase:
            item = item.upper()
        key = item.casefold()
        if key not in seen:
            seen.add(key)
            items.append(item)
    return items


def parse_stored_string_list(value: Any, field: str) -> list[str]:
    try:
        payload = json.loads(str(value or "[]"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field} 不是有效 JSON") from exc
    if not isinstance(payload, list) or any(not isinstance(item, str) or not item.strip() for item in payload):
        raise ValueError(f"{field} 必须是非空字符串数组")
    return [item.strip() for item in payload]


def ensure_dirs() -> None:
    WEB_ROOT.mkdir(parents=True, exist_ok=True)
    STATIC_ROOT.mkdir(parents=True, exist_ok=True)
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    LOG_ROOT.mkdir(parents=True, exist_ok=True)


def migrate_legacy_invite_code(conn: sqlite3.Connection) -> bool:
    """Rotate only the known, publicly documented legacy default invite code."""
    row = conn.execute("SELECT value FROM settings WHERE key = 'invite_code'").fetchone()
    if not row:
        return False
    current = str(row["value"])
    fingerprint = hashlib.sha256(current.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(fingerprint, LEGACY_INVITE_CODE_SHA256):
        return False
    replacement = secrets.token_urlsafe(18)
    updated = conn.execute(
        "UPDATE settings SET value = ? WHERE key = 'invite_code' AND value = ?",
        (replacement, current),
    )
    return updated.rowcount == 1


def migrate_admin_token_for_cookie_auth(conn: sqlite3.Connection) -> bool:
    """Rotate the URL-era token exactly once; never expose either value."""
    marker = conn.execute("SELECT 1 FROM settings WHERE key = ?", (ADMIN_COOKIE_MIGRATION_KEY,)).fetchone()
    if marker:
        return False
    updated = conn.execute(
        "UPDATE settings SET value = ? WHERE key = 'admin_token'",
        (secrets.token_urlsafe(32),),
    )
    if updated.rowcount != 1:
        return False
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?)",
        (ADMIN_COOKIE_MIGRATION_KEY, now()),
    )
    return True


def migrate_session_secret_for_secure_auth(conn: sqlite3.Connection) -> bool:
    """Invalidate sessions that may predate the HTTPS-only cookie policy once."""
    marker = conn.execute("SELECT 1 FROM settings WHERE key = ?", (SESSION_COOKIE_MIGRATION_KEY,)).fetchone()
    if marker:
        return False
    updated = conn.execute(
        "UPDATE settings SET value = ? WHERE key = 'session_secret'",
        (secrets.token_urlsafe(48),),
    )
    if updated.rowcount != 1:
        return False
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?)",
        (SESSION_COOKIE_MIGRATION_KEY, now()),
    )
    return True


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    ensure_dirs()
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                email TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                email TEXT NOT NULL,
                herbs TEXT NOT NULL,
                disease TEXT NOT NULL,
                ph TEXT NOT NULL DEFAULT '7.4',
                input_text TEXT NOT NULL,
                status TEXT NOT NULL,
                pid INTEGER,
                cpu_range TEXT,
                log_path TEXT,
                result_dir TEXT,
                zip_path TEXT,
                warning TEXT,
                exit_code INTEGER,
                attempt_of INTEGER,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                expires_at TEXT,
                requested_mode TEXT NOT NULL DEFAULT 'auto',
                run_id TEXT,
                process_identity TEXT,
                stopping_started_at TEXT,
                stage_status TEXT,
                completion_reason TEXT,
                cleanup_error TEXT,
                cleanup_retry_at TEXT,
                cleanup_completed_at TEXT,
                extra_targets_json TEXT NOT NULL DEFAULT '[]',
                extra_ligands_json TEXT NOT NULL DEFAULT '[]',
                FOREIGN KEY(user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS task_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES tasks(id)
            );
            CREATE TABLE IF NOT EXISTS task_artifacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                path TEXT NOT NULL,
                label TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES tasks(id)
            );
            CREATE TABLE IF NOT EXISTS task_resources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                run_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES tasks(id),
                UNIQUE(task_id, run_id, path)
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rate_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                identity TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_rate_events_kind_identity_created
            ON rate_events(kind, identity, created_at);
            CREATE INDEX IF NOT EXISTS idx_tasks_user_created
            ON tasks(user_id, created_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_task_events_task_id
            ON task_events(task_id, id);
            CREATE INDEX IF NOT EXISTS idx_task_artifacts_task_id
            ON task_artifacts(task_id, id);
            CREATE INDEX IF NOT EXISTS idx_task_resources_task_id
            ON task_resources(task_id, id);
            """
        )
        task_columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        migrations = {
            "requested_mode": "TEXT NOT NULL DEFAULT 'auto'",
            "run_id": "TEXT",
            "process_identity": "TEXT",
            "stopping_started_at": "TEXT",
            "stage_status": "TEXT",
            "completion_reason": "TEXT",
            "cleanup_error": "TEXT",
            "cleanup_retry_at": "TEXT",
            "cleanup_completed_at": "TEXT",
            "extra_targets_json": "TEXT NOT NULL DEFAULT '[]'",
            "extra_ligands_json": "TEXT NOT NULL DEFAULT '[]'",
        }
        for column, declaration in migrations.items():
            if column not in task_columns:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} {declaration}")
        conn.execute("DROP INDEX IF EXISTS idx_tasks_one_active_per_user")
        conn.execute(
            """
            CREATE UNIQUE INDEX idx_tasks_one_active_per_user
            ON tasks(user_id)
            WHERE status IN ('queued', 'running', 'stopping')
            """
        )
        duplicate_artifact = conn.execute(
            """
            SELECT 1 FROM task_artifacts
            GROUP BY task_id, kind, path HAVING COUNT(*) > 1 LIMIT 1
            """
        ).fetchone()
        if not duplicate_artifact:
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_task_artifacts_unique
                ON task_artifacts(task_id, kind, path)
                """
            )
        defaults = {
            "max_concurrency": str(DEFAULT_CONCURRENCY),
            "retention_days": str(DEFAULT_RETENTION_DAYS),
            "min_free_gb": str(DEFAULT_MIN_FREE_GB),
            "invite_code": INVITE_CODE or secrets.token_urlsafe(18),
            "admin_token": secrets.token_urlsafe(24),
            "session_secret": secrets.token_urlsafe(48),
        }
        for key, value in defaults.items():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (key, value))
        conn.execute(
            "UPDATE settings SET value = ? WHERE key = 'min_free_gb' AND value IN ('15', '15.0')",
            (str(DEFAULT_MIN_FREE_GB),),
        )
        migrate_legacy_invite_code(conn)
        migrate_admin_token_for_cookie_auth(conn)
        migrate_session_secret_for_secure_auth(conn)
        conn.execute("UPDATE settings SET value = ? WHERE key = 'max_concurrency' AND value = '3'", (str(DEFAULT_CONCURRENCY),))
        conn.execute("UPDATE settings SET value = ? WHERE key = 'retention_days' AND value = '7'", (str(DEFAULT_RETENTION_DAYS),))
        retention_value = conn.execute(
            "SELECT value FROM settings WHERE key = 'retention_days'"
        ).fetchone()
        try:
            retention_days = int(retention_value["value"]) if retention_value else DEFAULT_RETENTION_DAYS
        except (TypeError, ValueError):
            retention_days = DEFAULT_RETENTION_DAYS
        legacy_expiry = (datetime.now(timezone.utc) + timedelta(days=retention_days)).isoformat(timespec="seconds")
        conn.execute(
            """
            UPDATE tasks SET expires_at = ?
            WHERE status IN ('completed', 'no_result', 'failed', 'cancelled')
              AND (expires_at IS NULL OR trim(expires_at) = '')
            """,
            (legacy_expiry,),
        )


def setting(key: str, default: str = "") -> str:
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else default


def add_event(task_id: int, level: str, message: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO task_events(task_id, level, message, created_at) VALUES(?, ?, ?, ?)",
            (task_id, level, redact_sensitive_text(message), now()),
        )


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    iterations = 260_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations,
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iter_text, salt_text, digest_text = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iter_text))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def sign_session(user_id: int) -> str:
    expires = int(time.time()) + SESSION_TTL_SECONDS
    payload = f"{user_id}:{expires}"
    secret = setting("session_secret")
    sig = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def verify_session(value: str | None) -> int | None:
    if not value:
        return None
    try:
        user_text, expires_text, sig = value.split(":", 2)
        payload = f"{user_text}:{expires_text}"
        expected = hmac.new(setting("session_secret").encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected) or int(expires_text) < time.time():
            return None
        return int(user_text)
    except Exception:
        return None


def _admin_session_key() -> bytes:
    material = f"{setting('session_secret')}\0{setting('admin_token')}"
    return hashlib.sha256(material.encode("utf-8")).digest()


def sign_admin_session() -> str:
    expires = int(time.time()) + ADMIN_SESSION_TTL_SECONDS
    nonce = secrets.token_urlsafe(18)
    payload = f"admin:{expires}:{nonce}"
    signature = hmac.new(_admin_session_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}:{signature}"


def verify_admin_session(value: str | None) -> bool:
    if not value:
        return False
    try:
        role, expires_text, nonce, signature = value.split(":", 3)
        payload = f"{role}:{expires_text}:{nonce}"
        expected = hmac.new(_admin_session_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return (
            role == "admin"
            and bool(nonce)
            and int(expires_text) >= time.time()
            and hmac.compare_digest(signature, expected)
        )
    except (TypeError, ValueError):
        return False


def disk_free_gb(path: Path = ROOT) -> float:
    usage = shutil.disk_usage(path)
    # Threshold checks must use full precision, not rounded display values.
    return usage.free / (1024**3)


def task_to_dict(row: sqlite3.Row, *, admin: bool = False) -> dict[str, Any]:
    data = {key: row[key] for key in row.keys()}
    try:
        data["stage_status"] = json.loads(data.get("stage_status") or "{}")
    except (TypeError, json.JSONDecodeError):
        data["stage_status"] = {}
    for field in ("extra_targets_json", "extra_ligands_json"):
        try:
            data[field.removesuffix("_json")] = parse_stored_string_list(data.get(field), field)
        except ValueError:
            data[field.removesuffix("_json")] = []
        data.pop(field, None)
    if not admin:
        data.pop("pid", None)
        data.pop("cpu_range", None)
        data.pop("process_identity", None)
        data.pop("run_id", None)
    data["download_url"] = f"/download/{row['id']}" if row["zip_path"] else ""
    data["cleaned"] = bool(row["cleanup_completed_at"])
    return data


def queue_position_map(conn: sqlite3.Connection) -> dict[int, dict[str, int]]:
    rows = conn.execute("SELECT id FROM tasks WHERE status = 'queued' ORDER BY id").fetchall()
    return {
        int(row["id"]): {"queue_position": index + 1, "queue_ahead": index}
        for index, row in enumerate(rows)
    }


def apply_queue_metadata(task: dict[str, Any], positions: dict[int, dict[str, int]]) -> dict[str, Any]:
    if task.get("status") == "queued":
        task.update(positions.get(int(task["id"]), {"queue_position": 1, "queue_ahead": 0}))
    return task


def make_input_text(
    herbs: str,
    disease: str,
    ph: str,
    task_mode: str = "auto",
    extra_targets: str = "",
    extra_ligands: str = "",
    task_notes: str = "",
) -> str:
    notes = task_notes.strip()
    mode = (task_mode or "auto").strip().lower()
    mode_text = {
        "network": "只跑网药",
        "network+docking": "跑网药和分子对接，不跑反向设计",
        "full": "跑全流程",
    }.get(mode)
    if not mode_text:
        mode_text = "请根据补充说明自动识别任务模式" if notes else "跑全流程"

    parts = [f"中药={herbs}", f"疾病={disease}", f"pH={ph}", mode_text]
    if extra_targets.strip():
        parts.append(f"额外加入靶点/蛋白：{extra_targets.strip()}")
    if extra_ligands.strip():
        parts.append(f"额外加入配体：{extra_ligands.strip()}")
    if notes:
        parts.append(f"用户补充：{notes}")
    return "，".join(parts)


def clean_limited_field(name: str, value: Any) -> str:
    text = str(value or "").strip()
    limit = FIELD_LIMITS[name]
    if len(text) > limit:
        raise ValueError(f"{name} 超过 {limit} 个字符")
    if any((ord(ch) < 32 and ch not in "\t\n\r") for ch in text):
        raise ValueError(f"{name} 包含不可见控制字符")
    return text


def expected_web_result_dir(task_id: int) -> Path:
    return absolute_path(ROOT / "results" / "web_tasks" / f"task_{task_id}")


def is_expected_web_result_dir(path: Path, task_id: int) -> bool:
    try:
        return absolute_path(path) == expected_web_result_dir(task_id)
    except OSError:
        return False


def warning_from_sources(log_path: Path, result_dir: Path | None = None) -> str:
    paths: list[Path] = []
    if not log_path.is_file():
        paths = []
    else:
        paths.append(log_path)
    if result_dir and result_dir.exists():
        paths.extend(result_dir.glob("**/enrichment_summary.json"))
        paths.extend(result_dir.glob("**/qc_report.json"))
        paths.extend(result_dir.glob("**/network_pharmacology_report.md"))
    for path in paths:
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 2_000_000:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "富集 API 暂不可用" in text:
            return "富集 API 暂不可用，已降级继续"
    return ""


def read_log_tail(path: Path, max_lines: int, max_bytes: int = 512_000) -> str:
    if path.is_symlink() or not path.is_file():
        return ""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - max_bytes))
        data = handle.read(max_bytes)
    if size > max_bytes:
        data = data.split(b"\n", 1)[-1]
    text = data.decode("utf-8", errors="ignore")
    return redact_sensitive_text("\n".join(text.splitlines()[-max_lines:]))


def task_delete_roots() -> list[Path]:
    return [
        absolute_path(ROOT / "results" / "web_tasks"),
        absolute_path(ARTIFACT_ROOT),
        absolute_path(LOG_ROOT),
    ]


def network_runs_root() -> Path:
    return absolute_path(ROOT / "Network Pharmacology" / "results" / "network_pharmacology_runs")


def is_task_owned_delete_path(path: Path) -> bool:
    try:
        resolved = absolute_path(path)
    except OSError:
        resolved = path.absolute()
    # Never allow a task operation to remove a shared root itself.
    return any(root in resolved.parents for root in task_delete_roots())


def validate_task_network_resource(row: sqlite3.Row, path: Path) -> Path:
    task_id = int(row["id"])
    run_id = str(row["run_id"] or "")
    if not run_id:
        raise ValueError(f"Task {task_id} has no run_id for network resource validation")
    root = network_runs_root()
    if root.is_symlink():
        raise ValueError(f"Network runs root cannot be a symbolic link: {root}")
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"Network task resource is not a regular directory: {path}")
    resolved = path.resolve(strict=True)
    try:
        root_resolved = root.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError(f"Network runs root is missing: {root}") from exc
    if resolved.parent != root_resolved:
        raise ValueError(f"Network task resource is not a direct child of the run root: {resolved}")
    manifest_path = resolved / NETWORK_RUN_MANIFEST_FILENAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError(f"Network task resource has no regular ownership manifest: {resolved}")
    if manifest_path.stat().st_size > NETWORK_RUN_MANIFEST_MAX_BYTES:
        raise ValueError(f"Network task resource ownership manifest is too large: {resolved}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Network task resource ownership manifest is invalid: {resolved}") from exc
    if not isinstance(manifest, dict):
        raise ValueError(f"Network task resource ownership manifest must be an object: {resolved}")
    if (
        manifest.get("schema_version") != 1
        or manifest.get("kind") != "npflow_network_run_binding"
        or manifest.get("stage") != "network"
    ):
        raise ValueError(f"Network task resource ownership manifest has an unsupported contract: {resolved}")
    expected_task = f"task_{task_id}"
    if manifest.get("task_id") != expected_task or manifest.get("run_id") != run_id:
        raise ValueError(f"Network task resource ownership does not match {expected_task}/{run_id}: {resolved}")
    output_dir = manifest.get("output_dir")
    if not isinstance(output_dir, str) or not output_dir.strip() or not Path(output_dir).is_absolute():
        raise ValueError(f"Network task resource manifest output_dir is invalid: {resolved}")
    if Path(output_dir).resolve(strict=False) != resolved:
        raise ValueError(f"Network task resource manifest output_dir does not match its directory: {resolved}")
    return resolved


def discover_task_resources(row: sqlite3.Row) -> list[Path]:
    """Persist only producer-owned manifests that bind network run/task/path."""
    task_id = int(row["id"])
    if not str(row["run_id"] or ""):
        return []
    root = network_runs_root()
    if root.is_symlink() or not root.is_dir():
        return []
    discovered: list[Path] = []
    try:
        children = list(root.iterdir())
    except OSError:
        return []
    for child in children:
        try:
            resolved = validate_task_network_resource(row, child)
        except (OSError, ValueError):
            continue
        discovered.append(resolved)
    if discovered:
        with db() as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO task_resources(task_id, run_id, kind, path, created_at)
                VALUES(?, ?, 'network_run', ?, ?)
                """,
                [(task_id, str(row["run_id"]), str(path), now()) for path in discovered],
            )
    return discovered


def persisted_task_resource_paths(row: sqlite3.Row) -> list[Path]:
    task_id = int(row["id"])
    run_id = str(row["run_id"] or "")
    with db() as conn:
        records = conn.execute(
            "SELECT path FROM task_resources WHERE task_id = ? AND run_id = ? AND kind = 'network_run' ORDER BY id",
            (task_id, run_id),
        ).fetchall()
    paths: list[Path] = []
    for record in records:
        path = Path(record["path"])
        if not path.exists() and not path.is_symlink():
            continue
        paths.append(validate_task_network_resource(row, path))
    return paths


def is_persisted_task_resource_delete_path(path: Path) -> bool:
    try:
        candidate = path.resolve(strict=True)
    except (FileNotFoundError, OSError):
        return False
    with db() as conn:
        owners = conn.execute(
            """
            SELECT tasks.* FROM task_resources
            JOIN tasks ON tasks.id = task_resources.task_id
            WHERE task_resources.kind = 'network_run' AND task_resources.path = ?
            """,
            (str(candidate),),
        ).fetchall()
    for owner in owners:
        try:
            if validate_task_network_resource(owner, candidate) == candidate:
                return True
        except (OSError, ValueError):
            continue
    return False


def remove_task_path(path: Path) -> None:
    if not is_task_owned_delete_path(path) and not is_persisted_task_resource_delete_path(path):
        raise ValueError(f"Refusing to delete outside task-owned roots: {path}")
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    elif path.is_file():
        path.unlink(missing_ok=True)
    if path.exists():
        raise OSError(f"Path still exists after deletion: {path}")


def task_paths_for_delete(row: sqlite3.Row) -> list[Path]:
    paths: list[Path] = []
    task_id = int(row["id"])
    expected_result = expected_web_result_dir(task_id)
    expected_log = absolute_path(LOG_ROOT / f"task_{task_id}.log")
    artifact_dir = absolute_path(ARTIFACT_ROOT / f"task_{task_id}")

    if expected_result.exists():
        paths.append(expected_result)
    if row["zip_path"]:
        zip_path = Path(row["zip_path"])
        try:
            zip_resolved = zip_path.resolve()
        except OSError:
            zip_resolved = zip_path.absolute()
        if zip_resolved == artifact_dir or artifact_dir in zip_resolved.parents:
            paths.append(artifact_dir)
    if artifact_dir.exists():
        paths.append(artifact_dir)

    if expected_log.exists():
        paths.append(expected_log)

    discover_task_resources(row)
    paths.extend(persisted_task_resource_paths(row))

    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _linux_process_stat(pid: int) -> tuple[str, str]:
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        text = stat_path.read_text(encoding="utf-8", errors="strict")
        after_comm = text[text.rfind(")") + 2 :].split()
        # /proc/<pid>/stat field 22 (starttime); after_comm starts at field 3.
        return after_comm[0], f"linux-starttime:{after_comm[19]}"
    except (IndexError, OSError, ValueError):
        return "", ""


def process_identity(pid: int) -> str:
    return _linux_process_stat(pid)[1]


def process_is_alive(pid: int, expected_identity: str = "") -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OSError):
        return False
    state, actual = _linux_process_stat(pid)
    if state == "Z":
        return False
    if expected_identity:
        if not actual or not hmac.compare_digest(actual, expected_identity):
            return False
    return True


def terminate_task_process(row: sqlite3.Row) -> None:
    if row["status"] not in {"running", "stopping", "deleting"} or not row["pid"]:
        return
    pid = int(row["pid"])
    identity = str(row["process_identity"] or "") if "process_identity" in row.keys() else ""
    if process_is_alive(pid) and not identity:
        raise RuntimeError(f"Refusing to signal unverifiable legacy PID {pid}")
    if not process_is_alive(pid, identity):
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        raise
    deadline = time.time() + 8
    while time.time() < deadline:
        stat_path = Path(f"/proc/{pid}/stat")
        if stat_path.exists():
            try:
                state = stat_path.read_text(encoding="utf-8", errors="ignore").split()[2]
                if state == "Z":
                    return
            except (IndexError, OSError):
                pass
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.2)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    kill_deadline = time.time() + 2
    while process_is_alive(pid, identity) and time.time() < kill_deadline:
        time.sleep(0.1)
    if process_is_alive(pid, identity):
        raise OSError(f"Task process group {pid} did not terminate")


def terminate_spawned_process(
    proc: subprocess.Popen[Any],
    *,
    term_timeout: float = 8.0,
    kill_timeout: float = 2.0,
) -> None:
    """Synchronously reap a process that this launcher has just created.

    The ``Popen`` handle still owns an unreaped child, so its PID cannot be
    reused while this rollback runs.  POSIX launches use a new session and are
    terminated as a process group; the Windows fallback uses the exact process
    handle.  A caller must not release the DB slot unless this function
    returns successfully.
    """
    if proc.poll() is not None:
        return

    try:
        if os.name == "posix" and hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=term_timeout)
        return
    except subprocess.TimeoutExpired:
        pass

    if proc.poll() is not None:
        return
    try:
        if os.name == "posix" and hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=kill_timeout)
    except subprocess.TimeoutExpired as exc:
        raise OSError(f"New task process {proc.pid} did not terminate during launch rollback") from exc
    if proc.poll() is None:
        raise OSError(f"New task process {proc.pid} did not terminate during launch rollback")


def delete_task_record(row: sqlite3.Row) -> None:
    task_id = int(row["id"])
    with TASK_LIFECYCLE_LOCK:
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not current:
                conn.rollback()
                return
            conn.execute("UPDATE tasks SET status = 'deleting' WHERE id = ?", (task_id,))
            conn.commit()
        try:
            terminate_task_process(current)
            with db() as conn:
                conn.execute(
                    """
                    UPDATE tasks SET pid = NULL, process_identity = NULL, cpu_range = NULL
                    WHERE id = ? AND status = 'deleting'
                    """,
                    (task_id,),
                )
                current = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not current:
                return
            paths = task_paths_for_delete(current)
            for path in paths:
                remove_task_path(path)
        except Exception as exc:
            with db() as conn:
                conn.execute(
                    "UPDATE tasks SET cleanup_error = ?, cleanup_retry_at = ? WHERE id = ?",
                    (
                        redact_sensitive_text(exc),
                        (datetime.now(timezone.utc) + timedelta(seconds=CLEANUP_RETRY_SECONDS)).isoformat(timespec="seconds"),
                        task_id,
                    ),
                )
            raise
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM task_resources WHERE task_id = ?", (task_id,))
            conn.execute("DELETE FROM task_artifacts WHERE task_id = ?", (task_id,))
            conn.execute("DELETE FROM task_events WHERE task_id = ?", (task_id,))
            conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            conn.commit()


def active_user_task(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT id, status FROM tasks
        WHERE user_id = ? AND status IN ('queued', 'running', 'stopping', 'deleting')
        ORDER BY id LIMIT 1
        """,
        (user_id,),
    ).fetchone()


_STAGE_ALIASES = {
    "network": "network",
    "network_pharmacology": "network",
    "network-pharmacology": "network",
    "docking": "docking",
    "molecular_docking": "docking",
    "molecular-docking": "docking",
    "reverse": "reverse_design",
    "reverse_design": "reverse_design",
    "reverse-design": "reverse_design",
    "packaging": "packaging",
}
_MODE_STAGES = {
    "network": ("network",),
    "network+docking": ("network", "docking"),
    "full": ("network", "docking", "reverse_design"),
    "docking": ("docking",),
    "docking+reverse": ("docking", "reverse_design"),
    "reverse": ("reverse_design",),
}


def _manifest_stages(value: Any) -> dict[str, dict[str, Any]]:
    stages: dict[str, dict[str, Any]] = {}
    if isinstance(value, dict):
        iterable = []
        for name, details in value.items():
            if not isinstance(details, dict):
                raise ValueError(f"阶段 {name!r} 的定义必须是对象")
            item = dict(details)
            item.setdefault("name", name)
            iterable.append(item)
    elif isinstance(value, list):
        iterable = value
    else:
        raise ValueError("stages 必须是对象或数组")
    for item in iterable:
        if not isinstance(item, dict):
            raise ValueError("stages 数组成员必须是对象")
        raw_name = str(item.get("name") or item.get("stage") or "").strip().lower()
        name = _STAGE_ALIASES.get(raw_name)
        if not name:
            if item.get("requested") is True:
                raise ValueError(f"未知请求阶段: {raw_name or '(unnamed)'}")
            continue
        if name in stages:
            raise ValueError(f"阶段重复声明: {name}")
        stages[name] = item
    return stages


def _artifact_item_path(item: Any, artifact_lookup: dict[str, Any]) -> str:
    if isinstance(item, str):
        mapped = artifact_lookup.get(item)
        if mapped is not None and mapped is not item:
            return _artifact_item_path(mapped, {})
        return item
    if isinstance(item, dict):
        for key in ("path", "relative_path", "file"):
            if item.get(key):
                return str(item[key])
    return ""


def _artifact_lookup(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    lookup: dict[str, Any] = {}
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                name = str(item.get("name") or item.get("kind") or item.get("label") or "")
                if name:
                    lookup[name] = item
    return lookup


def _required_artifacts(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return list(value.values())
    return []


def _validate_required_artifacts(stage: dict[str, Any], result_dir: Path) -> str:
    required = _required_artifacts(stage.get("required_artifacts"))
    if not required:
        return "completed 阶段没有声明 required_artifacts"
    lookup = _artifact_lookup(stage.get("artifacts"))
    result_root = result_dir.resolve()
    for item in required:
        path_text = _artifact_item_path(item, lookup).strip()
        if not path_text:
            return "required_artifacts 含无效路径"
        candidate = Path(path_text)
        if not candidate.is_absolute():
            candidate = result_root / candidate
        try:
            resolved = candidate.resolve()
        except OSError:
            return f"无法解析必需产物: {path_text}"
        if result_root not in resolved.parents:
            return f"必需产物不在任务结果目录内: {path_text}"
        try:
            valid_file = resolved.is_file() and resolved.stat().st_size > 0
        except OSError:
            valid_file = False
        if not valid_file:
            return f"必需产物缺失或为空: {path_text}"
    return ""


def validate_pipeline_status(row: sqlite3.Row, result_dir: Path) -> tuple[str, str, dict[str, Any]]:
    """Validate the worker's atomic completion contract from the fixed task directory."""
    manifest_path = result_dir / PIPELINE_STATUS_FILENAME
    try:
        if result_dir.is_symlink() or not result_dir.is_dir():
            raise ValueError("任务结果目录缺失或为符号链接")
        result_root = result_dir.resolve()
        if result_root not in manifest_path.resolve().parents:
            raise ValueError("流水线状态文件不在任务结果目录内")
        if manifest_path.is_symlink() or not manifest_path.is_file() or manifest_path.stat().st_size > PIPELINE_STATUS_MAX_BYTES:
            raise ValueError("流水线状态文件缺失或过大")
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return "failed", f"无法验证流水线完成状态: {exc}", {}
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return "failed", "流水线状态文件 schema_version 无效", {}
    if payload.get("kind") != "npflow_pipeline_status":
        return "failed", "流水线状态文件 kind 无效", {}

    task_id = int(row["id"])
    expected_result = expected_web_result_dir(task_id)
    if result_root != expected_result.resolve(strict=False):
        return "failed", "流水线结果目录不是任务固定结果目录", {}
    declared_result = payload.get("result_dir")
    if (
        not isinstance(declared_result, str)
        or not declared_result.strip()
        or not Path(declared_result).is_absolute()
        or Path(declared_result).resolve(strict=False) != result_root
    ):
        return "failed", "流水线状态文件 result_dir 与任务结果目录不匹配", {}
    manifest_task_id = str(payload.get("task_id", "")).strip()
    if manifest_task_id not in {str(task_id), f"task_{task_id}"}:
        return "failed", "流水线状态文件 task_id 与任务不匹配", {}
    run_id = str(row["run_id"] or "")
    if not run_id or not hmac.compare_digest(str(payload.get("run_id", "")), run_id):
        return "failed", "流水线状态文件 run_id 与本次启动不匹配", {}

    requested_mode = str(row["requested_mode"] or "auto").strip().lower()
    manifest_mode = str(payload.get("requested_mode") or "").strip().lower()
    if manifest_mode not in PIPELINE_TASK_MODES:
        return "failed", "流水线状态文件 requested_mode 无效", {}
    if requested_mode != "auto" and not hmac.compare_digest(manifest_mode, requested_mode):
        return "failed", "流水线状态文件 requested_mode 与用户请求不匹配", {}

    try:
        stages = _manifest_stages(payload.get("stages"))
    except ValueError as exc:
        return "failed", f"流水线阶段契约无效: {exc}", {}
    expected = _MODE_STAGES.get(manifest_mode, ())
    if manifest_mode == "auto":
        expected = tuple(name for name, stage in stages.items() if stage.get("requested") is True)
    if not expected:
        return "failed", "流水线状态文件没有声明任何请求阶段", {}
    unexpected = [name for name, stage in stages.items() if stage.get("requested") is True and name not in expected]
    if unexpected:
        return "failed", f"流水线状态文件声明了请求模式之外的阶段: {', '.join(unexpected)}", {}

    stage_summary: dict[str, Any] = {}
    has_no_result = False
    for name in expected:
        stage = stages.get(name)
        if not stage:
            return "failed", f"流水线状态文件缺少请求阶段: {name}", stage_summary
        if stage.get("requested") is not True:
            return "failed", f"流水线状态文件未将阶段标记为 requested: {name}", stage_summary
        stage_status = str(stage.get("status") or "").strip().lower()
        reason = redact_sensitive_text(stage.get("reason") or "")
        stage_summary[name] = {"requested": True, "status": stage_status, "reason": reason}
        if stage_status == "completed":
            artifact_error = _validate_required_artifacts(stage, result_dir)
            if artifact_error:
                return "failed", f"{name} 阶段产物校验失败: {artifact_error}", stage_summary
        elif stage_status == "no_result":
            has_no_result = True
            if not reason:
                return "failed", f"{name} 阶段 no_result 缺少原因", stage_summary
        elif stage_status == "skipped":
            if not reason:
                return "failed", f"{name} 阶段 skipped 缺少原因", stage_summary
        elif stage_status == "failed":
            return "failed", reason or f"{name} 阶段失败", stage_summary
        else:
            return "failed", f"{name} 阶段状态无效: {stage_status or '(empty)'}", stage_summary

    overall_status = str(payload.get("status") or "").strip().lower()
    overall_reason = redact_sensitive_text(payload.get("reason") or "")
    if overall_status == "failed":
        return "failed", overall_reason or "流水线报告失败", stage_summary
    if overall_status in SUCCESS_STATUSES:
        packaging = stages.get("packaging")
        if not packaging:
            return "failed", "流水线状态文件缺少 packaging 辅助阶段", stage_summary
        packaging_status = str(packaging.get("status") or "").strip().lower()
        packaging_reason = redact_sensitive_text(packaging.get("reason") or "")
        stage_summary["packaging"] = {
            "requested": False,
            "status": packaging_status,
            "reason": packaging_reason,
        }
        if packaging.get("requested") is not False or packaging_status != "completed":
            return "failed", "packaging 辅助阶段未按契约完成", stage_summary
        artifact_error = _validate_required_artifacts(packaging, result_dir)
        if artifact_error:
            return "failed", f"packaging 阶段产物校验失败: {artifact_error}", stage_summary
    if overall_status == "completed":
        incomplete = [name for name in expected if stage_summary[name]["status"] != "completed"]
        if incomplete:
            return "failed", f"流水线声明 completed，但请求阶段未完成: {', '.join(incomplete)}", stage_summary
        return "completed", overall_reason, stage_summary
    if overall_status == "no_result":
        if not has_no_result:
            return "failed", "流水线声明 no_result，但没有请求阶段报告 no_result", stage_summary
        return "no_result", overall_reason or "流水线正常结束，但没有产生可用科研结果", stage_summary
    return "failed", f"流水线总体状态无效: {overall_status or '(empty)'}", stage_summary


class Scheduler:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started = False

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        threading.Thread(target=self._loop, name="npflow-scheduler", daemon=True).start()

    def _loop(self) -> None:
        while True:
            try:
                self._reconcile()
                self._retry_deleting()
                self._cleanup_expired()
                self._launch_available()
            except Exception as exc:
                print(f"scheduler error: {exc}", flush=True)
            time.sleep(3)

    def _reconcile(self) -> None:
        with db() as conn:
            rows = conn.execute("SELECT * FROM tasks WHERE status IN ('running', 'stopping')").fetchall()
        for row in rows:
            pid = int(row["pid"]) if row["pid"] else 0
            identity = str(row["process_identity"] or "")
            if not pid or not process_is_alive(pid, identity):
                self._finalize_task_record(int(row["id"]), exit_code=None, orphaned=True)
                continue
            if row["status"] == "stopping":
                # New launches carry a /proc start-time identity. Never signal a
                # potentially reused PID recovered from a legacy row.
                if not identity:
                    continue
                try:
                    os.killpg(pid, signal.SIGTERM)
                    requested_at = parse_time(row["stopping_started_at"])
                    if requested_at and datetime.now(timezone.utc) - requested_at >= timedelta(seconds=STOP_GRACE_SECONDS):
                        os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    self._finalize_task_record(int(row["id"]), exit_code=None, orphaned=True)
                except PermissionError as exc:
                    add_event(int(row["id"]), "error", f"无法停止任务进程: {exc}")

    def _retry_deleting(self) -> None:
        with db() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tasks
                WHERE status = 'deleting'
                  AND (cleanup_retry_at IS NULL OR cleanup_retry_at <= ?)
                ORDER BY id
                """,
                (now(),),
            ).fetchall()
        for row in rows:
            try:
                delete_task_record(row)
            except Exception:
                # delete_task_record persists the redacted error and next retry time.
                continue

    def _record_artifact(self, conn: sqlite3.Connection, task_id: int, kind: str, path: Path, label: str) -> None:
        exists = conn.execute(
            "SELECT 1 FROM task_artifacts WHERE task_id = ? AND kind = ? AND path = ? LIMIT 1",
            (task_id, kind, str(path)),
        ).fetchone()
        if not exists:
            conn.execute(
                "INSERT OR IGNORE INTO task_artifacts(task_id, kind, path, label, created_at) VALUES(?, ?, ?, ?, ?)",
                (task_id, kind, str(path), label, now()),
            )

    def _finalize_task_record(self, task_id: int, exit_code: int | None, orphaned: bool = False) -> None:
        with TASK_LIFECYCLE_LOCK:
            with db() as conn:
                row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not row or row["status"] not in {"running", "stopping"}:
                return

            discover_task_resources(row)

            result_dir = expected_web_result_dir(task_id)
            log_path = (LOG_ROOT / f"task_{task_id}.log").resolve()
            zip_path = str(row["zip_path"] or "")
            stage_summary: dict[str, Any] = {}
            completion_reason = ""
            if row["status"] == "stopping":
                status = "cancelled"
                completion_reason = "任务按停止请求取消"
            elif exit_code not in (0, None):
                status = "failed"
                completion_reason = f"任务进程退出码为 {exit_code}"
            else:
                status, completion_reason, stage_summary = validate_pipeline_status(row, result_dir)

            warning = warning_from_sources(log_path, result_dir if result_dir.is_dir() else None) or str(row["warning"] or "")
            if completion_reason and status != "completed":
                warning = completion_reason
            if status in SUCCESS_STATUSES:
                try:
                    artifact_dir = absolute_path(ARTIFACT_ROOT / f"task_{task_id}")
                    if artifact_dir.is_symlink():
                        raise OSError("任务下载目录不能是符号链接")
                    artifact_dir.mkdir(parents=True, exist_ok=True)
                    zip_base = artifact_dir / "npflow_result"
                    if not zip_path or not Path(zip_path).is_file():
                        zip_path = shutil.make_archive(str(zip_base), "zip", root_dir=str(result_dir))
                    if log_path.is_file():
                        shutil.copy2(log_path, artifact_dir / "task.log")
                except OSError as exc:
                    status = "failed"
                    completion_reason = f"结果打包失败: {redact_sensitive_text(exc)}"
                    warning = completion_reason
                    zip_path = ""

            expires_at = str(row["expires_at"] or terminal_expiry())
            with db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                updated = conn.execute(
                    """
                    UPDATE tasks
                    SET status = ?, exit_code = ?, finished_at = COALESCE(finished_at, ?), expires_at = ?,
                        result_dir = ?, zip_path = ?, warning = ?, stage_status = ?, completion_reason = ?,
                        pid = NULL, stopping_started_at = NULL, cleanup_error = NULL,
                        cleanup_retry_at = NULL, cleanup_completed_at = NULL
                    WHERE id = ? AND status IN ('running', 'stopping')
                    """,
                    (
                        status,
                        exit_code,
                        now(),
                        expires_at,
                        str(result_dir) if result_dir.exists() else "",
                        zip_path,
                        warning,
                        json.dumps(stage_summary, ensure_ascii=False, separators=(",", ":")),
                        completion_reason,
                        task_id,
                    ),
                )
                if updated.rowcount != 1:
                    conn.rollback()
                    return
                if zip_path:
                    self._record_artifact(conn, task_id, "zip", Path(zip_path), "结果压缩包")
                if result_dir.is_dir():
                    for report_name in ("说明书.txt", "说明书.md"):
                        report_path = result_dir / report_name
                        if report_path.is_file():
                            self._record_artifact(conn, task_id, "report", report_path, "结果说明书")
                    for report_path in result_dir.glob("**/docking_report.html"):
                        self._record_artifact(conn, task_id, "report", report_path, "对接报告")
                    for report_path in result_dir.glob("**/report.html"):
                        self._record_artifact(conn, task_id, "report", report_path, "模块报告")
                    for core_name in ("docking_scores_ranked.csv", "accepted_candidates.csv", "intersection_targets.csv"):
                        for core_path in result_dir.glob(f"**/{core_name}"):
                            self._record_artifact(conn, task_id, "core", core_path, core_name)
                conn.commit()
        level = "info" if status == "completed" else ("warning" if status in {"no_result", "cancelled"} else "error")
        suffix = "（服务重启后按状态契约补收尾）" if orphaned else ""
        add_event(task_id, level, f"任务结束: {status}, exit={exit_code}; {completion_reason}{suffix}")

    def _cleanup_expired(self) -> None:
        cutoff = datetime.now(timezone.utc)
        with db() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tasks
                WHERE status IN ('completed', 'no_result', 'failed', 'cancelled')
                  AND expires_at IS NOT NULL
                  AND cleanup_completed_at IS NULL
                  AND (cleanup_retry_at IS NULL OR cleanup_retry_at <= ?)
                """,
                (now(),),
            ).fetchall()
        for row in rows:
            expires = parse_time(row["expires_at"])
            if not expires or expires > cutoff:
                continue
            with TASK_LIFECYCLE_LOCK:
                with db() as conn:
                    current = conn.execute(
                        "SELECT * FROM tasks WHERE id = ? AND status IN ('completed', 'no_result', 'failed', 'cancelled')",
                        (row["id"],),
                    ).fetchone()
                if not current:
                    continue
                task_id = int(current["id"])
                failures: list[str] = []
                try:
                    cleanup_paths = task_paths_for_delete(current)
                except Exception as exc:
                    cleanup_paths = []
                    failures.append(f"resource discovery: {redact_sensitive_text(exc)}")
                for path in cleanup_paths:
                    try:
                        remove_task_path(path)
                    except Exception as exc:
                        failures.append(f"{path.name}: {redact_sensitive_text(exc)}")

                expected_result = expected_web_result_dir(task_id)
                artifact_dir = absolute_path(ARTIFACT_ROOT / f"task_{task_id}")
                expected_log = absolute_path(LOG_ROOT / f"task_{task_id}.log")
                result_value = current["result_dir"] if expected_result.exists() else None
                stored_zip = Path(current["zip_path"]) if current["zip_path"] else None
                zip_value = current["zip_path"] if stored_zip and stored_zip.is_file() and artifact_dir in stored_zip.resolve().parents else None
                log_value = current["log_path"] if expected_log.is_file() else None
                if failures:
                    error = "; ".join(failures)[:2000]
                    retry_at = (datetime.now(timezone.utc) + timedelta(seconds=CLEANUP_RETRY_SECONDS)).isoformat(timespec="seconds")
                    with db() as conn:
                        updated = conn.execute(
                            """
                            UPDATE tasks SET result_dir = ?, zip_path = ?, log_path = ?, cleanup_error = ?, cleanup_retry_at = ?
                            WHERE id = ? AND status IN ('completed', 'no_result', 'failed', 'cancelled')
                            """,
                            (result_value, zip_value, log_value, error, retry_at, task_id),
                        )
                        artifacts = conn.execute("SELECT id, path FROM task_artifacts WHERE task_id = ?", (task_id,)).fetchall()
                        for artifact in artifacts:
                            if not Path(artifact["path"]).is_file():
                                conn.execute("DELETE FROM task_artifacts WHERE id = ?", (artifact["id"],))
                    if updated.rowcount == 1:
                        add_event(task_id, "warning", f"清理过期结果失败，稍后重试: {error}")
                else:
                    with db() as conn:
                        conn.execute("BEGIN IMMEDIATE")
                        conn.execute("DELETE FROM task_resources WHERE task_id = ?", (task_id,))
                        conn.execute("DELETE FROM task_artifacts WHERE task_id = ?", (task_id,))
                        conn.execute(
                            """
                            UPDATE tasks SET zip_path = NULL, result_dir = NULL, log_path = NULL,
                                cleanup_error = NULL, cleanup_retry_at = NULL, cleanup_completed_at = ?
                            WHERE id = ? AND status IN ('completed', 'no_result', 'failed', 'cancelled')
                            """,
                            (now(), task_id),
                        )
                        conn.commit()

    def _launch_available(self) -> None:
        free_gb = disk_free_gb(ROOT)
        min_free_gb = float(setting("min_free_gb", str(DEFAULT_MIN_FREE_GB)))
        if free_gb < min_free_gb:
            return
        max_concurrency = int(setting("max_concurrency", str(DEFAULT_CONCURRENCY)))
        with db() as conn:
            running = conn.execute(
                """
                SELECT cpu_range FROM tasks
                WHERE status IN ('running', 'stopping') OR (status = 'deleting' AND pid IS NOT NULL)
                """
            ).fetchall()
            if len(running) >= max_concurrency:
                return
            used = {row["cpu_range"] for row in running}
            slots = [slot for slot in CPU_SLOTS[:max_concurrency] if slot not in used]
            if not slots:
                return
            queued = conn.execute("SELECT * FROM tasks WHERE status = 'queued' ORDER BY id LIMIT ?", (len(slots),)).fetchall()
        for task, cpu_range in zip(queued, slots):
            self._launch_task(task, cpu_range)

    def _launch_task(self, task: sqlite3.Row, cpu_range: str) -> None:
        task_id = int(task["id"])
        log_path = LOG_ROOT / f"task_{task_id}.log"
        result_dir = expected_web_result_dir(task_id)
        run_id = secrets.token_urlsafe(24)
        proc: subprocess.Popen[Any] | None = None
        identity = ""
        with TASK_LIFECYCLE_LOCK:
            try:
                with db() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    current = conn.execute("SELECT * FROM tasks WHERE id = ? AND status = 'queued'", (task_id,)).fetchone()
                    slot_busy = conn.execute(
                        """
                        SELECT 1 FROM tasks
                        WHERE id != ? AND cpu_range = ?
                          AND (status IN ('running', 'stopping') OR (status = 'deleting' AND pid IS NOT NULL))
                        LIMIT 1
                        """,
                        (task_id, cpu_range),
                    ).fetchone()
                    if not current or slot_busy:
                        conn.rollback()
                        return
                    env = os.environ.copy()
                    env.update(
                        {
                            "OMP_NUM_THREADS": str(TASK_CPU_LIMIT),
                            "OPENBLAS_NUM_THREADS": "2",
                            "MKL_NUM_THREADS": "2",
                            "NUMEXPR_NUM_THREADS": "2",
                            "NPFLOW_TASK_CPU_LIMIT": str(TASK_CPU_LIMIT),
                            "NPFLOW_TASK_MEMORY_GB": str(TASK_MEMORY_GB),
                            "NPFLOW_WEB_TASK_ID": f"task_{task_id}",
                            "NPFLOW_WEB_RUN_ID": run_id,
                            "NPFLOW_REQUESTED_MODE": str(current["requested_mode"] or "auto"),
                            "NPFLOW_REQUESTED_PH": str(current["ph"]),
                            "NPFLOW_REQUESTED_HERBS_JSON": json.dumps(requested_herbs(str(current["herbs"])), ensure_ascii=False),
                            "NPFLOW_REQUESTED_DISEASE": str(current["disease"]),
                            "NPFLOW_REQUESTED_EXTRA_TARGETS_JSON": json.dumps(
                                parse_stored_string_list(current["extra_targets_json"], "extra_targets_json"),
                                ensure_ascii=False,
                            ),
                            "NPFLOW_REQUESTED_EXTRA_LIGANDS_JSON": json.dumps(
                                parse_stored_string_list(current["extra_ligands_json"], "extra_ligands_json"),
                                ensure_ascii=False,
                            ),
                            "NPFLOW_ROOT": str(ROOT),
                        }
                    )
                    command = [
                        "taskset",
                        "-c",
                        cpu_range,
                        "nice",
                        "-n",
                        "0",
                        "ionice",
                        "-c2",
                        "-n4",
                        str(PYTHON),
                        str(NPFLOW_AGENT),
                        str(current["input_text"]),
                    ]
                    if log_path.is_symlink():
                        raise OSError("任务日志路径不能是符号链接")
                    with log_path.open("ab") as log_handle:
                        log_handle.write((f"资源限制: CPU={cpu_range}, threads={TASK_CPU_LIMIT}, memory=unlimited\n").encode("utf-8"))
                        log_handle.write((f"启动任务: task_id={task_id}, run_id={run_id}\n").encode("utf-8"))
                        try:
                            proc = subprocess.Popen(
                                command,
                                cwd=str(ROOT),
                                env=env,
                                stdout=log_handle,
                                stderr=subprocess.STDOUT,
                                start_new_session=True,
                            )
                        except Exception as exc:
                            message = f"任务启动失败: {redact_sensitive_text(exc)}"
                            log_handle.write((message + "\n").encode("utf-8"))
                            conn.execute(
                                """
                                UPDATE tasks
                                SET status = 'failed', exit_code = -1, log_path = ?, result_dir = ?, finished_at = ?,
                                    expires_at = ?, warning = ?, completion_reason = ?, run_id = ?, cleanup_completed_at = NULL
                                WHERE id = ? AND status = 'queued'
                                """,
                                (str(log_path), str(result_dir), now(), terminal_expiry(), message, message, run_id, task_id),
                            )
                            conn.commit()
                            add_event(task_id, "error", message)
                            return
                    identity = process_identity(proc.pid)
                    if os.name == "posix" and Path("/proc").is_dir() and not identity:
                        raise RuntimeError("无法记录任务进程启动身份")
                    updated = conn.execute(
                        """
                        UPDATE tasks
                        SET status = 'running', pid = ?, process_identity = ?, cpu_range = ?, log_path = ?, result_dir = ?,
                            started_at = ?, run_id = ?, cleanup_completed_at = NULL
                        WHERE id = ? AND status = 'queued'
                        """,
                        (proc.pid, identity, cpu_range, str(log_path), str(result_dir), now(), run_id, task_id),
                    )
                    if updated.rowcount != 1:
                        raise RuntimeError("task state changed during atomic launch")
                    conn.commit()
            except Exception as exc:
                if proc is not None:
                    try:
                        terminate_spawned_process(proc)
                    except Exception as reap_exc:
                        message = (
                            "任务认领失败且新进程未确认退出: "
                            f"{redact_sensitive_text(exc)}; rollback={redact_sensitive_text(reap_exc)}"
                        )
                        recovery_identity = identity
                        if not recovery_identity:
                            try:
                                recovery_identity = process_identity(proc.pid)
                            except Exception:
                                recovery_identity = ""
                        with db() as conn:
                            conn.execute("BEGIN IMMEDIATE")
                            updated = conn.execute(
                                """
                                UPDATE tasks
                                SET status = 'stopping', pid = ?, process_identity = ?, cpu_range = ?,
                                    log_path = ?, result_dir = ?, started_at = COALESCE(started_at, ?),
                                    run_id = ?, stopping_started_at = COALESCE(stopping_started_at, ?),
                                    warning = ?, completion_reason = ?
                                WHERE id = ? AND (
                                    status = 'queued'
                                    OR (status = 'running' AND pid = ? AND run_id = ?)
                                )
                                """,
                                (
                                    proc.pid,
                                    recovery_identity,
                                    cpu_range,
                                    str(log_path),
                                    str(result_dir),
                                    now(),
                                    run_id,
                                    now(),
                                    message,
                                    message,
                                    task_id,
                                    proc.pid,
                                    run_id,
                                ),
                            )
                            conn.commit()
                        if updated.rowcount == 1:
                            add_event(task_id, "error", message)
                        raise RuntimeError(message) from reap_exc
                message = f"任务认领失败: {redact_sensitive_text(exc)}"
                with db() as conn:
                    updated = conn.execute(
                        """
                        UPDATE tasks SET status = 'failed', exit_code = -1, pid = NULL,
                            process_identity = NULL, cpu_range = NULL, log_path = ?, result_dir = ?,
                            finished_at = ?, expires_at = ?, warning = ?, completion_reason = ?, run_id = ?,
                            cleanup_completed_at = NULL
                        WHERE id = ? AND (
                            status = 'queued'
                            OR (status = 'running' AND pid = ? AND run_id = ?)
                        )
                        """,
                        (
                            str(log_path),
                            str(result_dir),
                            now(),
                            terminal_expiry(),
                            message,
                            message,
                            run_id,
                            task_id,
                            proc.pid if proc is not None else None,
                            run_id,
                        ),
                    )
                if updated.rowcount == 1:
                    add_event(task_id, "error", message)
                    return
                raise
        add_event(task_id, "info", f"任务启动，PID={proc.pid}，CPU={cpu_range}，线程={TASK_CPU_LIMIT}，内存不做硬限制")
        threading.Thread(target=self._monitor_task, args=(task_id, proc), name=f"npflow-task-{task_id}", daemon=True).start()

    def _monitor_task(self, task_id: int, proc: subprocess.Popen[Any]) -> None:
        exit_code = proc.wait()
        self._finalize_task_record(task_id, exit_code=exit_code)


scheduler = Scheduler()


class Handler(BaseHTTPRequestHandler):
    server_version = "NPFlowWeb/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        message = redact_sensitive_text(fmt % args)
        admin_token = setting("admin_token")
        if admin_token:
            message = message.replace(f"/admin/{admin_token}", "/admin/[hidden]")
            message = message.replace(admin_token, "[hidden]")
        print(f"{self.address_string()} - {message}", flush=True)

    def _send(self, status: int, body: bytes = b"", content_type: str = "application/json", headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        if self._is_secure_request():
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        if headers:
            for key, value in headers.items():
                self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, status: int, data: Any, headers: dict[str, str] | None = None) -> None:
        self._send(status, json_dumps(data), "application/json; charset=utf-8", headers)

    def _read_json(self, max_bytes: int = MAX_JSON_BODY_BYTES) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type and content_type != "application/json":
            raise RequestError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "请求体必须使用 application/json")
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError as exc:
            raise RequestError(HTTPStatus.BAD_REQUEST, "Content-Length 无效") from exc
        if length < 0:
            raise RequestError(HTTPStatus.BAD_REQUEST, "Content-Length 无效")
        if length <= 0:
            return {}
        if length > max_bytes:
            raise RequestError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"request body too large: {length} bytes")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RequestError(HTTPStatus.BAD_REQUEST, "JSON 请求体无效") from exc
        if not isinstance(payload, dict):
            raise RequestError(HTTPStatus.BAD_REQUEST, "JSON 请求体必须是对象")
        return payload

    def _peer_ip(self) -> str:
        return str(self.client_address[0]).split("%", 1)[0]

    def _is_trusted_proxy(self, value: str | None = None) -> bool:
        try:
            address = ipaddress.ip_address((value or self._peer_ip()).split("%", 1)[0])
        except ValueError:
            return False
        return any(address in network for network in TRUSTED_PROXY_NETWORKS)

    def _client_ip(self) -> str:
        peer = self._peer_ip()
        if not self._is_trusted_proxy(peer):
            return peer
        chain: list[str] = []
        for item in self.headers.get("X-Forwarded-For", "").split(","):
            item = item.strip().split("%", 1)[0]
            try:
                chain.append(str(ipaddress.ip_address(item)))
            except ValueError:
                continue
        chain.append(peer)
        for address in reversed(chain):
            if not self._is_trusted_proxy(address):
                return address
        return chain[0]

    def _is_secure_request(self) -> bool:
        if isinstance(getattr(self, "connection", None), ssl.SSLSocket):
            return True
        if not self._is_trusted_proxy():
            return False
        forwarded_proto = self.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().lower()
        return forwarded_proto == "https"

    def _require_secure_auth(self) -> bool:
        if self._is_secure_request() or ALLOW_INSECURE_AUTH:
            return True
        self._json(
            HTTPStatus.UPGRADE_REQUIRED,
            {"error": "登录、注册和会话操作仅允许通过 HTTPS"},
            {"Upgrade": "TLS/1.2"},
        )
        return False

    def _session_cookie(self, value: str, max_age: int) -> str:
        cookie = f"npflow_session={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax"
        if self._is_secure_request() or not ALLOW_INSECURE_AUTH:
            cookie += "; Secure"
        return cookie

    def _admin_session_cookie(self, value: str, max_age: int) -> str:
        return f"npflow_admin={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Strict; Secure"

    def _rate_limit(self, kind: str, identity: str, limit: int, window_seconds: int) -> bool:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=window_seconds)).isoformat(timespec="seconds")
        retention_cutoff = (datetime.now(timezone.utc) - timedelta(seconds=RATE_EVENT_RETENTION_SECONDS)).isoformat(timespec="seconds")
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM rate_events WHERE created_at < ?", (retention_cutoff,))
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM rate_events WHERE kind = ? AND identity = ? AND created_at >= ?",
                (kind, identity, cutoff),
            ).fetchone()["n"]
            if int(count) >= limit:
                conn.rollback()
                return False
            conn.execute(
                "INSERT INTO rate_events(kind, identity, created_at) VALUES(?, ?, ?)",
                (kind, identity, now()),
            )
            conn.commit()
        return True

    def _cookie_user_id(self) -> int | None:
        try:
            cookie = SimpleCookie(self.headers.get("Cookie"))
        except CookieError:
            return None
        morsel = cookie.get("npflow_session")
        return verify_session(morsel.value if morsel else None)

    def _cookie_admin_ok(self) -> bool:
        try:
            cookie = SimpleCookie(self.headers.get("Cookie"))
        except CookieError:
            return False
        morsel = cookie.get("npflow_admin")
        return verify_admin_session(morsel.value if morsel else None)

    def _admin_authenticated(self) -> bool:
        token = self.headers.get("X-Admin-Token") or ""
        header_ok = bool(token and hmac.compare_digest(token, setting("admin_token")))
        return header_ok or self._cookie_admin_ok()

    def _require_user(self) -> sqlite3.Row | None:
        if not self._require_secure_auth():
            return None
        user_id = self._cookie_user_id()
        if not user_id:
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "请先登录"})
            return None
        with db() as conn:
            user = conn.execute("SELECT id, username, email, created_at FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "会话已失效"})
            return None
        return user

    def _admin_ok(self) -> bool:
        if not self._require_secure_auth():
            return False
        if not self._admin_authenticated():
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "管理员会话无效，请重新登录"})
            return False
        return True

    def _task_id(self, value: str) -> int | None:
        try:
            task_id = int(value)
        except (TypeError, ValueError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "任务编号无效"})
            return None
        if task_id <= 0:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "任务编号无效"})
            return None
        return task_id

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/":
            return self._static_file(STATIC_ROOT / "index.html")
        if path.startswith("/static/"):
            if path == "/static/admin.html":
                return self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
            return self._static_file(STATIC_ROOT / path.removeprefix("/static/"))
        if path == "/admin":
            if not self._require_secure_auth():
                return
            return self._static_file(STATIC_ROOT / "admin.html")
        if path.startswith("/admin/"):
            return self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
        if path == "/api/me":
            return self.api_me()
        if path == "/api/tasks":
            return self.api_tasks()
        if path.startswith("/api/tasks/") and path.endswith("/log"):
            parts = path.split("/")
            if len(parts) != 5:
                return self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
            task_id = self._task_id(parts[3])
            return self.api_task_log(task_id) if task_id is not None else None
        if path.startswith("/api/admin/tasks/") and path.endswith("/log"):
            if self._admin_ok():
                parts = path.split("/")
                if len(parts) != 6:
                    return self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
                task_id = self._task_id(parts[4])
                return self.api_admin_task_log(task_id) if task_id is not None else None
            return
        if path == "/api/admin/overview":
            if self._admin_ok():
                return self.api_admin_overview()
            return
        if path.startswith("/download/"):
            parts = path.split("/")
            if len(parts) != 3:
                return self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
            task_id = self._task_id(parts[2])
            return self.download(task_id) if task_id is not None else None
        if path in {"/api/login", "/api/register", "/api/logout", "/api/admin/login", "/api/admin/logout"}:
            return self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "认证接口不接受 GET"}, {"Allow": "POST"})
        if path == "/health":
            return self._json(HTTPStatus.OK, {"ok": True, "free_gb": disk_free_gb(ROOT)})
        self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/api/register":
            return self.api_register()
        if path == "/api/login":
            return self.api_login()
        if path == "/api/logout":
            if not self._require_secure_auth():
                return
            return self._json(HTTPStatus.OK, {"ok": True}, {"Set-Cookie": self._session_cookie("", 0)})
        if path == "/api/admin/login":
            return self.api_admin_login()
        if path == "/api/admin/logout":
            if not self._require_secure_auth():
                return
            return self._json(HTTPStatus.OK, {"ok": True}, {"Set-Cookie": self._admin_session_cookie("", 0)})
        if path == "/api/tasks":
            return self.api_submit_task()
        if path.startswith("/api/admin/tasks/"):
            if not self._admin_ok():
                return
            parts = path.split("/")
            if len(parts) != 6:
                return self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
            task_id = self._task_id(parts[4])
            if task_id is None:
                return
            action = parts[5]
            if action == "stop":
                return self.api_admin_stop(task_id)
            if action == "restart":
                return self.api_admin_restart(task_id)
            return self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
        if path == "/api/admin/cleanup":
            if self._admin_ok():
                return self.api_admin_cleanup()
            return
        self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")

    def do_DELETE(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/tasks/"):
            parts = path.split("/")
            if len(parts) != 4:
                return self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
            try:
                task_id = int(parts[3])
            except ValueError:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": "任务编号无效"})
            if task_id <= 0:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": "任务编号无效"})
            return self.api_delete_task(task_id)
        if path.startswith("/api/admin/tasks/"):
            if not self._admin_ok():
                return
            parts = path.split("/")
            if len(parts) != 5:
                return self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
            try:
                task_id = int(parts[4])
            except ValueError:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": "任务编号无效"})
            if task_id <= 0:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": "任务编号无效"})
            return self.api_admin_delete_task(task_id)
        self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")

    def _static_file(self, path: Path) -> None:
        path = path.resolve()
        if not path.exists() or STATIC_ROOT.resolve() not in path.parents:
            return self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self._send(HTTPStatus.OK, path.read_bytes(), content_type)

    def api_register(self) -> None:
        if not self._require_secure_auth():
            return
        try:
            data = self._read_json()
        except RequestError as exc:
            return self._json(exc.status, {"error": str(exc)})
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", ""))
        invite_code = str(data.get("invite_code", "")).strip()
        ip = self._client_ip()
        if not self._rate_limit("register", ip, 3, 24 * 60 * 60):
            return self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "该 IP 今日注册次数已达上限，请联系管理员"})
        configured_invite = setting("invite_code")
        if not configured_invite:
            return self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "注册功能尚未配置，请联系管理员"})
        if not hmac.compare_digest(invite_code, configured_invite):
            return self._json(HTTPStatus.FORBIDDEN, {"error": "注册码不正确，请联系管理员获取"})
        if len(username) < 2 or len(password) < 6:
            return self._json(HTTPStatus.BAD_REQUEST, {"error": "用户名至少 2 位，密码至少 6 位"})
        email = f"{username}@sushan-aidd.local"
        try:
            with db() as conn:
                cur = conn.execute(
                    "INSERT INTO users(username, password_hash, email, created_at) VALUES(?, ?, ?, ?)",
                    (username, hash_password(password), email, now()),
                )
                user_id = int(cur.lastrowid)
        except sqlite3.IntegrityError:
            return self._json(HTTPStatus.CONFLICT, {"error": "用户名已存在"})
        cookie = self._session_cookie(sign_session(user_id), SESSION_TTL_SECONDS)
        return self._json(
            HTTPStatus.OK,
            {"ok": True, "user": {"id": user_id, "username": username}},
            {"Set-Cookie": cookie},
        )

    def api_login(self) -> None:
        if not self._require_secure_auth():
            return
        try:
            data = self._read_json()
        except RequestError as exc:
            return self._json(exc.status, {"error": str(exc)})
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", ""))
        identity = f"{self._client_ip()}:{username}"
        if not self._rate_limit("login", identity, 12, 15 * 60):
            return self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "登录尝试过于频繁，请稍后再试"})
        with db() as conn:
            user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if not user or not verify_password(password, user["password_hash"]):
            return self._json(HTTPStatus.UNAUTHORIZED, {"error": "用户名或密码错误"})
        cookie = self._session_cookie(sign_session(int(user["id"])), SESSION_TTL_SECONDS)
        self._json(HTTPStatus.OK, {"ok": True, "user": {"id": user["id"], "username": user["username"]}}, {"Set-Cookie": cookie})

    def api_admin_login(self) -> None:
        if not self._require_secure_auth():
            return
        try:
            data = self._read_json()
        except RequestError as exc:
            return self._json(exc.status, {"error": str(exc)})
        token = str(data.get("token", "")).strip()
        if len(token) > 512:
            return self._json(HTTPStatus.BAD_REQUEST, {"error": "管理员令牌格式无效"})
        if not self._rate_limit("admin_login", self._client_ip(), 10, 15 * 60):
            return self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "管理员登录尝试过于频繁，请稍后再试"})
        configured = setting("admin_token")
        if not token or not configured or not hmac.compare_digest(token, configured):
            return self._json(HTTPStatus.UNAUTHORIZED, {"error": "管理员令牌无效"})
        cookie = self._admin_session_cookie(sign_admin_session(), ADMIN_SESSION_TTL_SECONDS)
        return self._json(HTTPStatus.OK, {"ok": True}, {"Set-Cookie": cookie})

    def api_me(self) -> None:
        user = self._require_user()
        if user:
            self._json(HTTPStatus.OK, {"user": dict(user)})

    def api_submit_task(self) -> None:
        user = self._require_user()
        if not user:
            return
        if not self._rate_limit("submit", f"{self._client_ip()}:{user['id']}", 10, 60 * 60):
            return self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "提交过于频繁，请稍后再试"})
        free_gb = disk_free_gb(ROOT)
        min_free_gb = float(setting("min_free_gb", str(DEFAULT_MIN_FREE_GB)))
        if free_gb < min_free_gb:
            return self._json(HTTPStatus.INSUFFICIENT_STORAGE, {"error": f"磁盘剩余 {free_gb}GB，低于 {min_free_gb}GB 阈值，已暂停新任务"})
        try:
            data = self._read_json(MAX_SUBMIT_BODY_BYTES)
            herbs = clean_limited_field("herbs", data.get("herbs", ""))
            disease = clean_limited_field("disease", data.get("disease", ""))
            ph = clean_limited_field("ph", data.get("ph", "")) or "7.4"
            task_mode = clean_limited_field("task_mode", data.get("task_mode", "auto")).lower() or "auto"
            extra_targets = clean_limited_field("extra_targets", data.get("extra_targets", ""))
            extra_ligands = clean_limited_field("extra_ligands", data.get("extra_ligands", ""))
            task_notes = clean_limited_field("task_notes", data.get("task_notes", ""))
        except RequestError as exc:
            return self._json(exc.status, {"error": str(exc)})
        except ValueError as exc:
            return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        email = user["email"]
        if not herbs or not disease:
            return self._json(HTTPStatus.BAD_REQUEST, {"error": "中药和疾病不能为空"})
        if task_mode not in VALID_TASK_MODES:
            return self._json(HTTPStatus.BAD_REQUEST, {"error": "任务模式无效"})
        if "," in disease or "，" in disease or "+" in disease:
            return self._json(HTTPStatus.BAD_REQUEST, {"error": "第一版每次仅支持一个疾病"})
        try:
            ph = normalize_ph(ph)
        except ValueError as exc:
            return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        input_text = make_input_text(herbs, disease, ph, task_mode, extra_targets, extra_ligands, task_notes)
        extra_targets_items = requested_extras(extra_targets, uppercase=True)
        extra_ligands_items = requested_extras(extra_ligands)
        try:
            with db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                active = active_user_task(conn, int(user["id"]))
                if active:
                    conn.rollback()
                    return self._json(
                        HTTPStatus.CONFLICT,
                        {"error": f"当前账号已有任务 #{active['id']} 正在{active['status']}，完成后才能提交下一个任务"},
                    )
                cur = conn.execute(
                    """
                    INSERT INTO tasks(
                        user_id, email, herbs, disease, ph, input_text, requested_mode,
                        extra_targets_json, extra_ligands_json, status, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)
                    """,
                    (
                        user["id"],
                        email,
                        herbs,
                        disease,
                        ph,
                        input_text,
                        task_mode,
                        json.dumps(extra_targets_items, ensure_ascii=False, separators=(",", ":")),
                        json.dumps(extra_ligands_items, ensure_ascii=False, separators=(",", ":")),
                        now(),
                    ),
                )
                task_id = int(cur.lastrowid)
                conn.commit()
        except sqlite3.IntegrityError:
            return self._json(HTTPStatus.CONFLICT, {"error": "当前账号已有任务正在排队或运行，完成后才能提交下一个任务"})
        add_event(task_id, "info", "任务已进入队列")
        self._json(HTTPStatus.CREATED, {"ok": True, "task_id": task_id})

    def api_tasks(self) -> None:
        user = self._require_user()
        if not user:
            return
        with db() as conn:
            rows = conn.execute("SELECT * FROM tasks WHERE user_id = ? ORDER BY id DESC", (user["id"],)).fetchall()
            positions = queue_position_map(conn)
            counts = conn.execute(
                "SELECT status, COUNT(*) AS n FROM tasks WHERE status IN ('running', 'stopping', 'deleting', 'queued') GROUP BY status"
            ).fetchall()
        tasks = [apply_queue_metadata(task_to_dict(row), positions) for row in rows]
        count_map = {row["status"]: int(row["n"]) for row in counts}
        self._json(
            HTTPStatus.OK,
            {
                "tasks": tasks,
                "queue_summary": {
                    "running_count": sum(count_map.get(status, 0) for status in ("running", "stopping")),
                    "deleting_count": count_map.get("deleting", 0),
                    "queued_count": count_map.get("queued", 0),
                    "max_concurrency": int(setting("max_concurrency", str(DEFAULT_CONCURRENCY))),
                },
            },
        )

    def api_task_log(self, task_id: int) -> None:
        user = self._require_user()
        if not user:
            return
        with db() as conn:
            task = conn.execute("SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, user["id"])).fetchone()
        if not task:
            return self._json(HTTPStatus.NOT_FOUND, {"error": "任务不存在"})
        expected_log = absolute_path(LOG_ROOT / f"task_{task_id}.log")
        text = ""
        if task["log_path"]:
            try:
                stored_log = absolute_path(Path(task["log_path"]))
                if stored_log == expected_log:
                    text = read_log_tail(expected_log, 200)
            except OSError as exc:
                return self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"任务日志读取失败: {redact_sensitive_text(exc)}"})
        with db() as conn:
            events = conn.execute("SELECT level, message, created_at FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT 20", (task_id,)).fetchall()
        self._json(HTTPStatus.OK, {"log": text, "events": [dict(row) for row in reversed(events)]})

    def api_delete_task(self, task_id: int) -> None:
        user = self._require_user()
        if not user:
            return
        with db() as conn:
            task = conn.execute("SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, user["id"])).fetchone()
        if not task:
            return self._json(HTTPStatus.NOT_FOUND, {"error": "任务不存在"})
        try:
            delete_task_record(task)
        except Exception as exc:  # noqa: BLE001
            return self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"删除任务失败: {exc}"})
        self._json(HTTPStatus.OK, {"ok": True})

    def api_admin_task_log(self, task_id: int) -> None:
        with db() as conn:
            task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            return self._json(HTTPStatus.NOT_FOUND, {"error": "任务不存在"})
        expected_log = absolute_path(LOG_ROOT / f"task_{task_id}.log")
        text = ""
        if task["log_path"]:
            try:
                stored_log = absolute_path(Path(task["log_path"]))
                if stored_log == expected_log:
                    text = read_log_tail(expected_log, 300)
            except OSError as exc:
                return self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"任务日志读取失败: {redact_sensitive_text(exc)}"})
        with db() as conn:
            events = conn.execute("SELECT level, message, created_at FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT 30", (task_id,)).fetchall()
        self._json(HTTPStatus.OK, {"log": text, "events": [dict(row) for row in reversed(events)]})

    def api_admin_overview(self) -> None:
        with db() as conn:
            tasks = conn.execute(
                """
                SELECT tasks.*, users.username
                FROM tasks JOIN users ON users.id = tasks.user_id
                ORDER BY tasks.id DESC LIMIT 200
                """
            ).fetchall()
            counts = conn.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status").fetchall()
            positions = queue_position_map(conn)
        self._json(
            HTTPStatus.OK,
            {
                "tasks": [apply_queue_metadata(task_to_dict(row, admin=True), positions) for row in tasks],
                "counts": {row["status"]: row["n"] for row in counts},
                "free_gb": disk_free_gb(ROOT),
                "settings": {
                    "max_concurrency": setting("max_concurrency"),
                    "retention_days": setting("retention_days"),
                    "min_free_gb": setting("min_free_gb"),
                    "cpu_slots": CPU_SLOTS,
                    "task_cpu_limit": TASK_CPU_LIMIT,
                    "task_memory_gb": TASK_MEMORY_GB,
                    "task_memory_limited": False,
                    "task_memory_label": "内存不做硬限制",
                    "task_resource_label": TASK_RESOURCE_LABEL,
                    "invite_code": setting("invite_code", INVITE_CODE),
                },
            },
        )

    def api_admin_stop(self, task_id: int) -> None:
        with TASK_LIFECYCLE_LOCK:
            with db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
                if not task or task["status"] not in {"queued", "running", "stopping"}:
                    conn.rollback()
                    return self._json(HTTPStatus.CONFLICT, {"error": "任务当前状态不可停止"})
                if task["status"] == "queued":
                    conn.execute(
                        """
                        UPDATE tasks SET status = 'cancelled', finished_at = ?, expires_at = ?,
                            completion_reason = ?, cleanup_completed_at = NULL
                        WHERE id = ? AND status = 'queued'
                        """,
                        (now(), terminal_expiry(), "管理员取消排队任务", task_id),
                    )
                    conn.commit()
                    add_event(task_id, "warning", "管理员取消排队任务")
                    return self._json(HTTPStatus.OK, {"ok": True})
                stopping_at = str(task["stopping_started_at"] or now())
                updated = conn.execute(
                    "UPDATE tasks SET status = 'stopping', stopping_started_at = ? WHERE id = ? AND status IN ('running', 'stopping')",
                    (stopping_at, task_id),
                )
                if updated.rowcount != 1:
                    conn.rollback()
                    return self._json(HTTPStatus.CONFLICT, {"error": "任务状态已变化，请刷新后重试"})
                conn.commit()
            pid = int(task["pid"]) if task["pid"] else 0
            identity = str(task["process_identity"] or "")
            if pid and identity and process_is_alive(pid, identity):
                try:
                    os.killpg(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                except PermissionError as exc:
                    add_event(task_id, "error", f"无法发送停止信号: {exc}")
                    return self._json(HTTPStatus.CONFLICT, {"error": "任务已标记停止中，但无法向已验证进程发送信号"})
            elif pid and not identity:
                add_event(task_id, "warning", "任务缺少进程启动身份；为避免 PID 复用误杀，未发送停止信号")
        add_event(task_id, "warning", "管理员请求停止任务")
        self._json(HTTPStatus.OK, {"ok": True})

    def api_admin_delete_task(self, task_id: int) -> None:
        with db() as conn:
            task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            return self._json(HTTPStatus.NOT_FOUND, {"error": "任务不存在"})
        try:
            delete_task_record(task)
        except Exception as exc:  # noqa: BLE001
            return self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"删除任务失败: {exc}"})
        self._json(HTTPStatus.OK, {"ok": True})

    def api_admin_restart(self, task_id: int) -> None:
        try:
            with db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                old = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
                if not old:
                    conn.rollback()
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "任务不存在"})
                active = active_user_task(conn, int(old["user_id"]))
                if active:
                    conn.rollback()
                    return self._json(
                        HTTPStatus.CONFLICT,
                        {"error": f"该用户已有活动任务 #{active['id']}（{active['status']}），不能重跑"},
                    )
                cur = conn.execute(
                    """
                    INSERT INTO tasks(
                        user_id, email, herbs, disease, ph, input_text, requested_mode,
                        extra_targets_json, extra_ligands_json, status, attempt_of, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
                    """,
                    (
                        old["user_id"],
                        old["email"],
                        old["herbs"],
                        old["disease"],
                        old["ph"],
                        old["input_text"],
                        old["requested_mode"] or "auto",
                        old["extra_targets_json"] or "[]",
                        old["extra_ligands_json"] or "[]",
                        task_id,
                        now(),
                    ),
                )
                new_id = int(cur.lastrowid)
                conn.commit()
        except sqlite3.IntegrityError:
            return self._json(HTTPStatus.CONFLICT, {"error": "该用户已有排队、运行、停止中或删除中的任务"})
        add_event(new_id, "info", f"由任务 {task_id} 重启创建")
        self._json(HTTPStatus.CREATED, {"ok": True, "task_id": new_id})

    def api_admin_cleanup(self) -> None:
        scheduler._cleanup_expired()
        self._json(HTTPStatus.OK, {"ok": True})

    def download(self, task_id: int) -> None:
        if not self._require_secure_auth():
            return
        user_id = self._cookie_user_id()
        with db() as conn:
            if self._admin_authenticated():
                task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            else:
                task = conn.execute("SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id or -1)).fetchone()
        if not task or not task["zip_path"]:
            return self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
        try:
            path = Path(task["zip_path"]).resolve()
            artifact_dir_path = absolute_path(ARTIFACT_ROOT / f"task_{task_id}")
            if artifact_dir_path.is_symlink():
                return self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
            artifact_dir = artifact_dir_path.resolve()
        except OSError:
            return self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
        if artifact_dir not in path.parents:
            return self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
        if not path.is_file():
            return self._send(HTTPStatus.GONE, b"expired", "text/plain")
        headers = {"Content-Disposition": f'attachment; filename="SushanAIDD_task_{task_id}.zip"'}
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        with path.open("rb") as f:
            shutil.copyfileobj(f, self.wfile)


def main() -> None:
    init_db()
    scheduler.start()
    print(f"NPFlow HTTP health/static listener: {HTTP_BIND_HOST}:{DEFAULT_PORT}")
    if ALLOW_INSECURE_AUTH:
        print("WARNING: NPFLOW_ALLOW_INSECURE_AUTH is enabled; credentials and session cookies may use cleartext HTTP", flush=True)
    elif TRUSTED_PROXY_NETWORKS:
        print(f"Trusted TLS proxy networks: {', '.join(str(item) for item in TRUSTED_PROXY_NETWORKS)}")
    else:
        print("Authentication requires direct HTTPS; no trusted reverse proxies configured")
    if HTTPS_CERT.exists() and HTTPS_KEY.exists():
        https_server = ThreadingHTTPServer((HTTPS_BIND_HOST, DEFAULT_HTTPS_PORT), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certfile=str(HTTPS_CERT), keyfile=str(HTTPS_KEY))
        https_server.socket = context.wrap_socket(https_server.socket, server_side=True)
        threading.Thread(target=https_server.serve_forever, name="npflow-https", daemon=True).start()
        print(f"NPFlow HTTPS listening on {HTTPS_BIND_HOST}:{DEFAULT_HTTPS_PORT}")
    else:
        print(f"HTTPS disabled; missing cert/key: {HTTPS_CERT} {HTTPS_KEY}")
    ThreadingHTTPServer((HTTP_BIND_HOST, DEFAULT_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
