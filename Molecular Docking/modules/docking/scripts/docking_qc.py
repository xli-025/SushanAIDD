from __future__ import annotations

from typing import Any


def classify_pair(
    *,
    exit_code: int | None,
    pose_file_exists: bool,
    parse_result: dict[str, Any],
    missing_reason: str = "",
) -> tuple[str, str, str]:
    if missing_reason:
        return "skipped", "input_validation", missing_reason
    if exit_code is None:
        return "failed", "vina", "vina_not_installed"
    if exit_code != 0:
        return "failed", "vina", "vina_nonzero_exit"
    if parse_result.get("parse_status") != "success":
        return "failed", "parse", str(parse_result.get("failure_reason") or "parse_failed")
    if not pose_file_exists:
        return "failed", "vina", "no_pose_generated"
    if parse_result.get("best_score") == "":
        return "failed", "parse", "score_missing"
    return "success", "", ""


def summarize_pairs(rows: list[dict[str, Any]]) -> dict[str, Any]:
    success_rows = [row for row in rows if row.get("status") == "success"]
    failed_rows = [row for row in rows if row.get("status") == "failed"]
    skipped_rows = [row for row in rows if row.get("status") == "skipped"]
    best_pair = None
    if success_rows:
        best_pair = sorted(success_rows, key=lambda row: float(row.get("best_score") or 0))[0]
    return {
        "total_pairs": len(rows),
        "successful_pairs": len(success_rows),
        "failed_pairs": len(failed_rows),
        "skipped_pairs": len(skipped_rows),
        "best_pair": best_pair or {},
    }

