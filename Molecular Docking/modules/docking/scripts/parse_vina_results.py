from __future__ import annotations

import re
from pathlib import Path
from typing import Any


MODE_LINE_RE = re.compile(r"^\s*(\d+)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*$")


def parse_vina_log(log_file: Path) -> dict[str, Any]:
    if not log_file.exists():
        return {
            "parse_status": "failed",
            "best_score": "",
            "num_modes_found": 0,
            "failure_reason": "empty_log",
        }
    text = log_file.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        return {
            "parse_status": "failed",
            "best_score": "",
            "num_modes_found": 0,
            "failure_reason": "empty_log",
        }

    scores: list[float] = []
    for line in text.splitlines():
        match = MODE_LINE_RE.match(line)
        if match:
            scores.append(float(match.group(2)))

    if not scores:
        return {
            "parse_status": "failed",
            "best_score": "",
            "num_modes_found": 0,
            "failure_reason": "score_missing",
        }

    return {
        "parse_status": "success",
        "best_score": min(scores),
        "num_modes_found": len(scores),
        "failure_reason": "",
    }


def build_score_row(pair_qc: dict[str, Any]) -> dict[str, Any]:
    return {
        "pair_id": pair_qc.get("pair_id", ""),
        "replicate_id": pair_qc.get("replicate_id", 1),
        "target_gene": pair_qc.get("target_gene", ""),
        "ligand_id": pair_qc.get("ligand_id", ""),
        "best_score": pair_qc.get("best_score", ""),
        "num_modes_found": pair_qc.get("num_modes_found", 0),
        "pose_file": pair_qc.get("pose_file", ""),
        "log_file": pair_qc.get("log_file", ""),
        "status": pair_qc.get("status", ""),
    }

