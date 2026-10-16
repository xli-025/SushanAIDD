from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from modules.docking.scripts.common import DEFAULT_OUTPUT_DIR, ensure_dir, now_utc, write_json
from modules.docking.scripts.docking_qc import summarize_pairs


def generate_report(
    pair_rows: list[dict[str, Any]],
    *,
    dependency_diagnostic: dict[str, Any] | None = None,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    project_name: str = "docking_validation",
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    report_dir = ensure_dir(output_dir / "08_docking_report")
    summary = summarize_pairs(pair_rows)
    scientific_usable_pairs = sum(
        1
        for row in pair_rows
        if str(row.get("usable_for_scientific_ranking", "")).strip().lower() in {"true", "1", "yes"}
    )
    low_confidence_pairs = len(pair_rows) - scientific_usable_pairs
    success_rows = [
        row
        for row in pair_rows
        if row.get("status") == "success" and str(row.get("best_score", "")).strip() != ""
    ]
    best_raw_pair = min(success_rows, key=lambda row: float(row.get("best_score", 999999))) if success_rows else None
    usable_success_rows = [
        row
        for row in pair_rows
        if row.get("status") == "success"
        and str(row.get("usable_for_scientific_ranking", "")).strip().lower() in {"true", "1", "yes"}
        and str(row.get("best_score", "")).strip() != ""
    ]
    best_scientific_pair = None
    if usable_success_rows:
        best_scientific_pair = min(usable_success_rows, key=lambda row: float(row.get("best_score", 999999)))
    warnings = []
    if dependency_diagnostic:
        warnings.extend(dependency_diagnostic.get("blocking_missing_dependencies", []))
        warnings.extend(dependency_diagnostic.get("optional_missing_dependencies", []))
    for row in pair_rows:
        if row.get("warning"):
            warnings.append(str(row["warning"]))

    report = {
        "module": "docking",
        "project_name": project_name,
        "generated_at": now_utc(),
        "status": "passed" if summary["successful_pairs"] else "failed",
        "summary": summary,
        "scientific_usability": {
            "usable_for_scientific_ranking_pairs": scientific_usable_pairs,
            "low_confidence_or_workflow_validation_pairs": low_confidence_pairs,
            "best_raw_pair": best_raw_pair,
            "best_scientific_pair": best_scientific_pair,
        },
        "warnings": warnings,
        "scientific_note": (
            "AutoDock Vina scores are useful for candidate ranking only. "
            "They are not experimental binding affinity and do not prove a therapeutic mechanism."
        ),
        "pair_qc": pair_rows,
    }
    write_json(report_dir / "docking_qc_report.json", report)
    (report_dir / "docking_report.html").write_text(render_html(report), encoding="utf-8")
    return report


def render_html(report: dict[str, Any]) -> str:
    summary = report.get("summary", {})
    best = report.get("scientific_usability", {}).get("best_raw_pair") or summary.get("best_pair") or {}
    best_scientific = report.get("scientific_usability", {}).get("best_scientific_pair") or {}
    rows = report.get("pair_qc", [])
    row_html = "\n".join(
        "<tr>"
        f"<td>{html.escape(str(row.get('pair_id', '')))}</td>"
        f"<td>{html.escape(str(row.get('status', '')))}</td>"
        f"<td>{html.escape(str(row.get('best_score', '')))}</td>"
        f"<td>{html.escape(str(row.get('usable_for_scientific_ranking', '')))}</td>"
        f"<td>{html.escape(str(row.get('backend_used', '')))}</td>"
        f"<td>{html.escape(str(row.get('charge_sanity_status', '')))}</td>"
        f"<td>{html.escape(str(row.get('failure_reason', '')))}</td>"
        "</tr>"
        for row in rows
    )
    warning_items = "\n".join(f"<li>{html.escape(str(w))}</li>" for w in report.get("warnings", []))
    best_text = "None"
    if best:
        best_text = (
            f"{html.escape(str(best.get('target_gene', '')))} / "
            f"{html.escape(str(best.get('ligand_id', '')))} "
            f"(score {html.escape(str(best.get('best_score', '')))})"
        )
    scientific_best_text = "None"
    if best_scientific:
        scientific_best_text = (
            f"{html.escape(str(best_scientific.get('target_gene', '')))} / "
            f"{html.escape(str(best_scientific.get('ligand_id', '')))} "
            f"(score {html.escape(str(best_scientific.get('best_score', '')))})"
        )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Docking Validation Report</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 32px; color: #1f2937; }}
    h1 {{ font-size: 28px; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 16px; }}
    th, td {{ border: 1px solid #d1d5db; padding: 8px; text-align: left; }}
    th {{ background: #f3f4f6; }}
    .note {{ background: #fff7ed; border-left: 4px solid #f97316; padding: 12px; }}
  </style>
</head>
<body>
  <h1>Docking Validation Report</h1>
  <p>Status: <strong>{html.escape(str(report.get('status', '')))}</strong></p>
  <p>Successful pairs: {summary.get('successful_pairs', 0)}; failed pairs: {summary.get('failed_pairs', 0)}; skipped pairs: {summary.get('skipped_pairs', 0)}.</p>
  <p>Scientific-ranking usable pairs: {report.get('scientific_usability', {}).get('usable_for_scientific_ranking_pairs', 0)}; workflow-validation/low-confidence pairs: {report.get('scientific_usability', {}).get('low_confidence_or_workflow_validation_pairs', 0)}.</p>
  <p>Best raw Vina pair: {best_text}</p>
  <p>Best scientific-ranking usable pair: {scientific_best_text}</p>
  <div class="note">{html.escape(str(report.get('scientific_note', '')))}</div>
  <h2>Warnings</h2>
  <ul>{warning_items or '<li>None</li>'}</ul>
  <h2>Pair QC</h2>
  <table>
    <thead><tr><th>Pair</th><th>Status</th><th>Best score</th><th>Scientific ranking usable</th><th>Backend</th><th>Charge sanity</th><th>Failure reason</th></tr></thead>
    <tbody>{row_html}</tbody>
  </table>
</body>
</html>
"""
