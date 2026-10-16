from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .common import (
    ReverseDesignError,
    deterministic_seed,
    ensure_dir,
    find_executable,
    read_json,
    read_rows,
    to_float,
    to_int,
    truthy,
    write_rows,
)
from .provenance import combined_execution_mode, formal_result_eligible, join_fallback_reasons
from .atom_mapping import TRACE_FIELDS
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from npflow_chemistry import docking_protocol
from npflow_runtime import remaining_timeout

COMPARISON_TRACE_FIELDS = ["comparison_protocol_id", "comparison_status", "comparison_protocol", "baseline_origin", "job_role"]


VINA_JOB_MANIFEST_VERSION = "vina_jobs.v1"
VINA_INPUT_FINGERPRINT_VERSION = "vina_input.v1"
SIMULATION_SCORING_ENGINE = "deterministic_score_simulator_v1"

REDOCK_FIELDS = [
    "job_id",
    "input_fingerprint",
    "job_status",
    "candidate_id",
    "seed_pair_id",
    "target_id",
    "parent_ligand_id",
    "generation_backend",
    "scoring_engine",
    "execution_mode",
    "fallback_reason",
    "old_score",
    "new_score",
    "score_improvement",
    "pass_score_cutoff",
    "score_threshold_met",
    "accepted",
    "confidence_level",
    "round",
    "status",
    "vina_pose_file",
    "vina_log_file",
    "vina_config_file",
    "simulation_artifact_file",
    "runtime_seconds",
    "stop_reason",
    "failure_reason",
] + TRACE_FIELDS + COMPARISON_TRACE_FIELDS


def get_vina_cpu_limit(default: int = 24) -> int:
    raw = os.environ.get("NPFLOW_TASK_CPU_LIMIT", "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return max(1, value)


def parse_vina_score(log_file: Path) -> tuple[float | None, int]:
    if not log_file.exists():
        return None, 0
    best: float | None = None
    modes = 0
    for line in log_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].isdigit():
            try:
                score = float(parts[1])
            except ValueError:
                continue
            modes += 1
            best = score if best is None else min(best, score)
    return best, modes


def _box_lines(box: dict[str, Any]) -> list[str]:
    return [
        f"center_x = {box.get('center_x', '')}",
        f"center_y = {box.get('center_y', '')}",
        f"center_z = {box.get('center_z', '')}",
        f"size_x = {box.get('size_x', '')}",
        f"size_y = {box.get('size_y', '')}",
        f"size_z = {box.get('size_z', '')}",
    ]


def _simulate_score(seed: dict[str, Any], candidate_id: str, old_score: float) -> float:
    jitter = deterministic_seed(f"{seed.get('seed_pair_id')}|{candidate_id}|dry_redock") % 80
    return round(old_score - 0.4 - jitter / 100.0, 3)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job_id(
    seed_pair_id: Any,
    round_id: Any,
    candidate_id: Any,
    scoring_engine: Any = "",
    input_fingerprint: str = "",
) -> str:
    base = (
        f"{str(seed_pair_id or '').strip()}|r{str(round_id or '').strip()}|"
        f"{str(candidate_id or '').strip()}|{str(scoring_engine or 'unknown').strip()}"
    )
    return f"{base}|fp:{input_fingerprint}" if input_fingerprint else base


def _file_input_identity(value: Any) -> dict[str, Any]:
    raw = str(value or "").strip()
    if not raw:
        return {"path": "", "sha256": "missing", "size": None}
    path = Path(raw)
    identity: dict[str, Any] = {"path": str(path.resolve(strict=False)), "sha256": "missing", "size": None}
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        identity["sha256"] = digest.hexdigest()
        identity["size"] = path.stat().st_size
    except OSError as exc:
        identity["sha256"] = f"unreadable:{type(exc).__name__}"
    return identity


def _numeric_vina_seed(seed: dict[str, Any], candidate_id: str) -> int:
    numeric_seed = to_int(seed.get("random_seed", ""))
    if numeric_seed is None:
        numeric_seed = deterministic_seed(f"{seed.get('seed_pair_id')}|{candidate_id}")
    return numeric_seed


def _vina_input_fingerprint(
    *,
    seed: dict[str, Any],
    candidate: dict[str, Any],
    box: dict[str, Any],
    scoring_engine: str,
    vina_cpu: int,
) -> tuple[str, dict[str, Any]]:
    candidate_id = str(candidate.get("candidate_id", ""))
    parameters = {
        "exhaustiveness": to_int(seed.get("exhaustiveness"), 8),
        "num_modes": to_int(seed.get("num_modes"), 9),
        "energy_range": to_int(seed.get("energy_range"), 3),
        "cpu": vina_cpu,
        "seed": _numeric_vina_seed(seed, candidate_id),
    }
    components = {
        "contract_version": VINA_INPUT_FINGERPRINT_VERSION,
        "scoring_engine": scoring_engine,
        "receptor_pdbqt": _file_input_identity(
            seed.get("prepared_receptor_pdbqt") or seed.get("receptor_pdbqt")
        ),
        "candidate_pdbqt": _file_input_identity(candidate.get("candidate_pdbqt")),
        "box": {key: box.get(key, "") for key in ("center_x", "center_y", "center_z", "size_x", "size_y", "size_z")},
        "vina_parameters": parameters,
    }
    encoded = json.dumps(components, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), parameters


def _empty_manifest(seed_pair_id: str = "") -> dict[str, Any]:
    return {
        "contract_version": VINA_JOB_MANIFEST_VERSION,
        "seed_pair_id": seed_pair_id,
        "jobs": [],
    }


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    ensure_dir(path.parent)
    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temp_path.replace(path)


def _legacy_result(row: dict[str, Any], *, source: Path) -> dict[str, Any]:
    generation_backend = str(row.get("generation_backend", "")).strip()
    scoring_engine = str(row.get("scoring_engine", "")).strip() or "unknown_legacy"
    execution_mode = str(row.get("execution_mode", "")).strip() or "unknown"
    result = {
        **row,
        "input_fingerprint": str(row.get("input_fingerprint", "")).strip(),
        "generation_backend": generation_backend,
        "scoring_engine": scoring_engine,
        "execution_mode": execution_mode,
        "fallback_reason": join_fallback_reasons(
            row.get("fallback_reason"),
            f"legacy_result_imported_without_manifest:{source.name}",
        ),
    }
    if not formal_result_eligible(result):
        result["accepted"] = False
        result["confidence_level"] = "provenance_unknown_legacy"
        result["stop_reason"] = "legacy_result_not_eligible_without_provenance"
    return result


def load_vina_job_manifest(
    path: Path,
    *,
    seed_pair_id: str = "",
    legacy_result_paths: Iterable[Path] = (),
) -> dict[str, Any]:
    if path.exists():
        try:
            manifest = read_json(path)
        except Exception as exc:  # noqa: BLE001 - corrupt budget state must fail closed.
            raise ReverseDesignError(f"Cannot read Vina job manifest {path}: {exc}") from exc
        if not isinstance(manifest, dict) or manifest.get("contract_version") != VINA_JOB_MANIFEST_VERSION:
            raise ReverseDesignError(f"Unsupported or invalid Vina job manifest: {path}")
        if not isinstance(manifest.get("jobs"), list) or not all(isinstance(item, dict) for item in manifest["jobs"]):
            raise ReverseDesignError(f"Invalid Vina job list in manifest: {path}")
    else:
        manifest = _empty_manifest(seed_pair_id)

    known = {str(job.get("job_id", "")) for job in manifest["jobs"]}
    changed = False
    for result_path in legacy_result_paths:
        if not result_path.exists():
            continue
        for row in read_rows(result_path):
            candidate_id = row.get("candidate_id", "")
            round_id = row.get("round", "") or result_path.parent.name.removeprefix("round_")
            row_seed = row.get("seed_pair_id", "") or seed_pair_id
            legacy_scoring_engine = str(row.get("scoring_engine", "")).strip() or "unknown_legacy"
            job_id = str(row.get("job_id", "")).strip() or _job_id(
                row_seed,
                round_id,
                candidate_id,
                legacy_scoring_engine,
            )
            if not candidate_id or job_id in known:
                continue
            result = _legacy_result(row, source=result_path)
            result["job_id"] = job_id
            result["job_status"] = str(row.get("job_status", "")).strip() or (
                "success" if str(row.get("status", "")).strip().lower() in {"success", "simulation_only"} else "failed"
            )
            scoring_engine = result["scoring_engine"]
            counts_toward_budget = scoring_engine != SIMULATION_SCORING_ENGINE
            manifest["jobs"].append(
                {
                    "job_id": job_id,
                    "candidate_id": candidate_id,
                    "seed_pair_id": row_seed,
                    "round": round_id,
                    "job_status": result["job_status"],
                    "counts_toward_vina_budget": counts_toward_budget,
                    "legacy_imported": True,
                    "input_fingerprint": result.get("input_fingerprint", ""),
                    "result": result,
                    "updated_at": _utc_now(),
                }
            )
            known.add(job_id)
            changed = True
    if changed or not path.exists():
        _write_manifest(path, manifest)
    return manifest


def count_vina_jobs(manifest: dict[str, Any]) -> int:
    return sum(1 for job in manifest.get("jobs", []) if truthy(job.get("counts_toward_vina_budget")))


def recover_pair_job_manifest(pair_dir: Path, *, seed_pair_id: str = "") -> dict[str, Any]:
    legacy_paths = sorted(pair_dir.glob("round_*/redocking_results.csv"))
    return load_vina_job_manifest(
        pair_dir / "vina_job_manifest.json",
        seed_pair_id=seed_pair_id,
        legacy_result_paths=legacy_paths,
    )


def count_vina_jobs_for_pair(pair_dir: Path, *, seed_pair_id: str = "") -> int:
    return count_vina_jobs(recover_pair_job_manifest(pair_dir, seed_pair_id=seed_pair_id))


def count_vina_jobs_in_tree(out_dir: Path) -> int:
    total = 0
    pair_dirs = {path.parent for path in out_dir.glob("pair_*/vina_job_manifest.json")}
    pair_dirs.update(path.parent.parent for path in out_dir.glob("pair_*/round_*/redocking_results.csv"))
    for pair_dir in sorted(pair_dirs):
        total += count_vina_jobs_for_pair(pair_dir)
    return total


def remaining_vina_job_budget(
    *,
    max_per_seed_pair: int,
    max_total: int,
    used_for_seed_pair: int,
    used_total: int,
    requested: int,
) -> int:
    return max(
        0,
        min(
            max(0, requested),
            max(0, max_per_seed_pair - used_for_seed_pair),
            max(0, max_total - used_total),
        ),
    )


def _interrupted_result(
    *,
    seed: dict[str, Any],
    row: dict[str, Any],
    job: dict[str, Any],
    cutoff: float,
) -> dict[str, Any]:
    generation_backend = str(job.get("generation_backend") or row.get("generation_backend") or "").strip()
    scoring_engine = str(job.get("scoring_engine") or "vina").strip()
    execution_mode = str(job.get("execution_mode") or combined_execution_mode(generation_backend, scoring_engine))
    return {
        "job_id": job.get("job_id", ""),
        "input_fingerprint": job.get("input_fingerprint", ""),
        "job_status": "interrupted",
        "candidate_id": row.get("candidate_id", ""),
        "seed_pair_id": seed.get("seed_pair_id", ""),
        "target_id": seed.get("target_id", ""),
        "parent_ligand_id": seed.get("ligand_id", ""),
        "generation_backend": generation_backend,
        "scoring_engine": scoring_engine,
        "execution_mode": execution_mode,
        "fallback_reason": join_fallback_reasons(row.get("fallback_reason"), job.get("fallback_reason")),
        "old_score": float(to_float(seed.get("old_score"), 0.0) or 0.0),
        "new_score": "",
        "score_improvement": "",
        "pass_score_cutoff": round(cutoff, 4),
        "score_threshold_met": False,
        "accepted": False,
        "confidence_level": "redock_failed",
        "round": row.get("round", ""),
        "status": "failed",
        "vina_pose_file": "",
        "vina_log_file": "",
        "vina_config_file": "",
        "simulation_artifact_file": "",
        "runtime_seconds": 0.0,
        "stop_reason": "previous_attempt_interrupted",
        "failure_reason": "previous_attempt_interrupted",
    }


def redock_candidates(
    *,
    seed: dict[str, Any],
    selected_for_vina_csv: Path,
    out_csv: Path,
    out_dir: Path,
    improvement_margin_kcal: float = 1.0,
    high_confidence_margin_kcal: float = 1.5,
    timeout_seconds: int = 1800,
    mode: str = "auto",
    max_jobs: int = 10,
    job_manifest_path: Path | None = None,
) -> list[dict[str, Any]]:
    if mode not in {"auto", "vina", "dry_run"}:
        raise ReverseDesignError(f"Unsupported redocking mode: {mode}")

    rows = [row for row in read_rows(selected_for_vina_csv) if row.get("vina_preparation_status") == "success"]
    ensure_dir(out_dir)
    vina = find_executable("vina")
    actual_vina = mode in {"auto", "vina"}
    if actual_vina and not vina:
        raise ReverseDesignError(
            f"Vina executable is required for formal redocking mode '{mode}'; "
            "use --redock-mode dry_run only for explicitly labelled simulation output."
        )

    scoring_engine = "vina" if actual_vina else SIMULATION_SCORING_ENGINE
    vina_cpu = get_vina_cpu_limit()
    old_score = float(to_float(seed.get("old_score"), 0.0) or 0.0)
    cutoff = old_score - improvement_margin_kcal
    box = read_json(Path(str(seed.get("prepared_pocket_box_json") or seed.get("pocket_box_json") or seed.get("box_json"))))

    if job_manifest_path is None:
        pair_dir = out_dir.parent.parent
        job_manifest_path = pair_dir / "vina_job_manifest.json"
    else:
        pair_dir = job_manifest_path.parent
    manifest = load_vina_job_manifest(
        job_manifest_path,
        seed_pair_id=str(seed.get("seed_pair_id", "")),
        legacy_result_paths=sorted(pair_dir.glob("round_*/redocking_results.csv")),
    )
    jobs_by_id = {str(job.get("job_id", "")): job for job in manifest["jobs"]}

    results: list[dict[str, Any]] = []
    new_jobs_started = 0
    for index, row in enumerate(rows, start=1):
        candidate_id = row.get("candidate_id", f"candidate_{index}")
        round_id = row.get("round", "") or out_csv.parent.name.removeprefix("round_")
        input_fingerprint, vina_parameters = _vina_input_fingerprint(
            seed=seed,
            candidate={**row, "candidate_id": candidate_id},
            box=box,
            scoring_engine=scoring_engine,
            vina_cpu=vina_cpu,
        )
        job_id = _job_id(
            seed.get("seed_pair_id", ""),
            round_id,
            candidate_id,
            scoring_engine,
            input_fingerprint,
        )
        existing = jobs_by_id.get(job_id)
        if existing is not None:
            existing_result = existing.get("result")
            if isinstance(existing_result, dict):
                results.append(existing_result)
                continue
            interrupted = _interrupted_result(seed=seed, row=row, job=existing, cutoff=cutoff)
            existing["job_status"] = "interrupted"
            existing["result"] = interrupted
            existing["updated_at"] = _utc_now()
            _write_manifest(job_manifest_path, manifest)
            results.append(interrupted)
            continue
        if new_jobs_started >= max(0, max_jobs):
            continue
        new_jobs_started += 1

        generation_backend = str(row.get("generation_backend", "")).strip()
        execution_mode = combined_execution_mode(generation_backend, scoring_engine)
        parent_reference = actual_vina and generation_backend == "parent_reference" and row.get("job_role") == "parent_baseline"
        if parent_reference:
            execution_mode = "reference"
        fallback_reason = join_fallback_reasons(row.get("fallback_reason"))
        candidate_dir = ensure_dir(out_dir / f"{candidate_id}_{input_fingerprint[:12]}")
        pose_file = candidate_dir / "pose.pdbqt"
        log_file = candidate_dir / "vina.log"
        config_file = candidate_dir / "config.txt"
        simulation_file = candidate_dir / "simulation_score.txt"
        for stale_artifact in (pose_file, log_file, config_file, simulation_file):
            stale_artifact.unlink(missing_ok=True)

        entry = {
            "job_id": job_id,
            "input_fingerprint": input_fingerprint,
            "input_fingerprint_version": VINA_INPUT_FINGERPRINT_VERSION,
            "candidate_id": candidate_id,
            "seed_pair_id": seed.get("seed_pair_id", ""),
            "round": round_id,
            "job_status": "running",
            "counts_toward_vina_budget": actual_vina,
            "generation_backend": generation_backend,
            "scoring_engine": scoring_engine,
            "execution_mode": execution_mode,
            "fallback_reason": fallback_reason,
            "started_at": _utc_now(),
            "result": None,
        }
        manifest["jobs"].append(entry)
        jobs_by_id[job_id] = entry
        _write_manifest(job_manifest_path, manifest)

        status = "success"
        failure = ""
        runtime = 0.0
        new_score: float | None = None
        if actual_vina:
            config_lines = [
                f"receptor = {seed.get('prepared_receptor_pdbqt') or seed.get('receptor_pdbqt')}",
                f"ligand = {row.get('candidate_pdbqt')}",
                f"out = {pose_file}",
                *_box_lines(box),
                f"exhaustiveness = {vina_parameters['exhaustiveness']}",
                f"num_modes = {vina_parameters['num_modes']}",
                f"energy_range = {vina_parameters['energy_range']}",
                f"cpu = {vina_parameters['cpu']}",
                f"seed = {vina_parameters['seed']}",
                "",
            ]
            config_file.write_text("\n".join(config_lines), encoding="utf-8")
            started = time.time()
            try:
                with log_file.open("w", encoding="utf-8") as handle:
                    completed = subprocess.run(
                        [vina, "--config", str(config_file)],
                        text=True,
                        stdout=handle,
                        stderr=subprocess.STDOUT,
                        timeout=remaining_timeout(timeout_seconds),
                    )
                runtime = round(time.time() - started, 3)
                if completed.returncode != 0:
                    status = "failed"
                    failure = f"vina_exit_code_{completed.returncode}"
                else:
                    new_score, _ = parse_vina_score(log_file)
                    if new_score is None:
                        status = "failed"
                        failure = "cannot_parse_vina_score"
                    elif not pose_file.exists() or pose_file.stat().st_size == 0:
                        status = "failed"
                        failure = "vina_pose_missing_or_empty"
                        new_score = None
            except subprocess.TimeoutExpired:
                runtime = round(time.time() - started, 3)
                status = "failed"
                failure = "vina_timeout"
                with log_file.open("a", encoding="utf-8") as handle:
                    handle.write("\nVina timeout; original output retained\n")
            except OSError as exc:
                runtime = round(time.time() - started, 3)
                status = "failed"
                failure = f"vina_execution_error:{exc}"
        else:
            new_score = _simulate_score(seed, candidate_id, old_score)
            simulation_file.write_text(
                "SIMULATION ONLY - not a Vina result and not eligible for acceptance\n"
                f"deterministic_score={new_score}\n",
                encoding="utf-8",
            )

        improvement = old_score - new_score if new_score is not None else None
        threshold_met = bool(new_score is not None and new_score <= cutoff)
        row_status = status if status == "failed" else ("success" if execution_mode == "production" else "simulation_only")
        if parent_reference and status == "success":
            row_status = "reference_only"
        provisional = {
            "status": row_status,
            "generation_backend": generation_backend,
            "scoring_engine": scoring_engine,
            "execution_mode": execution_mode,
        }
        accepted = bool(threshold_met and formal_result_eligible(provisional))
        protocol_id, protocol = "", {}
        comparison_status = "unverified_legacy_protocol"
        if actual_vina:
            protocol_id, protocol = docking_protocol(
                seed.get("prepared_receptor_pdbqt") or seed.get("receptor_pdbqt"), box, vina, vina_parameters,
                row.get("ligand_ph_requested", seed.get("ligand_ph_requested")),
                row.get("ligand_protonation_status", "unknown"))
            if protocol_id == seed.get("comparison_protocol_id"):
                comparison_status = "matched_parent_protocol"
        comparable = comparison_status == "matched_parent_protocol"
        # Legacy inputs remain readable and scoreable, but cannot assert improvement.
        if not comparable:
            accepted = False
            improvement = None
        if status == "failed":
            confidence = "redock_failed"
            stop_reason = failure or "redock_failed"
        elif parent_reference:
            confidence = "parent_reference_only"
            stop_reason = "baseline_not_a_generated_candidate"
        elif execution_mode != "production":
            confidence = "simulation_only"
            stop_reason = "simulation_result_not_eligible_for_acceptance"
        elif improvement is not None and improvement >= high_confidence_margin_kcal:
            confidence = "high_confidence_accepted"
            stop_reason = "accepted_score_improved"
        elif improvement is not None and improvement >= improvement_margin_kcal:
            confidence = "accepted"
            stop_reason = "accepted_score_improved"
        else:
            confidence = "not_enough_improvement"
            stop_reason = "not_enough_improvement"
        if actual_vina and status == "success" and not comparable:
            confidence = "raw_score_only"
            stop_reason = "comparison_protocol_unconfirmed"

        job_status = "success" if status == "success" else ("timeout" if failure == "vina_timeout" else "failed")
        result = {
            **{key: row.get(key, "") for key in TRACE_FIELDS},
            "comparison_protocol_id": protocol_id, "comparison_protocol": json.dumps(protocol, sort_keys=True),
            "comparison_status": comparison_status, "baseline_origin": seed.get("baseline_origin", "main_docking"),
            "job_role": row.get("job_role", "candidate"),
            "job_id": job_id,
            "input_fingerprint": input_fingerprint,
            "job_status": job_status,
            "candidate_id": candidate_id,
            "seed_pair_id": seed.get("seed_pair_id", ""),
            "target_id": seed.get("target_id", ""),
            "parent_ligand_id": seed.get("ligand_id", ""),
            "generation_backend": generation_backend,
            "scoring_engine": scoring_engine,
            "execution_mode": execution_mode,
            "fallback_reason": fallback_reason,
            "old_score": old_score,
            "new_score": "" if new_score is None else new_score,
            "score_improvement": "" if improvement is None else round(improvement, 4),
            "pass_score_cutoff": round(cutoff, 4),
            "score_threshold_met": threshold_met,
            "accepted": accepted,
            "confidence_level": confidence,
            "round": round_id,
            "status": row_status,
            "vina_pose_file": str(pose_file) if actual_vina and pose_file.exists() else "",
            "vina_log_file": str(log_file) if actual_vina and log_file.exists() else "",
            "vina_config_file": str(config_file) if actual_vina and config_file.exists() else "",
            "simulation_artifact_file": str(simulation_file) if simulation_file.exists() else "",
            "runtime_seconds": runtime,
            "stop_reason": stop_reason,
            "failure_reason": failure,
        }
        entry["job_status"] = job_status
        entry["result"] = result
        entry["updated_at"] = _utc_now()
        _write_manifest(job_manifest_path, manifest)
        results.append(result)

    write_rows(out_csv, results, REDOCK_FIELDS)
    return results
