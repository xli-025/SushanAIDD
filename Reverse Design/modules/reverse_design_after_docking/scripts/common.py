from __future__ import annotations

import csv
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[5]
PYTHON_BIN = PROJECT_ROOT / ".venv" / "bin" / "python"


class ReverseDesignError(RuntimeError):
    """Raised when reverse design cannot continue for the current command."""


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            {str(key): ("" if value is None else str(value).strip()) for key, value in row.items()}
            for row in csv.DictReader(handle)
        ]


def write_rows(path: Path, rows: Iterable[dict[str, Any]], fields: list[str]) -> None:
    ensure_dir(path.parent)
    rows = list(rows)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def append_unique_fields(fields: list[str], rows: Iterable[dict[str, Any]]) -> list[str]:
    seen = set(fields)
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fields.append(key)
    return fields


def slugify(value: Any, default: str = "item", max_len: int = 90) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("._-")
    return (text or default)[:max_len]


def truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "是"}


def to_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or str(value).strip() == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def to_int(value: Any, default: int | None = None) -> int | None:
    try:
        if value is None or str(value).strip() == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def find_executable(name: str) -> str:
    sibling = Path(sys.executable).with_name(name)
    if sibling.exists() and sibling.is_file():
        return str(sibling)
    project_sibling = PROJECT_ROOT / ".venv" / "bin" / name
    if project_sibling.exists() and project_sibling.is_file():
        return str(project_sibling)
    return shutil.which(name) or ""


def candidate_bases(dock_dir: Path) -> list[Path]:
    bases = [dock_dir]
    for parent in dock_dir.parents:
        bases.append(parent)
        if parent == PROJECT_ROOT:
            break
    for extra in [
        PROJECT_ROOT,
        PROJECT_ROOT / "Molecular Docking",
        PROJECT_ROOT / "Network Pharmacology",
    ]:
        if extra not in bases:
            bases.append(extra)
    return bases


def resolve_path(value: Any, *, dock_dir: Path | None = None, base: Path | None = None) -> Path:
    raw = str(value or "").strip()
    if not raw:
        return Path("")
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    candidates: list[Path] = []
    if base:
        candidates.append(base / path)
    if dock_dir:
        candidates.extend(root / path for root in candidate_bases(dock_dir))
    candidates.append(PROJECT_ROOT / path)
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return candidates[0] if candidates else path


def copy_if_exists(src: Path, dst: Path) -> bool:
    if not src or not src.exists() or not src.is_file():
        return False
    ensure_dir(dst.parent)
    shutil.copy2(src, dst)
    return True


def parse_box(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def deterministic_seed(text: str) -> int:
    import hashlib

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 2147483647 or 1
