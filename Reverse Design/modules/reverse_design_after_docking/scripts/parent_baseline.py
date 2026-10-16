"""Reuse a verified parent protocol or spend one existing-budget job on a baseline."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .common import deterministic_seed, find_executable, read_json, to_float, write_json, write_rows
from .prepare_candidates_for_vina import prepare_candidates_for_vina
from .redock_candidates import redock_candidates, get_vina_cpu_limit, _numeric_vina_seed

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from npflow_chemistry import docking_protocol


def ensure_parent_baseline(seed: dict, pair_dir: Path, *, budget: int, timeout: int) -> None:
    seed.setdefault("original_main_docking_score", seed.get("old_score"))
    vina = find_executable("vina")
    box = read_json(Path(seed.get("pocket_box_json") or seed.get("box_json")))
    params = {"exhaustiveness": seed.get("exhaustiveness", 8), "num_modes": seed.get("num_modes", 9),
              "energy_range": seed.get("energy_range", 3), "cpu": get_vina_cpu_limit(),
              "seed": _numeric_vina_seed(seed, "parent_baseline")}
    if not str(seed.get("random_seed", "")).isdigit():
        seed["random_seed"] = params["seed"]
    identity, _ = docking_protocol(seed["receptor_pdbqt"], box, vina, params,
                                  seed.get("ligand_ph_requested"), seed.get("parent_protonation_status", "unknown"))
    if identity == seed.get("comparison_protocol_id") and seed.get("parent_protonation_status") in {"applied", "skipped_not_requested"}:
        seed["baseline_origin"] = "verified_main_docking_reused"
        return
    seed["comparison_protocol_id"] = ""
    seed["baseline_origin"] = "unavailable_raw_scores_only"
    if budget <= 0:
        return
    directory = pair_dir / "parent_baseline"
    directory.mkdir(parents=True, exist_ok=True)
    parent = {"candidate_id": "parent_baseline", "seed_pair_id": seed.get("seed_pair_id"),
              "round": "baseline", "canonical_smiles": seed.get("smiles"), "filter_status": "pass",
              "generation_backend": "parent_reference", "job_role": "parent_baseline"}
    write_rows(directory / "input.csv", [parent], list(parent))
    ph = seed.get("ligand_ph_requested")
    prepared = prepare_candidates_for_vina(filtered_csv=directory / "input.csv", out_csv=directory / "prepared.csv",
        out_dir=directory, limit=1, ph=None if ph in (None, "") else float(ph))
    if not prepared or prepared[0].get("vina_preparation_status") != "success":
        return
    prepared[0]["job_role"] = "parent_baseline"
    write_rows(directory / "prepared.csv", prepared, list(prepared[0]))
    result = redock_candidates(seed=seed, selected_for_vina_csv=directory / "prepared.csv",
        out_csv=directory / "result.csv", out_dir=directory / "redocking", mode="vina", max_jobs=1,
        timeout_seconds=timeout, job_manifest_path=pair_dir / "vina_job_manifest.json")
    if result and result[0].get("job_status") == "success" and to_float(result[0].get("new_score")) is not None:
        seed.update({"old_score": result[0]["new_score"], "comparison_protocol_id": result[0]["comparison_protocol_id"],
                     "comparison_protocol": result[0]["comparison_protocol"], "baseline_origin": "parent_recomputed_within_existing_budget"})
    write_json(directory / "baseline_context.json", {key: seed.get(key, "") for key in (
        "original_main_docking_score", "old_score", "comparison_protocol_id", "comparison_protocol", "baseline_origin")})
