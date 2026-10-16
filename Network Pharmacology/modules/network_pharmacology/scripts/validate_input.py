#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is not installed. Run: python -m pip install pyyaml", file=sys.stderr)
    sys.exit(1)


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def load_yaml(path: Path) -> dict:
    if not path.exists():
        fail(f"Config file does not exist: {path}")

    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception as exc:
        fail(f"Failed to read YAML config: {exc}")

    if not isinstance(data, dict):
        fail("YAML config must be a dictionary/object.")

    return data


def require_section(config: dict, section: str) -> dict:
    value = config.get(section)
    if not isinstance(value, dict):
        fail(f"Missing or invalid section: {section}")
    return value


def require_nonempty_str(section: dict, key: str, section_name: str) -> str:
    value = section.get(key)
    if not isinstance(value, str) or not value.strip():
        fail(f"Missing or empty field: {section_name}.{key}")
    return value.strip()


def require_bool(section: dict, key: str, section_name: str) -> bool:
    """Require a real YAML boolean, never a truthy string or integer."""
    value = section.get(key)
    if type(value) is not bool:
        fail(f"{section_name}.{key} must be a boolean (true or false).")
    return value


def require_finite_number(section: dict, key: str, section_name: str, *, minimum: float, maximum: float | None = None) -> float:
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        fail(f"{section_name}.{key} must be a finite number.")
    number = float(value)
    if number < minimum or (maximum is not None and number > maximum):
        range_text = f"between {minimum} and {maximum}" if maximum is not None else f">= {minimum}"
        fail(f"{section_name}.{key} must be {range_text}.")
    return number


def validate_weights(section: dict, section_name: str, required_keys: set[str]) -> None:
    missing = required_keys - set(section)
    if missing:
        fail(f"{section_name} is missing weights: {', '.join(sorted(missing))}.")
    total = 0.0
    for key in required_keys:
        total += require_finite_number(section, key, section_name, minimum=0.0, maximum=1.0)
    if not math.isclose(total, 1.0, abs_tol=1e-6):
        fail(f"{section_name} weights must sum to 1.0; got {total:.6g}.")


def normalize_selection_config(config: dict) -> tuple[dict, dict, dict]:
    """Return resolved selection sections while preserving legacy configs.

    v2 uses selection.docking_targets/docking_ligands.  The legacy
    core_target_selection section is accepted only as a compatibility input and
    is materialized into the resolved configuration so every downstream stage
    sees one deterministic effective contract.
    """
    selection = config.get("selection")
    legacy = config.get("core_target_selection")
    if not isinstance(selection, dict):
        selection = {}
    if not isinstance(legacy, dict):
        legacy = {}

    docking_targets = selection.get("docking_targets")
    if not isinstance(docking_targets, dict):
        docking_targets = {
            "top_n": legacy.get("top_n", 20),
            "min_target_score": legacy.get("min_target_score", 0.55),
            "min_target_confidence": legacy.get("min_target_confidence", 0.55),
            "require_disease_intersection": True,
            "require_human_target": True,
        }
    docking_ligands = selection.get("docking_ligands")
    if not isinstance(docking_ligands, dict):
        docking_ligands = {
            "top_n": 20,
            "min_ligand_score": 0.55,
            "min_ligand_confidence": 0.55,
            "require_structure": True,
            "require_linked_docking_target": True,
        }

    resolved_legacy = {
        "top_n": docking_targets.get("top_n", legacy.get("top_n", 20)),
        "ranking_method": legacy.get("ranking_method", "weighted_multifactor_v2"),
        "metrics": legacy.get(
            "metrics",
            ["target_score", "disease_association_score", "compound_count"],
        ),
    }
    resolved_selection = {
        **selection,
        "contract_version": selection.get("contract_version", "docking_selection.v2"),
        "docking_targets": docking_targets,
        "docking_ligands": docking_ligands,
    }
    return resolved_selection, resolved_legacy, docking_targets


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate input parameters for the network pharmacology module."
    )
    parser.add_argument(
        "--config",
        default="config/params.yaml",
        help="Path to params.yaml. Default: config/params.yaml",
    )
    args = parser.parse_args()

    project_root = Path.cwd()
    config_path = Path(args.config)

    config = load_yaml(config_path)

    project = require_section(config, "project")
    user_input = require_section(config, "input")
    data_sources = require_section(config, "data_sources")
    compound_filter = require_section(config, "compound_filter")
    ppi = require_section(config, "ppi")
    output = require_section(config, "output")
    disease_mapping = require_section(config, "disease_mapping")
    selection, core_target_selection, docking_targets = normalize_selection_config(config)

    project_name = require_nonempty_str(project, "name", "project")
    organism = require_nonempty_str(project, "organism", "project")
    input_mode = str(user_input.get("mode", "herb")).strip().lower() or "herb"
    if input_mode not in {"herb", "compound"}:
        fail("input.mode must be either 'herb' or 'compound'.")
    herb_name = ""
    compound_name = ""
    if input_mode == "compound":
        compound_name = require_nonempty_str(user_input, "compound_name", "input")
    else:
        herb_name = require_nonempty_str(user_input, "herb_name", "input")
    disease_name = require_nonempty_str(user_input, "disease_name", "input")
    output_dir_raw = require_nonempty_str(output, "dir", "output")

    taxon_id = project.get("taxon_id")
    if taxon_id != 9606:
        fail("Only Homo sapiens / taxon_id 9606 is supported in v1.0 MVP.")

    if organism.lower() not in {"homo sapiens", "human"}:
        fail("Only Homo sapiens is supported in v1.0 MVP.")

    require_finite_number(compound_filter, "ob_min", "compound_filter", minimum=0.0)
    require_finite_number(compound_filter, "dl_min", "compound_filter", minimum=0.0)

    require_finite_number(ppi, "string_required_score", "ppi", minimum=0.0, maximum=1.0)

    require_exact_match = require_bool(
        disease_mapping, "require_exact_match", "disease_mapping"
    )
    allow_nonexact_match = require_bool(
        disease_mapping, "allow_nonexact_match", "disease_mapping"
    )
    require_bool(disease_mapping, "allow_broad_term", "disease_mapping")
    if require_exact_match and allow_nonexact_match:
        fail(
            "disease_mapping.require_exact_match and "
            "disease_mapping.allow_nonexact_match cannot both be true."
        )

    top_n = docking_targets.get("top_n")
    if not isinstance(top_n, int) or top_n <= 0:
        fail("selection.docking_targets.top_n must be a positive integer.")

    metrics = core_target_selection.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        fail("core_target_selection.metrics must be a non-empty list.")

    ligand_selection = selection["docking_ligands"]
    ligand_top_n = ligand_selection.get("top_n")
    if not isinstance(ligand_top_n, int) or ligand_top_n <= 0:
        fail("selection.docking_ligands.top_n must be a positive integer.")
    for key in ("require_structure", "require_linked_docking_target"):
        require_bool(ligand_selection, key, "selection.docking_ligands")
    for key in ("require_disease_intersection", "require_human_target"):
        require_bool(docking_targets, key, "selection.docking_targets")
    for section_name, section, score_key, confidence_key in (
        ("selection.docking_targets", docking_targets, "min_target_score", "min_target_confidence"),
        ("selection.docking_ligands", ligand_selection, "min_ligand_score", "min_ligand_confidence"),
    ):
        require_finite_number(section, score_key, section_name, minimum=0.0, maximum=1.0)
        require_finite_number(section, confidence_key, section_name, minimum=0.0, maximum=1.0)

    ranking = config.get("ranking", {})
    if ranking:
        if not isinstance(ranking, dict):
            fail("ranking must be an object.")
        validate_weights(
            require_section(ranking, "ligand_score_weights"),
            "ranking.ligand_score_weights",
            {"ingredient_confidence", "structure_quality", "target_link", "disease_relevance", "multi_source_support"},
        )
        validate_weights(
            require_section(ranking, "target_score_weights"),
            "ranking.target_score_weights",
            {"source_confidence", "disease_association", "ppi_topology", "compound_coverage", "multi_source_support"},
        )

    source_weights = config.get("source_weights", {})
    if source_weights:
        if not isinstance(source_weights, dict):
            fail("source_weights must be an object.")
        herbiv = source_weights.get("herbiv", {})
        if herbiv:
            if not isinstance(herbiv, dict):
                fail("source_weights.herbiv must be an object.")
            candidate = require_finite_number(
                herbiv,
                "min_score_for_candidate",
                "source_weights.herbiv",
                minimum=0.0,
                maximum=1000.0,
            )
            exploratory_min = require_finite_number(
                herbiv,
                "exploratory_min_score",
                "source_weights.herbiv",
                minimum=0.0,
                maximum=1000.0,
            )
            exploratory_max = require_finite_number(
                herbiv,
                "exploratory_max_score",
                "source_weights.herbiv",
                minimum=0.0,
                maximum=1000.0,
            )
            if exploratory_min > exploratory_max or exploratory_max >= candidate:
                fail(
                    "source_weights.herbiv thresholds must satisfy "
                    "exploratory_min_score <= exploratory_max_score < min_score_for_candidate."
                )
    output_dir = project_root / output_dir_raw
    input_out_dir = output_dir / "00_input"
    input_out_dir.mkdir(parents=True, exist_ok=True)

    resolved_config = copy.deepcopy(config)
    resolved_config["schema_version"] = str(config.get("schema_version") or "npflow.network.v2")
    resolved_config["selection"] = selection
    resolved_config["core_target_selection"] = core_target_selection
    resolved_config["runtime"] = {
        "validated_at": datetime.now(timezone.utc).isoformat(),
        "project_root": str(project_root),
        "config_path": str(config_path),
        "output_dir": str(output_dir),
    }

    resolved_params_path = input_out_dir / "params.resolved.yaml"
    input_summary_path = input_out_dir / "input_summary.json"

    with resolved_params_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(resolved_config, f, allow_unicode=True, sort_keys=False)

    summary = {
        "module": "network_pharmacology",
        "status": "passed",
        "project_name": project_name,
        "input_mode": input_mode,
        "herb_name": herb_name,
        "compound_name": compound_name,
        "disease_name": disease_name,
        "organism": organism,
        "taxon_id": taxon_id,
        "data_sources": data_sources,
        "compound_filter": compound_filter,
        "ppi": ppi,
        "selection": selection,
        "core_target_selection": core_target_selection,
        "output_dir": str(output_dir),
        "generated_files": [
            str(resolved_params_path),
            str(input_summary_path),
        ],
        "warnings": [],
    }

    with input_summary_path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print("Input validation passed.")
    print(f"Resolved params: {resolved_params_path}")
    print(f"Input summary:   {input_summary_path}")


if __name__ == "__main__":
    main()
