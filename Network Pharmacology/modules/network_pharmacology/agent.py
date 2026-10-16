#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from modules.network_pharmacology.scripts.common import read_json
from npflow_runtime import remaining_timeout


STEPS = [
    ("validate_input", "modules/network_pharmacology/scripts/validate_input.py"),
    ("fetch_herb_compounds", "modules/network_pharmacology/scripts/fetch_herb_compounds.py"),
    ("fetch_disease_targets", "modules/network_pharmacology/scripts/fetch_disease_targets.py"),
    ("intersect_targets", "modules/network_pharmacology/scripts/intersect_targets.py"),
    ("build_ppi", "modules/network_pharmacology/scripts/build_ppi.py"),
    ("run_enrichment", "modules/network_pharmacology/scripts/run_enrichment.py"),
    ("rank_core_targets", "modules/network_pharmacology/scripts/rank_core_targets.py"),
    ("generate_report", "modules/network_pharmacology/scripts/generate_report.py"),
]


def config_output_dir(config: str) -> Path:
    with Path(config).open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise SystemExit(f"Config must be a YAML object: {config}")
    output_dir = data.get("output", {}).get("dir", "results/network_pharmacology")
    return Path(output_dir)


def should_use_pubchem(config: str, override: bool | None) -> bool:
    if override is not None:
        return override
    with Path(config).open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        return False
    source = str(data.get("data_sources", {}).get("compound_structure", "")).strip().lower()
    return source == "pubchem"


def run_step(name: str, script: str, config: str, *, pubchem: bool = False) -> None:
    if name == "validate_input":
        cmd = [sys.executable, script, "--config", config]
    else:
        cmd = [
            sys.executable,
            script,
            "--config",
            config,
        ]
        if name == "fetch_herb_compounds" and pubchem:
            cmd.append("--pubchem")
    print(f"\n==> {name}")
    result = subprocess.run(cmd, cwd=Path.cwd(), text=True, timeout=remaining_timeout(1800))
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def make_runtime_config(
    config: str,
    herb_name: str | None,
    compound_name: str | None,
    disease_name: str | None,
    project_name: str | None,
    input_mode: str | None,
) -> str:
    if not herb_name and not compound_name and not disease_name and not project_name and not input_mode:
        return config

    with Path(config).open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise SystemExit(f"Config must be a YAML object: {config}")

    data.setdefault("project", {})
    data.setdefault("input", {})
    if input_mode:
        data["input"]["mode"] = input_mode
    if herb_name:
        data["input"]["herb_name"] = herb_name
        data["input"]["mode"] = data["input"].get("mode", "herb")
    if compound_name:
        data["input"]["compound_name"] = compound_name
        data["input"]["mode"] = data["input"].get("mode", "compound")
    if disease_name:
        data["input"]["disease_name"] = disease_name
    if project_name:
        data["project"]["name"] = project_name
    elif herb_name or compound_name or disease_name:
        entity = data["input"].get("compound_name") or data["input"].get("herb_name", "entity")
        disease = data["input"].get("disease_name", "disease")
        safe = "".join(ch if ch.isalnum() else "_" for ch in f"{entity}_{disease}")
        data["project"]["name"] = f"npflow_{safe}"[:80]

    runtime_config = Path("results/network_pharmacology/00_input/agent.params.yaml")
    runtime_config.parent.mkdir(parents=True, exist_ok=True)
    with runtime_config.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
    return str(runtime_config)


def run_workflow(
    config: str,
    herb_name: str | None = None,
    compound_name: str | None = None,
    disease_name: str | None = None,
    project_name: str | None = None,
    input_mode: str | None = None,
    pubchem_override: bool | None = None,
) -> None:
    config = make_runtime_config(config, herb_name, compound_name, disease_name, project_name, input_mode)
    out_dir = config_output_dir(config)
    resolved_config = out_dir / "00_input" / "params.resolved.yaml"
    pubchem = should_use_pubchem(config, pubchem_override)
    for name, script in STEPS:
        step_config = config if name == "validate_input" else str(resolved_config)
        run_step(name, script, step_config, pubchem=pubchem)
    print("\nNetwork pharmacology workflow completed.")
    summarize(str(out_dir))


def summarize(results: str) -> None:
    qc_path = Path(results) / "08_report" / "qc_report.json"
    if not qc_path.exists():
        print(f"QC report not found: {qc_path}")
        return
    qc = read_json(qc_path)
    counts = qc.get("counts", {})
    print("\nSummary")
    print(f"- Project: {qc.get('project_name', '')}")
    print(f"- Input mode: {qc.get('input_mode', 'herb')}")
    print(f"- Herb/formula: {qc.get('herb_name', '')}")
    print(f"- Compound: {qc.get('compound_name', '')}")
    print(f"- Disease: {qc.get('disease_name', '')}")
    print(f"- Status: {qc.get('status', '')}")
    print(f"- Active ingredients: {counts.get('active_ingredients', 0)}")
    print(f"- Intersection targets: {counts.get('intersection_targets', 0)}")
    print(f"- Docking targets: {counts.get('docking_targets', 0)}")
    print(f"- Docking ligands: {counts.get('docking_ligands', counts.get('ligand_ready_ingredients', 0))}")
    print(f"- Can enter docking: {qc.get('can_enter_docking', False)}")
    print(f"- Report: {Path(results) / '08_report' / 'network_pharmacology_report.html'}")
    print(f"- Docking targets: {Path(results) / '07_core_targets' / 'docking_targets.csv'}")


def qc(results: str) -> None:
    qc_path = Path(results) / "08_report" / "qc_report.json"
    if not qc_path.exists():
        raise SystemExit(f"QC report not found: {qc_path}")
    data = read_json(qc_path)
    print(f"Status: {data.get('status')}")
    for step, status in data.get("step_statuses", {}).items():
        print(f"- {step}: {status}")
    warnings = data.get("warnings") or []
    if warnings:
        print("\nWarnings")
        for warning in warnings[:50]:
            print(f"- {warning}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Network Pharmacology Agent for NPFlow-Agent.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run the full network pharmacology workflow.")
    run_parser.add_argument("--config", default="config/params.yaml")
    run_parser.add_argument("--herb-name", help="Override input.herb_name from the config.")
    run_parser.add_argument("--compound-name", help="Override input.compound_name from the config.")
    run_parser.add_argument("--mode", choices=["herb", "compound"], help="Override input.mode from the config.")
    run_parser.add_argument("--disease-name", help="Override input.disease_name from the config.")
    run_parser.add_argument("--project-name", help="Override project.name from the config.")
    pubchem_group = run_parser.add_mutually_exclusive_group()
    pubchem_group.add_argument("--pubchem", dest="pubchem", action="store_true", help="Force PubChem structure enrichment.")
    pubchem_group.add_argument("--no-pubchem", dest="pubchem", action="store_false", help="Disable PubChem structure enrichment.")
    run_parser.set_defaults(pubchem=None)

    summary_parser = subparsers.add_parser("summarize", help="Summarize a completed workflow.")
    summary_parser.add_argument("--results", default="results/network_pharmacology")

    qc_parser = subparsers.add_parser("qc", help="Print QC status and warnings.")
    qc_parser.add_argument("--results", default="results/network_pharmacology")

    args = parser.parse_args()
    if args.command == "run":
        run_workflow(
            args.config,
            args.herb_name,
            args.compound_name,
            args.disease_name,
            args.project_name,
            args.mode,
            args.pubchem,
        )
    elif args.command == "summarize":
        summarize(args.results)
    elif args.command == "qc":
        qc(args.results)


if __name__ == "__main__":
    main()
