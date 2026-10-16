from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import hashlib
import json
from pathlib import Path
from typing import Any

from modules.docking.scripts.common import DEFAULT_OUTPUT_DIR, DockingError, ensure_dir, read_json, slugify, write_rows
from modules.docking.scripts.docking_qc import classify_pair
from modules.docking.scripts.parse_vina_results import build_score_row, parse_vina_log
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from npflow_chemistry import docking_protocol
from npflow_runtime import remaining_timeout


PAIR_QC_FIELDS = [
    "pair_id",
    "replicate_id",
    "target_gene",
    "ligand_id",
    "receptor_pdbqt",
    "ligand_pdbqt",
    "box_id",
    "vina_command",
    "status",
    "exit_code",
    "runtime_seconds",
    "best_score",
    "num_modes_found",
    "pose_file",
    "log_file",
    "failure_stage",
    "failure_reason",
    "warning",
    "backend_used",
    "charge_sanity_status",
    "usable_for_scientific_ranking",
    "effective_vina_params", "comparison_protocol_id", "comparison_protocol", "config_file",
]

SCORE_FIELDS = [
    "pair_id",
    "replicate_id",
    "target_gene",
    "ligand_id",
    "box_id",
    "best_score",
    "num_modes_found",
    "pose_file",
    "log_file",
    "status",
    "raw_vina_rank",
    "scientific_usable_rank",
    "confidence_adjusted_rank",
    "ranking_bucket",
    "backend_used",
    "charge_sanity_status",
    "usable_for_scientific_ranking",
]


def find_vina() -> str | None:
    env_bin = os.environ.get("VINA_BIN")
    if env_bin and Path(env_bin).exists():
        return env_bin
    sibling_vina = Path(sys.executable).with_name("vina")
    if sibling_vina.exists():
        return str(sibling_vina)
    return shutil.which("vina")


def get_vina_cpu_limit(default: int = 24) -> int:
    raw = os.environ.get("NPFLOW_TASK_CPU_LIMIT", "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return max(1, value)


def _resolve(path: str, base: Path) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate.resolve()
    return (base / candidate).resolve()


def load_fixture_manifest(fixture_dir: Path) -> dict[str, Any]:
    fixture_dir = fixture_dir.resolve()
    box = read_json(fixture_dir / "box.json")
    return {
        "project_name": "smoke_vina",
        "pairs": [
            {
                "pair_id": "SMOKE_TARGET__SMOKE_LIGAND",
                "target_gene": "SMOKE_TARGET",
                "ligand_id": "SMOKE_LIGAND",
                "receptor_pdbqt": str(fixture_dir / "receptor.pdbqt"),
                "ligand_pdbqt": str(fixture_dir / "ligand.pdbqt"),
                "box_id": box.get("box_id", "fixture_box"),
                "box": box,
            }
        ],
    }


def load_input_manifest(manifest_path: Path) -> dict[str, Any]:
    data = read_json(manifest_path)
    if not isinstance(data, dict):
        raise DockingError(f"Manifest must be a JSON object: {manifest_path}")
    if "pairs" not in data or not isinstance(data["pairs"], list):
        raise DockingError("Manifest must contain a list field named 'pairs'.")
    return data


def validate_pair_inputs(pair: dict[str, Any], base: Path) -> str:
    receptor = _resolve(str(pair.get("receptor_pdbqt", "")), base)
    ligand = _resolve(str(pair.get("ligand_pdbqt", "")), base)
    box = pair.get("box") or {}
    if not receptor.exists():
        return "missing_receptor_pdbqt"
    if not ligand.exists():
        return "missing_ligand_pdbqt"
    required_box = [
        "center_x",
        "center_y",
        "center_z",
        "size_x",
        "size_y",
        "size_z",
    ]
    if any(str(box.get(key, "")).strip() == "" for key in required_box):
        return "missing_box"
    return ""


def deterministic_seed(pair_id: str, replicate_id: int) -> int:
    digest = hashlib.sha256(f"{pair_id}|{replicate_id}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 2147483647 or 1


def build_ranked_scores(pair_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    success_rows = [
        row
        for row in pair_rows
        if row.get("status") == "success" and str(row.get("best_score", "")).strip() != ""
    ]
    raw_sorted = sorted(success_rows, key=lambda row: float(row.get("best_score") or 999999))
    raw_rank = {id(row): index for index, row in enumerate(raw_sorted, start=1)}
    scientific_rows = [
        row
        for row in raw_sorted
        if str(row.get("usable_for_scientific_ranking", "")).strip().lower() in {"true", "1", "yes"}
    ]
    scientific_rank = {id(row): index for index, row in enumerate(scientific_rows, start=1)}
    adjusted_sorted = scientific_rows + [row for row in raw_sorted if id(row) not in scientific_rank]
    adjusted_rank = {id(row): index for index, row in enumerate(adjusted_sorted, start=1)}
    rows: list[dict[str, Any]] = []
    for row in adjusted_sorted:
        usable = id(row) in scientific_rank
        rows.append(
            {
                "pair_id": row.get("pair_id", ""),
                "replicate_id": row.get("replicate_id", ""),
                "target_gene": row.get("target_gene", ""),
                "ligand_id": row.get("ligand_id", ""),
                "box_id": row.get("box_id", ""),
                "best_score": row.get("best_score", ""),
                "num_modes_found": row.get("num_modes_found", ""),
                "pose_file": row.get("pose_file", ""),
                "log_file": row.get("log_file", ""),
                "status": row.get("status", ""),
                "raw_vina_rank": raw_rank.get(id(row), ""),
                "scientific_usable_rank": scientific_rank.get(id(row), ""),
                "confidence_adjusted_rank": adjusted_rank.get(id(row), ""),
                "ranking_bucket": "scientific_usable" if usable else "workflow_validation_low_confidence",
                "backend_used": row.get("backend_used", ""),
                "charge_sanity_status": row.get("charge_sanity_status", ""),
                "usable_for_scientific_ranking": row.get("usable_for_scientific_ranking", ""),
            }
        )
    return rows


def run_pairs(
    manifest: dict[str, Any],
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    manifest_base: Path | None = None,
    exhaustiveness: int = 8,
    num_modes: int = 9,
    energy_range: int = 3,
    timeout_seconds: int = 1800,
    num_replicates: int = 1,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    output_dir = Path(output_dir)
    run_root = ensure_dir(output_dir / "smoke_vina")
    score_dir = ensure_dir(output_dir / "06_scores")
    base = manifest_base or Path.cwd()
    vina_bin = find_vina()
    vina_cpu = get_vina_cpu_limit()
    pair_rows: list[dict[str, Any]] = []
    score_rows: list[dict[str, Any]] = []

    for pair in manifest.get("pairs", []):
        pair_id = str(pair.get("pair_id") or f"{pair.get('target_gene', 'target')}__{pair.get('ligand_id', 'ligand')}")
        target_gene = str(pair.get("target_gene") or "target")
        ligand_id = str(pair.get("ligand_id") or "ligand")
        receptor = _resolve(str(pair.get("receptor_pdbqt", "")), base)
        ligand = _resolve(str(pair.get("ligand_pdbqt", "")), base)
        box = pair.get("box") or {}
        box_id = str(pair.get("box_id", box.get("box_id", "fixture_box")))
        receptor_flags = pair.get("receptor_qc_flags") or {}
        missing_reason = validate_pair_inputs(pair, base)

        for replicate_id in range(1, max(1, num_replicates) + 1):
            seed = int(pair.get("vina_params", {}).get("seed") or deterministic_seed(pair_id, replicate_id)) if str(pair.get("vina_params", {}).get("seed", "")).isdigit() else deterministic_seed(pair_id, replicate_id)
            effective_params = {"exhaustiveness": exhaustiveness, "num_modes": num_modes,
                                "energy_range": energy_range, "cpu": vina_cpu, "seed": seed}
            pair["vina_params"] = dict(effective_params, num_replicates=num_replicates)
            protocol_id, protocol = "", {}
            if not missing_reason and vina_bin:
                ligand_flags = pair.get("ligand_qc_flags") or {}
                protocol_id, protocol = docking_protocol(receptor, box, vina_bin, effective_params,
                    ligand_flags.get("ph_requested"), ligand_flags.get("ph_protonation_status", "unknown"))
            pair_dir = ensure_dir(run_root / slugify(target_gene) / slugify(ligand_id) / slugify(box_id) / f"replicate_{replicate_id}")
            config_path = pair_dir / "config.txt"
            pose_file = pair_dir / "pose.pdbqt"
            log_file = pair_dir / "vina.log"
            config_text = "\n".join(
                [
                    f"receptor = {receptor}",
                    f"ligand = {ligand}",
                    f"out = {pose_file}",
                    f"center_x = {box.get('center_x', '')}",
                    f"center_y = {box.get('center_y', '')}",
                    f"center_z = {box.get('center_z', '')}",
                    f"size_x = {box.get('size_x', '')}",
                    f"size_y = {box.get('size_y', '')}",
                    f"size_z = {box.get('size_z', '')}",
                    f"exhaustiveness = {exhaustiveness}",
                    f"num_modes = {num_modes}",
                    f"energy_range = {energy_range}",
                    f"cpu = {vina_cpu}",
                    f"seed = {seed}",
                    "",
                ]
            )
            config_path.write_text(config_text, encoding="utf-8")

            exit_code: int | None = None
            runtime = 0.0
            command: list[str] = []
            warning = ""
            if not missing_reason and vina_bin:
                command = [vina_bin, "--config", str(config_path)]
                started = time.time()
                try:
                    with log_file.open("w", encoding="utf-8") as log_handle:
                        result = subprocess.run(
                            command,
                            text=True,
                            stdout=log_handle,
                            stderr=subprocess.STDOUT,
                            timeout=remaining_timeout(timeout_seconds),
                        )
                    exit_code = result.returncode
                except subprocess.TimeoutExpired:
                    exit_code = 124
                    warning = "Vina run exceeded timeout_per_pair."
                    with log_file.open("a", encoding="utf-8") as handle:
                        handle.write("\nVina timeout; original output retained\n")
                runtime = round(time.time() - started, 3)
            else:
                if not vina_bin:
                    log_file.write_text("AutoDock Vina executable not found on PATH.\n", encoding="utf-8")

            parse_result = parse_vina_log(log_file)
            status, failure_stage, failure_reason = classify_pair(
                exit_code=exit_code,
                pose_file_exists=pose_file.exists(),
                parse_result=parse_result,
                missing_reason=missing_reason,
            )
            row = {
                "pair_id": pair_id,
                "replicate_id": replicate_id,
                "effective_vina_params": json.dumps(effective_params, sort_keys=True),
                "comparison_protocol_id": protocol_id, "comparison_protocol": json.dumps(protocol, sort_keys=True),
                "config_file": str(config_path),
                "target_gene": target_gene,
                "ligand_id": ligand_id,
                "receptor_pdbqt": str(receptor),
                "ligand_pdbqt": str(ligand),
                "box_id": box_id,
                "vina_command": " ".join(command),
                "status": status,
                "exit_code": "" if exit_code is None else exit_code,
                "runtime_seconds": runtime,
                "best_score": parse_result.get("best_score", ""),
                "num_modes_found": parse_result.get("num_modes_found", 0),
                "pose_file": str(pose_file),
                "log_file": str(log_file),
                "failure_stage": failure_stage,
                "failure_reason": failure_reason,
                "warning": ";".join(
                    item
                    for item in [warning, str(receptor_flags.get("warning", ""))]
                    if item
                ),
                "backend_used": receptor_flags.get("backend_used", ""),
                "charge_sanity_status": receptor_flags.get("charge_sanity_status", ""),
                "usable_for_scientific_ranking": receptor_flags.get("usable_for_scientific_ranking", ""),
            }
            pair_rows.append(row)
            score_rows.append(build_score_row(row))

    write_rows(score_dir / "docking_pair_qc.csv", pair_rows, PAIR_QC_FIELDS)
    write_rows(score_dir / "docking_scores_raw.csv", score_rows, SCORE_FIELDS)
    ranked = build_ranked_scores(pair_rows)
    write_rows(score_dir / "docking_scores_ranked.csv", ranked, SCORE_FIELDS)
    if len(pair_rows) == 1:
        first = pair_rows[0]
        log_file = Path(str(first.get("log_file", "")))
        pose_file = Path(str(first.get("pose_file", "")))
        if log_file.exists():
            shutil.copyfile(log_file, run_root / "vina.log")
        if pose_file.exists():
            shutil.copyfile(pose_file, run_root / "pose.pdbqt")
    return pair_rows, score_rows
