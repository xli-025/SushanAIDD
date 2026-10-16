#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests
import yaml
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from npflow_runtime import remaining_timeout


USER_AGENT = "NPFlow-Agent/0.1.0 (network pharmacology research workflow)"
TCMSP_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
TCMSP_HEADERS = {
    "User-Agent": TCMSP_BROWSER_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Referer": "https://tcmsp-e.com/",
    "Connection": "keep-alive",
}
_TCMSP_SESSION = requests.Session()
_TCMSP_SESSION.headers.update(TCMSP_HEADERS)


class WorkflowError(RuntimeError):
    """Raised when a workflow step cannot produce a valid output."""


def fail(message: str) -> None:
    raise WorkflowError(message)


def project_root_from_cwd() -> Path:
    root = Path.cwd()
    if not (root / "config").exists() or not (root / "modules").exists():
        fail("Run this command from the NPFlow-Agent project root.")
    return root


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        fail(f"YAML file does not exist: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        fail(f"YAML file must contain an object: {path}")
    return data


def load_params(config: str | Path = "results/network_pharmacology/00_input/params.resolved.yaml") -> dict[str, Any]:
    path = Path(config)
    if not path.exists():
        path = Path("config/params.yaml")
    return load_yaml(path)


def output_root(params: dict[str, Any]) -> Path:
    runtime = params.get("runtime") or {}
    if runtime.get("output_dir"):
        return Path(runtime["output_dir"])
    out_dir = params.get("output", {}).get("dir", "results/network_pharmacology")
    return Path.cwd() / out_dir


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: Path, data: dict[str, Any] | list[Any]) -> None:
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_rows(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8", newline="") as f:
        delimiter = "\t" if path.suffix == ".tsv" else ","
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore", delimiter=delimiter)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        fail(f"CSV file does not exist: {path}")
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def http_get(url: str, *, params: dict[str, Any] | None = None, timeout: int = 60, retries: int = 3) -> requests.Response:
    last_exc: Exception | None = None
    # Keep the legacy positive attempt budget; zero still sends the initial request.
    for attempt in range(1, max(1, retries) + 1):
        try:
            response = requests.get(
                url,
                params=params,
                timeout=remaining_timeout(timeout),
                headers={"User-Agent": USER_AGENT},
            )
            if response.status_code < 500:
                return response
            last_exc = WorkflowError(f"HTTP {response.status_code} from {response.url}")
        except Exception as exc:  # requests can raise several subclasses.
            last_exc = exc
        time.sleep(min(2 * attempt, 6))
    fail(f"GET failed for {url}: {last_exc}")


def http_get_tcmsp(url: str, *, params: dict[str, Any] | None = None, timeout: int = 60, retries: int = 3) -> requests.Response:
    """GET TCMSP pages with browser-like headers and a persistent session.

    TCMSP frequently returns HTTP 200 database-error pages. This helper only
    improves access fidelity; callers must still validate that herb grid/grid2
    content was actually present before treating a live scrape as successful.
    """
    last_exc: Exception | None = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            if attempt == 1:
                try:
                    _TCMSP_SESSION.get(TCMSP_HEADERS["Referer"], timeout=remaining_timeout(min(timeout, 20)))
                except Exception:
                    pass
            response = _TCMSP_SESSION.get(url, params=params, timeout=remaining_timeout(timeout), allow_redirects=True)
            if response.status_code < 500:
                return response
            last_exc = WorkflowError(f"HTTP {response.status_code} from {response.url}")
        except Exception as exc:
            last_exc = exc
        time.sleep(min(2 * attempt, 6))
    fail(f"TCMSP GET failed for {url}: {last_exc}")


def http_post(
    url: str,
    *,
    data: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    timeout: int = 60,
    retries: int = 3,
) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            response = requests.post(
                url,
                data=data,
                json=json_body,
                timeout=remaining_timeout(timeout),
                headers={"User-Agent": USER_AGENT},
            )
            if response.status_code < 500:
                return response
            last_exc = WorkflowError(f"HTTP {response.status_code} from {response.url}")
        except Exception as exc:
            last_exc = exc
        time.sleep(min(2 * attempt, 6))
    fail(f"POST failed for {url}: {last_exc}")


def slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return slug.strip("_") or "value"


def unique_preserve(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        value = value.strip()
        if value and value not in seen:
            seen.add(value)
            output.append(value)
    return output


def rank_desc(rows: list[dict[str, Any]], key: str, out_key: str) -> None:
    ordered = sorted(
        enumerate(rows),
        key=lambda item: float(item[1].get(key) or 0),
        reverse=True,
    )
    current_rank = 0
    last_value: float | None = None
    for index, row in ordered:
        value = float(row.get(key) or 0)
        if last_value is None or value != last_value:
            current_rank += 1
            last_value = value
        rows[index][out_key] = current_rank


def normalize_minmax(values: list[float]) -> list[float]:
    """Min-max normalization to [0, 1]. Returns all 0.5 if all values equal."""
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi == lo:
        return [0.5] * len(values)
    return [(v - lo) / (hi - lo) for v in values]


def rank_normalize(rows: list[dict[str, Any]], key: str, out_key: str) -> None:
    """Compute a 0-1 normalized score from a rank, where rank 1 = 1.0.

    score = 1 - (rank - 1) / max(rank - 1, 1)
    So the best (rank 1) gets 1.0, the worst gets 0.0.
    """
    if not rows:
        return
    max_rank = max(float(row.get(out_key, len(rows))) for row in rows)
    if max_rank <= 1:
        for row in rows:
            row[out_key + "_norm"] = 1.0
        return
    for row in rows:
        rank_val = float(row.get(out_key, max_rank))
        row[out_key + "_norm"] = 1.0 - (rank_val - 1) / (max_rank - 1)


def safe_log2(x: float) -> float:
    """log2(1 + x), safe for x >= 0."""
    import math
    return math.log2(1.0 + max(0.0, x))


def exit_with_errors(fn) -> None:
    try:
        fn()
    except WorkflowError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
