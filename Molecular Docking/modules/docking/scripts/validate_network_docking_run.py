from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from modules.docking.scripts.common import ensure_dir, now_utc, read_json, write_json


REQUIRED_FILES = [
    "00_input_manifest/docking_input_manifest.json",
    "00_input_manifest/network_docking_manifest.json",
    "00_input_manifest/selected_targets.csv",
    "00_input_manifest/selected_ligands.csv",
    "01_target_structures/target_structure_selection.csv",
    "01_target_structures/target_structure_selection_qc.csv",
    "02_receptor_prepared/receptor_preparation_qc.csv",
    "03_ligand_prepared/ligand_preparation_qc.csv",
    "04_binding_sites/binding_site_boxes.csv",
    "06_scores/docking_pair_qc.csv",
    "06_scores/docking_scores_ranked.csv",
    "08_docking_report/docking_qc_report.json",
    "08_docking_report/docking_report.html",
]

RANKING_COLUMNS = [
    "raw_vina_rank",
    "scientific_usable_rank",
    "confidence_adjusted_rank",
    "ranking_bucket",
]

RECEPTOR_QC_COLUMNS = [
    "protonation_status",
    "backend_used",
    "charge_sanity_status",
    "usable_for_scientific_ranking",
    "confidence_level",
]


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return [{key: (value or "").strip() for key, value in row.items()} for row in csv.DictReader(f)]


def csv_fields(path: Path) -> list[str]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list((csv.DictReader(f).fieldnames or []))


def truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}


def resolve_output_path(path_value: str, dock_dir: Path) -> Path:
    path = Path(path_value)
    if path.is_absolute():
        return path
    cwd_candidate = Path.cwd() / path
    if cwd_candidate.exists():
        return cwd_candidate
    dock_candidate = dock_dir / path
    if dock_candidate.exists():
        return dock_candidate
    return cwd_candidate


def add_check(checks: list[dict[str, Any]], name: str, passed: bool, message: str, *, level: str = "error") -> None:
    checks.append({"name": name, "passed": bool(passed), "level": level, "message": message})


def validate_network_docking_run(dock_dir: Path) -> dict[str, Any]:
    dock_dir = Path(dock_dir).resolve()
    checks: list[dict[str, Any]] = []

    for rel_path in REQUIRED_FILES:
        path = dock_dir / rel_path
        add_check(checks, f"required_file:{rel_path}", path.exists(), f"{rel_path} exists" if path.exists() else f"Missing {rel_path}")

    manifest_path = dock_dir / "00_input_manifest" / "docking_input_manifest.json"
    manifest_pairs: list[dict[str, Any]] = []
    if manifest_path.exists():
        try:
            manifest = read_json(manifest_path)
            manifest_pairs = list(manifest.get("pairs", [])) if isinstance(manifest, dict) else []
            add_check(checks, "manifest_readable", isinstance(manifest, dict), "docking_input_manifest.json is readable")
        except Exception as exc:
            add_check(checks, "manifest_readable", False, f"Cannot read docking_input_manifest.json: {exc}")

    pair_qc_path = dock_dir / "06_scores" / "docking_pair_qc.csv"
    pair_rows = read_rows(pair_qc_path)
    if manifest_path.exists() and pair_qc_path.exists():
        add_check(
            checks,
            "manifest_pairs_match_pair_qc",
            len(manifest_pairs) == len(pair_rows),
            f"manifest pairs={len(manifest_pairs)}; docking_pair_qc rows={len(pair_rows)}",
        )

    success_rows = [row for row in pair_rows if row.get("status") == "success"]
    pose_paths = [resolve_output_path(row.get("pose_file", ""), dock_dir) for row in success_rows if row.get("pose_file")]
    log_paths = [resolve_output_path(row.get("log_file", ""), dock_dir) for row in success_rows if row.get("log_file")]
    existing_pose_count = sum(1 for path in pose_paths if path.exists() and path.stat().st_size > 0)
    existing_log_count = sum(1 for path in log_paths if path.exists() and path.stat().st_size > 0)
    config_count = len(list((dock_dir / "smoke_vina").glob("**/config.txt")))
    add_check(checks, "successful_pose_count", existing_pose_count == len(success_rows), f"success={len(success_rows)}; pose files={existing_pose_count}")
    add_check(checks, "successful_log_count", existing_log_count == len(success_rows), f"success={len(success_rows)}; vina logs={existing_log_count}")
    add_check(checks, "successful_config_count", config_count == len(success_rows), f"success={len(success_rows)}; config files={config_count}")

    ranked_path = dock_dir / "06_scores" / "docking_scores_ranked.csv"
    ranking_fields = csv_fields(ranked_path)
    missing_ranking = [field for field in RANKING_COLUMNS if field not in ranking_fields]
    add_check(
        checks,
        "ranking_columns",
        not missing_ranking,
        "ranking columns complete" if not missing_ranking else f"Missing ranking columns: {', '.join(missing_ranking)}",
    )

    ranked_rows = read_rows(ranked_path)
    scientific_rows = [
        row
        for row in ranked_rows
        if row.get("ranking_bucket") == "scientific_usable" and truthy(row.get("usable_for_scientific_ranking"))
    ]
    add_check(
        checks,
        "scientific_usable_pair_exists",
        len(scientific_rows) > 0,
        f"scientific usable ranked pairs={len(scientific_rows)}",
    )
    best_scientific_rank_rows = [row for row in ranked_rows if row.get("scientific_usable_rank") == "1"]
    best_scientific_ok = bool(best_scientific_rank_rows) and all(truthy(row.get("usable_for_scientific_ranking")) for row in best_scientific_rank_rows)
    add_check(
        checks,
        "scientific_best_usable",
        best_scientific_ok,
        "scientific rank 1 is usable" if best_scientific_ok else "scientific rank 1 missing or not usable",
    )

    report_path = dock_dir / "08_docking_report" / "docking_qc_report.json"
    if report_path.exists():
        try:
            report = read_json(report_path)
            best = (report.get("scientific_usability") or {}).get("best_scientific_pair") or {}
            report_best_ok = bool(best) and truthy(best.get("usable_for_scientific_ranking"))
            add_check(
                checks,
                "report_best_scientific_usable",
                report_best_ok,
                "report best scientific pair is usable" if report_best_ok else "report best scientific pair missing or not usable",
            )
        except Exception as exc:
            add_check(checks, "report_best_scientific_usable", False, f"Cannot read report JSON: {exc}")

    receptor_qc_path = dock_dir / "02_receptor_prepared" / "receptor_preparation_qc.csv"
    receptor_fields = csv_fields(receptor_qc_path)
    missing_receptor_fields = [field for field in RECEPTOR_QC_COLUMNS if field not in receptor_fields]
    add_check(
        checks,
        "receptor_qc_columns",
        not missing_receptor_fields,
        "receptor QC columns complete" if not missing_receptor_fields else f"Missing receptor QC columns: {', '.join(missing_receptor_fields)}",
    )

    target_qc_path = dock_dir / "01_target_structures" / "target_structure_selection_qc.csv"
    target_rows = read_rows(target_qc_path)
    reasonless = [
        row
        for row in target_rows
        if row.get("selected_final") not in {"True", "true", "1"}
        and row.get("selection_status") in {"rejected", "skipped", "failed", "candidate_has_reference_ligand"}
        and not (row.get("rejection_reason") or row.get("failure_reason"))
    ]
    add_check(
        checks,
        "target_candidate_rejection_reasons",
        not reasonless,
        "all rejected/skipped target candidates have reasons" if not reasonless else f"candidate rows without reason={len(reasonless)}",
    )

    errors = [check for check in checks if not check["passed"] and check.get("level") == "error"]
    status = "PASS" if not errors else "FAIL"
    validation = {
        "module": "docking",
        "validator": "validate_network_docking_run",
        "generated_at": now_utc(),
        "dock_dir": str(dock_dir),
        "status": status,
        "summary": {
            "checks_total": len(checks),
            "checks_passed": sum(1 for check in checks if check["passed"]),
            "checks_failed": len(errors),
            "manifest_pairs": len(manifest_pairs),
            "pair_qc_rows": len(pair_rows),
            "successful_pairs": len(success_rows),
            "scientific_usable_ranked_pairs": len(scientific_rows),
        },
        "checks": checks,
    }
    validation_dir = ensure_dir(dock_dir / "validation")
    write_json(validation_dir / "validation_status.json", validation)
    (validation_dir / "validation_summary.txt").write_text(render_summary(validation), encoding="utf-8")
    return validation


def render_summary(validation: dict[str, Any]) -> str:
    lines = [
        f"Status: {validation.get('status', '')}",
        f"Dock dir: {validation.get('dock_dir', '')}",
        "",
        "Summary:",
    ]
    for key, value in (validation.get("summary") or {}).items():
        lines.append(f"- {key}: {value}")
    failed = [check for check in validation.get("checks", []) if not check.get("passed")]
    lines.append("")
    lines.append("Failed checks:")
    if failed:
        for check in failed:
            lines.append(f"- {check.get('name')}: {check.get('message')}")
    else:
        lines.append("- None")
    lines.append("")
    lines.append("All checks:")
    for check in validation.get("checks", []):
        marker = "PASS" if check.get("passed") else "FAIL"
        lines.append(f"- [{marker}] {check.get('name')}: {check.get('message')}")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Validate a completed NPFlow network-to-docking run.")
    parser.add_argument("--dock-dir", required=True)
    args = parser.parse_args()
    validation = validate_network_docking_run(Path(args.dock_dir))
    print(f"Validation status: {validation['status']}")
    print(f"Summary: {Path(args.dock_dir) / 'validation' / 'validation_summary.txt'}")
    raise SystemExit(0 if validation["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
