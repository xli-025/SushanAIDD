#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # Reverse Design/

from modules.reverse_design_after_docking.backends.deepfrag2_backend import DeepFrag2Backend
from modules.reverse_design_after_docking.backends.dry_run_backend import DryRunBackend, FRAGMENT_FIELDS
from modules.reverse_design_after_docking.scripts.assemble_candidates import assemble_candidates
from modules.reverse_design_after_docking.scripts.check_environment import check_environment
from modules.reverse_design_after_docking.scripts.common import ReverseDesignError, ensure_dir, write_rows
from modules.reverse_design_after_docking.scripts.compare_and_accept import compare_and_accept
from modules.reverse_design_after_docking.scripts.prepare_candidates_for_vina import prepare_candidates_for_vina
from modules.reverse_design_after_docking.scripts.prepare_complex import prepare_complex
from modules.reverse_design_after_docking.scripts.parent_baseline import ensure_parent_baseline
from modules.reverse_design_after_docking.scripts.provenance import join_fallback_reasons, stamp_generation_provenance
from modules.reverse_design_after_docking.scripts.redock_candidates import (
    count_vina_jobs_for_pair,
    count_vina_jobs_in_tree,
    redock_candidates,
    remaining_vina_job_budget,
)
from modules.reverse_design_after_docking.scripts.sanitize_candidates import sanitize_candidates
from modules.reverse_design_after_docking.scripts.select_attachment_points import select_attachment_points
from modules.reverse_design_after_docking.scripts.select_seed_pairs import select_seed_pairs
from modules.reverse_design_after_docking.scripts.summarize_reverse_design import summarize_reverse_design


def _default_out_dir(dock_dir: Path) -> Path:
    return dock_dir / "09_reverse_design_after_docking"


DEEPFRAG2_ENV_BIN = Path(__file__).resolve().parents[2] / ".deepfrag2_env" / "bin"


def _backend(name: str, environment: dict[str, Any], warnings: list[str]):
    backend_obj: Any
    backend_name: str
    if name == "deepfrag2":
        # Check if the standalone deepfrag2 env has the executable
        df2_exe = DEEPFRAG2_ENV_BIN / "deepfrag2"
        df2_ok = df2_exe.exists() and environment.get("deepfrag2_env_ok")
        if df2_ok:
            backend_obj = DeepFrag2Backend()
            backend_name = "deepfrag2"
            return backend_obj, backend_name, ""
        else:
            reason = "env_missing" if not df2_exe.exists() else "test_failed"
            warnings.append(f"deepfrag2_unavailable_fallback_to_dry_run:{reason}")
            fallback_reason = f"deepfrag2_unavailable:{reason}"
    else:
        fallback_reason = ""
    # Fallback to dry_run
    backend_obj = DryRunBackend()
    backend_name = "dry_run"
    return backend_obj, backend_name, fallback_reason


def run(args: argparse.Namespace) -> None:
    dock_dir = Path(args.dock_dir).resolve()
    if not dock_dir.exists():
        raise ReverseDesignError(f"Docking directory not found: {dock_dir}")
    out_dir = ensure_dir(Path(args.out_dir).resolve() if args.out_dir else _default_out_dir(dock_dir))
    warnings: list[str] = []
    errors: list[str] = []
    environment = check_environment(out_dir, run_deepfrag2_test=not args.skip_deepfrag2_test)
    backend, backend_name, backend_fallback_reason = _backend(args.backend, environment, warnings)
    max_rounds = min(args.max_rounds, args.hard_max_rounds)

    seeds = select_seed_pairs(
        dock_dir,
        out_dir,
        vina_score_cutoff=args.vina_score_cutoff,
        max_seed_pairs=args.max_seed_pairs,
        require_usable_for_scientific_ranking=not args.allow_low_confidence_seeds,
        prefer_source=args.prefer_source,
    )
    if not seeds:
        summarize_reverse_design(
            out_dir=out_dir,
            environment=environment,
            selected_seeds=[],
            improvement_margin_kcal=args.improvement_margin_kcal,
            max_seed_pairs=args.max_seed_pairs,
            max_rounds=max_rounds,
            backend_name=backend_name,
            warnings=warnings,
            errors=errors,
        )
        print("Reverse design skipped: no seed pairs met the Vina score cutoff.")
        print(f"Output: {out_dir}")
        return

    formal_redocking = args.redock_mode in {"auto", "vina"}
    if formal_redocking and not environment.get("can_run_vina_redocking"):
        errors.append(f"vina_unavailable_for_formal_redocking:{args.redock_mode}")
        qc = summarize_reverse_design(
            out_dir=out_dir,
            environment=environment,
            selected_seeds=seeds,
            improvement_margin_kcal=args.improvement_margin_kcal,
            max_seed_pairs=args.max_seed_pairs,
            max_rounds=max_rounds,
            backend_name=backend_name,
            warnings=warnings,
            errors=errors,
        )
        print(f"Reverse design status: {qc['status']}")
        print(f"Output: {out_dir}")
        raise ReverseDesignError("Vina is required for formal reverse-design redocking.")

    total_vina_jobs = count_vina_jobs_in_tree(out_dir)
    for seed in seeds:
        try:
            prepared = prepare_complex(seed, out_dir)
            seed.update(prepared)
            accepted_for_seed = False
            pair_dir = Path(str(prepared["pair_dir"]))
            if formal_redocking:
                before_baseline = count_vina_jobs_for_pair(pair_dir, seed_pair_id=str(seed.get("seed_pair_id", "")))
                baseline_budget = remaining_vina_job_budget(max_per_seed_pair=args.max_vina_jobs_per_seed_pair,
                    max_total=args.max_total_vina_jobs, used_for_seed_pair=before_baseline,
                    used_total=total_vina_jobs, requested=1)
                ensure_parent_baseline(seed, pair_dir, budget=baseline_budget, timeout=args.timeout_per_vina)
                total_vina_jobs += max(0, count_vina_jobs_for_pair(pair_dir, seed_pair_id=str(seed.get("seed_pair_id", ""))) - before_baseline)
            for round_id in range(1, max_rounds + 1):
                if formal_redocking and total_vina_jobs >= args.max_total_vina_jobs:
                    warnings.append("max_total_vina_jobs_reached")
                    break
                round_dir = ensure_dir(pair_dir / f"round_{round_id}")
                attachments = select_attachment_points(
                    seed_pair_id=str(seed.get("seed_pair_id", "")),
                    parent_ligand_sdf=Path(str(prepared.get("parent_best_pose_sdf") or prepared.get("parent_ligand_sdf"))),
                    receptor_pdb=Path(str(prepared.get("receptor_pdb") or prepared.get("prepared_receptor_pdb"))),
                    out_csv=round_dir / "attachment_points.csv",
                    top_k=args.top_k_attachment_points,
                )
                if not attachments:
                    warnings.append(f"{seed.get('seed_pair_id')}:no_valid_attachment_point")
                    break
                try:
                    fragments = backend.generate_fragments(
                        seed=seed,
                        round_id=round_id,
                        attachment_points=attachments,
                        out_dir=round_dir,
                        fragments_per_attachment_point=args.fragments_per_attachment_point,
                    )
                    round_backend_name = backend_name
                    round_fallback_reason = backend_fallback_reason
                except Exception as exc:  # noqa: BLE001 - per-call fallback, not global.
                    warnings.append(f"{seed.get('seed_pair_id')}:r{round_id}_backend_failed:{exc}")
                    # Use a temporary dry_run fallback ONLY for this call.
                    # Do NOT overwrite the global backend/backend_name — other
                    # seed-pair / round iterations should still try the real backend.
                    fallback_backend = DryRunBackend()
                    fragments = fallback_backend.generate_fragments(
                        seed=seed,
                        round_id=round_id,
                        attachment_points=attachments,
                        out_dir=round_dir,
                        fragments_per_attachment_point=args.fragments_per_attachment_point,
                    )
                    round_backend_name = fallback_backend.name
                    round_fallback_reason = join_fallback_reasons(
                        backend_fallback_reason,
                        f"{backend_name}_runtime_failure:{type(exc).__name__}:{exc}",
                    )
                    if fragments:
                        warnings.append(f"{seed.get('seed_pair_id')}:r{round_id}_used_dry_run_fallback")
                fragments = stamp_generation_provenance(
                    fragments,
                    generation_backend=round_backend_name,
                    fallback_reason=round_fallback_reason,
                )
                write_rows(round_dir / "deepfrag2_fragments.csv", fragments, FRAGMENT_FIELDS)
                if not fragments:
                    warnings.append(f"{seed.get('seed_pair_id')}:no_fragments_generated")
                    break
                assemble_candidates(
                    seed=seed,
                    fragments_csv=round_dir / "deepfrag2_fragments.csv",
                    attachment_points_csv=round_dir / "attachment_points.csv",
                    out_csv=round_dir / "generated_raw.csv",
                )
                sanitize_candidates(
                    seed=seed,
                    generated_raw_csv=round_dir / "generated_raw.csv",
                    out_csv=round_dir / "generated_filtered.csv",
                    qed_min=args.qed_min,
                    max_mw=args.max_mw,
                    min_parent_similarity=args.min_parent_similarity,
                    max_heavy_atom_delta=args.max_heavy_atom_delta,
                    reject_pains=not args.allow_pains,
                )
                selected_for_vina = prepare_candidates_for_vina(
                    filtered_csv=round_dir / "generated_filtered.csv",
                    out_csv=round_dir / "selected_for_vina.csv",
                    out_dir=round_dir,
                    limit=args.vina_candidates_per_round,
                    ph=float(seed["ligand_ph_requested"]) if str(seed.get("ligand_ph_requested", "")).strip() else None,
                )
                success_vina_inputs = [row for row in selected_for_vina if row.get("vina_preparation_status") == "success"]
                if not success_vina_inputs:
                    warnings.append(f"{seed.get('seed_pair_id')}:no_candidates_prepared_for_vina")
                    continue
                pair_vina_jobs_before = count_vina_jobs_for_pair(
                    pair_dir,
                    seed_pair_id=str(seed.get("seed_pair_id", "")),
                )
                if formal_redocking:
                    max_jobs = remaining_vina_job_budget(
                        max_per_seed_pair=args.max_vina_jobs_per_seed_pair,
                        max_total=args.max_total_vina_jobs,
                        used_for_seed_pair=pair_vina_jobs_before,
                        used_total=total_vina_jobs,
                        requested=args.vina_candidates_per_round,
                    )
                else:
                    max_jobs = min(args.vina_candidates_per_round, len(success_vina_inputs))
                redocking_csv = round_dir / (
                    "redocking_results.csv" if formal_redocking else "simulation_redocking_results.csv"
                )
                redocked = redock_candidates(
                    seed=seed,
                    selected_for_vina_csv=round_dir / "selected_for_vina.csv",
                    out_csv=redocking_csv,
                    out_dir=round_dir / ("redocking" if formal_redocking else "simulation_redocking"),
                    improvement_margin_kcal=args.improvement_margin_kcal,
                    high_confidence_margin_kcal=args.high_confidence_margin_kcal,
                    timeout_seconds=args.timeout_per_vina,
                    mode=args.redock_mode,
                    max_jobs=max_jobs,
                    job_manifest_path=pair_dir / "vina_job_manifest.json",
                )
                pair_vina_jobs_after = count_vina_jobs_for_pair(
                    pair_dir,
                    seed_pair_id=str(seed.get("seed_pair_id", "")),
                )
                total_vina_jobs += max(0, pair_vina_jobs_after - pair_vina_jobs_before)
                _, accepted = compare_and_accept(
                    redocking_csv=redocking_csv,
                    accepted_csv=round_dir / "accepted_candidates.csv",
                    improvement_margin_kcal=args.improvement_margin_kcal,
                    high_confidence_margin_kcal=args.high_confidence_margin_kcal,
                )
                if accepted and args.stop_on_first_accepted_candidate:
                    accepted_for_seed = True
                    break
            if not accepted_for_seed:
                warnings.append(f"{seed.get('seed_pair_id')}:failed_optimization_or_no_accepted_candidate")
        except Exception as exc:  # noqa: BLE001 - pair-level failure must not stop the whole module.
            errors.append(f"{seed.get('seed_pair_id')}: {exc}")

    qc = summarize_reverse_design(
        out_dir=out_dir,
        environment=environment,
        selected_seeds=seeds,
        improvement_margin_kcal=args.improvement_margin_kcal,
        max_seed_pairs=args.max_seed_pairs,
        max_rounds=max_rounds,
        backend_name=backend_name,
        warnings=warnings,
        errors=errors,
    )
    print(f"Reverse design status: {qc['status']}")
    print(f"Output: {out_dir}")
    print(f"Accepted candidates: {qc['n_accepted']}")
    if qc["status"] == "failed":
        raise ReverseDesignError("Reverse design failed; see reverse_design_qc.json for details.")


def check_env(args: argparse.Namespace) -> None:
    out_dir = ensure_dir(Path(args.out_dir).resolve())
    report = check_environment(out_dir, run_deepfrag2_test=not args.skip_deepfrag2_test)
    print(f"Environment report: {out_dir / 'environment_check_report.txt'}")
    if report["blocking_missing_dependencies"]:
        print("Blocking missing dependencies:")
        for dep in report["blocking_missing_dependencies"]:
            print(f"- {dep}")
        raise SystemExit(2)


def select_seeds(args: argparse.Namespace) -> None:
    rows = select_seed_pairs(
        Path(args.dock_dir),
        Path(args.out_dir),
        vina_score_cutoff=args.vina_score_cutoff,
        max_seed_pairs=args.max_seed_pairs,
        require_usable_for_scientific_ranking=not args.allow_low_confidence_seeds,
        prefer_source=args.prefer_source,
    )
    print(f"Selected seed pairs: {len(rows)}")
    print(f"Output: {Path(args.out_dir) / 'selected_seed_pairs.csv'}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reverse design after docking automation.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    env_parser = subparsers.add_parser("check-env", help="Check reverse design dependencies.")
    env_parser.add_argument("--out-dir", default="results/reverse_design_environment")
    env_parser.add_argument("--skip-deepfrag2-test", action="store_true")
    env_parser.set_defaults(func=check_env)

    seed_parser = subparsers.add_parser("select-seeds", help="Select docking seed pairs.")
    seed_parser.add_argument("--dock-dir", required=True)
    seed_parser.add_argument("--out-dir", required=True)
    seed_parser.add_argument("--vina-score-cutoff", type=float, default=-6.0)
    seed_parser.add_argument("--max-seed-pairs", type=int, default=3)
    seed_parser.add_argument("--allow-low-confidence-seeds", action="store_true")
    seed_parser.add_argument("--prefer-source", default="natural_compound")
    seed_parser.set_defaults(func=select_seeds)

    run_parser = subparsers.add_parser("run", help="Run reverse design after a docking run.")
    run_parser.add_argument("--dock-dir", required=True)
    run_parser.add_argument("--out-dir")
    run_parser.add_argument("--backend", choices=["dry_run", "deepfrag2"], default="dry_run")
    run_parser.add_argument("--redock-mode", choices=["auto", "vina", "dry_run"], default="auto")
    run_parser.add_argument("--skip-deepfrag2-test", action="store_true")
    run_parser.add_argument("--vina-score-cutoff", type=float, default=-6.0)
    run_parser.add_argument("--max-seed-pairs", type=int, default=3)
    run_parser.add_argument("--prefer-source", default="natural_compound")
    run_parser.add_argument("--allow-low-confidence-seeds", action="store_true")
    run_parser.add_argument("--fragments-per-attachment-point", type=int, default=10)
    run_parser.add_argument("--top-k-attachment-points", type=int, default=3)
    run_parser.add_argument("--vina-candidates-per-round", type=int, default=10)
    run_parser.add_argument("--max-rounds", type=int, default=3)
    run_parser.add_argument("--hard-max-rounds", type=int, default=4)
    run_parser.add_argument("--max-vina-jobs-per-seed-pair", type=int, default=30)
    run_parser.add_argument("--max-total-vina-jobs", type=int, default=90)
    run_parser.add_argument("--improvement-margin-kcal", type=float, default=0.8)
    run_parser.add_argument("--high-confidence-margin-kcal", type=float, default=1.5)
    run_parser.add_argument("--qed-min", type=float, default=0.30)
    run_parser.add_argument("--max-mw", type=float, default=650.0)
    run_parser.add_argument("--min-parent-similarity", type=float, default=0.35)
    run_parser.add_argument("--max-heavy-atom-delta", type=int, default=8)
    run_parser.add_argument("--allow-pains", action="store_true")
    run_parser.add_argument("--timeout-per-vina", type=int, default=1800)
    run_parser.add_argument("--stop-on-first-accepted-candidate", action=argparse.BooleanOptionalAction, default=True)
    run_parser.set_defaults(func=run)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except ReverseDesignError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
