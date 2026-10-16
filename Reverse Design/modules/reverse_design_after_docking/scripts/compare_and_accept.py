from __future__ import annotations

from pathlib import Path
from typing import Any

from .common import read_rows, to_float, truthy, write_rows
from .provenance import formal_result_eligible
from .redock_candidates import REDOCK_FIELDS


def compare_and_accept(
    *,
    redocking_csv: Path,
    accepted_csv: Path,
    improvement_margin_kcal: float = 1.0,
    high_confidence_margin_kcal: float = 1.5,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = read_rows(redocking_csv)
    updated: list[dict[str, Any]] = []
    accepted_rows: list[dict[str, Any]] = []
    for row in rows:
        old_score = to_float(row.get("old_score"))
        new_score = to_float(row.get("new_score"))
        is_formal = formal_result_eligible(row)
        if "comparison_status" in row and row.get("comparison_status") != "matched_parent_protocol":
            accepted, improvement, cutoff = False, "", ""
            confidence, stop_reason = "raw_score_only", "comparison_protocol_unconfirmed"
        elif not is_formal:
            accepted = False
            improvement = "" if old_score is None or new_score is None else round(old_score - new_score, 4)
            cutoff = "" if old_score is None else round(old_score - improvement_margin_kcal, 4)
            if str(row.get("execution_mode", "")).strip().lower() == "simulation":
                confidence = "simulation_only"
                stop_reason = "simulation_result_not_eligible_for_acceptance"
            else:
                confidence = "provenance_or_redock_failure"
                stop_reason = row.get("failure_reason") or "result_not_eligible_for_acceptance"
        elif old_score is None or new_score is None:
            accepted = False
            improvement = ""
            cutoff = ""
            confidence = "redock_failed"
            stop_reason = row.get("failure_reason") or "redock_failed"
        else:
            cutoff_value = old_score - improvement_margin_kcal
            improvement_value = old_score - new_score
            accepted = new_score <= cutoff_value
            improvement = round(improvement_value, 4)
            cutoff = round(cutoff_value, 4)
            if improvement_value >= high_confidence_margin_kcal:
                confidence = "high_confidence_accepted"
            elif improvement_value >= improvement_margin_kcal:
                confidence = "accepted"
            else:
                confidence = "not_enough_improvement"
            stop_reason = "accepted_score_improved" if accepted else "not_enough_improvement"
        new_row = {
            **row,
            "score_improvement": improvement,
            "pass_score_cutoff": cutoff,
            "score_threshold_met": accepted if is_formal else truthy(row.get("score_threshold_met")),
            "accepted": accepted,
            "confidence_level": confidence,
            "stop_reason": stop_reason,
        }
        updated.append(new_row)
        if accepted and is_formal:
            accepted_rows.append(new_row)
    write_rows(redocking_csv, updated, REDOCK_FIELDS)
    write_rows(accepted_csv, accepted_rows, REDOCK_FIELDS)
    return updated, accepted_rows
