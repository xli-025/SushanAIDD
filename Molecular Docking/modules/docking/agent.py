#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from modules.docking.scripts.check_dependencies import diagnose
from modules.docking.scripts.common import DEFAULT_OUTPUT_DIR, DockingError
from modules.docking.scripts.build_fixture_docking_manifest import build_fixture_docking_manifest as build_fixture_manifest_file
from modules.docking.scripts.define_binding_sites import define_binding_sites as define_binding_sites_batch
from modules.docking.scripts.generate_docking_report import generate_report
from modules.docking.scripts.ligand_state_contract import validate_ligand_ph
from modules.docking.scripts.prepare_ligands import prepare_ligands as prepare_ligands_batch
from modules.docking.scripts.prepare_receptors import prepare_receptors as prepare_receptors_batch
from modules.docking.scripts.run_vina_batch import load_fixture_manifest, load_input_manifest, run_pairs
from modules.docking.scripts.run_from_network import run_from_network as run_from_network_batch
from modules.docking.scripts.select_receptor_structures import select_receptor_structures as select_receptor_structures_batch
from modules.docking.scripts.validate_network_docking_run import validate_network_docking_run as validate_network_docking_run_batch


def check_deps(args: argparse.Namespace) -> None:
    diagnostic = diagnose(output_dir=Path(args.output_dir), write=True)
    print(f"Dependency diagnostic: {Path(args.output_dir) / 'dependency_diagnostic.json'}")
    if diagnostic["blocking_missing_dependencies"]:
        print("Blocking missing dependencies:")
        for item in diagnostic["blocking_missing_dependencies"]:
            print(f"- {item}")
    else:
        print("All blocking dependencies are available.")


def validate_engine(args: argparse.Namespace) -> None:
    output_dir = Path(args.output_dir)
    diagnostic = diagnose(output_dir=output_dir, write=True)
    manifest = load_fixture_manifest(Path(args.fixture))
    pair_rows, _ = run_pairs(
        manifest,
        output_dir=output_dir,
        manifest_base=Path(args.fixture),
        exhaustiveness=args.exhaustiveness,
        num_modes=args.num_modes,
        energy_range=args.energy_range,
        timeout_seconds=args.timeout_per_pair,
        num_replicates=1,
    )
    report = generate_report(
        pair_rows,
        dependency_diagnostic=diagnostic,
        output_dir=output_dir,
        project_name="smoke_vina",
    )
    print(f"Smoke status: {report['status']}")
    print(f"Report: {output_dir / '08_docking_report' / 'docking_report.html'}")
    if not diagnostic["can_run_minimal_vina"]:
        raise SystemExit(2)
    if not report["summary"]["successful_pairs"]:
        raise SystemExit(1)


def run(args: argparse.Namespace) -> None:
    output_dir = Path(args.output_dir)
    diagnostic = diagnose(output_dir=output_dir, write=True)
    if args.pocket_mode not in {"fixture", "cocrystal"}:
        raise DockingError("This MVP only supports --pocket-mode fixture or cocrystal.")
    manifest_path = Path(args.input_manifest)
    manifest = load_input_manifest(manifest_path)
    pair_rows, _ = run_pairs(
        manifest,
        output_dir=output_dir,
        manifest_base=manifest_path.parent,
        exhaustiveness=args.exhaustiveness,
        num_modes=args.num_modes,
        energy_range=args.energy_range,
        timeout_seconds=args.timeout_per_pair,
        num_replicates=args.num_replicates,
    )
    report = generate_report(
        pair_rows,
        dependency_diagnostic=diagnostic,
        output_dir=output_dir,
        project_name=str(manifest.get("project_name") or "manifest_docking"),
    )
    print(f"Run status: {report['status']}")
    print(f"Report: {output_dir / '08_docking_report' / 'docking_report.html'}")
    if not diagnostic["can_run_minimal_vina"]:
        raise SystemExit(2)
    if not report["summary"]["successful_pairs"]:
        raise SystemExit(1)


def prepare_ligands(args: argparse.Namespace) -> None:
    diagnostic = diagnose(output_dir=Path(args.out_dir).parent, write=True)
    if not diagnostic["rdkit_import_ok"] or not diagnostic["meeko_import_ok"]:
        raise DockingError("RDKit and Meeko are required for ligand preparation; see dependency_diagnostic.json.")
    try:
        ph = validate_ligand_ph(args.ph)
    except ValueError as exc:
        raise DockingError(str(exc)) from exc
    qc_rows = prepare_ligands_batch(
        Path(args.input_csv),
        out_dir=Path(args.out_dir),
        fetch_pubchem=not args.no_pubchem,
        random_seed=args.random_seed,
        ph=ph,
    )
    success_count = sum(1 for row in qc_rows if row.get("pdbqt_status") == "success")
    print(f"Ligand preparation completed. Success: {success_count}/{len(qc_rows)}")
    print(f"QC: {Path(args.out_dir) / 'ligand_preparation_qc.csv'}")
    print(f"PDBQT dir: {Path(args.out_dir) / 'pdbqt'}")
    if success_count == 0:
        print("No ligands produced PDBQT; inspect ligand_preparation_qc.csv for failure_reason values.")


def prepare_receptors(args: argparse.Namespace) -> None:
    qc_rows = prepare_receptors_batch(
        Path(args.input_csv),
        out_dir=Path(args.out_dir),
        min_protein_atoms=args.min_protein_atoms,
        receptor_prep_backend=args.receptor_prep_backend,
    )
    success_count = sum(1 for row in qc_rows if row.get("preparation_status") == "success")
    print(f"Receptor preparation completed. Success: {success_count}/{len(qc_rows)}")
    print(f"QC: {Path(args.out_dir) / 'receptor_preparation_qc.csv'}")
    print(f"PDBQT dir: {Path(args.out_dir) / 'pdbqt'}")
    if success_count == 0:
        print("No receptors produced PDBQT; inspect receptor_preparation_qc.csv for failure_reason values.")


def define_binding_sites(args: argparse.Namespace) -> None:
    rows = define_binding_sites_batch(
        Path(args.receptor_qc),
        out_dir=Path(args.out_dir),
        padding=args.padding,
        min_size=args.min_size,
    )
    success_count = sum(1 for row in rows if row.get("binding_site_status") == "defined")
    print(f"Binding site definition completed. Defined: {success_count}/{len(rows)}")
    print(f"Boxes: {Path(args.out_dir) / 'binding_site_boxes.csv'}")


def build_fixture_manifest(args: argparse.Namespace) -> None:
    manifest = build_fixture_manifest_file(
        receptor_qc=Path(args.receptor_qc),
        ligand_qc=Path(args.ligand_qc),
        box_csv=Path(args.box_csv),
        out_manifest=Path(args.out_manifest),
    )
    print(f"Fixture docking manifest written: {args.out_manifest}")
    print(f"Pairs: {len(manifest.get('pairs', []))}")


def select_receptor_structures(args: argparse.Namespace) -> None:
    receptor_rows, qc_rows = select_receptor_structures_batch(
        Path(args.docking_targets_csv),
        out_dir=Path(args.out_dir),
        max_targets=args.max_targets,
        max_candidates_per_target=args.max_candidates_per_target,
    )
    print(f"Target structure selection completed. Selected: {len(receptor_rows)}/{len(qc_rows)}")
    print(f"Receptors: {Path(args.out_dir) / 'selected_receptors.csv'}")
    print(f"QC: {Path(args.out_dir) / 'target_structure_selection_qc.csv'}")


def run_from_network(args: argparse.Namespace) -> None:
    try:
        ph = validate_ligand_ph(args.ph)
    except ValueError as exc:
        raise DockingError(str(exc)) from exc
    summary = run_from_network_batch(
        network_run_dir=Path(args.network_run_dir) if args.network_run_dir else None,
        out_dir=Path(args.out_dir) if args.out_dir else None,
        max_targets=args.max_targets,
        max_ligands=args.max_ligands,
        max_candidates_per_target=args.max_candidates_per_target,
        ph=ph,
    )
    print("Network-to-docking completed.")
    for key, value in summary.items():
        print(f"{key}: {value}")


def validate_network_run(args: argparse.Namespace) -> None:
    validation = validate_network_docking_run_batch(Path(args.dock_dir))
    print(f"Validation status: {validation['status']}")
    print(f"Summary: {Path(args.dock_dir) / 'validation' / 'validation_summary.txt'}")
    if validation["status"] != "PASS":
        raise SystemExit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="NPFlow molecular docking MVP.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    deps = subparsers.add_parser("check-deps", help="Check docking dependencies.")
    deps.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    deps.set_defaults(func=check_deps)

    validate = subparsers.add_parser("validate-engine", help="Run the fixture Vina smoke test.")
    validate.add_argument("--fixture", default="modules/docking/tests/fixtures/smoke_vina")
    validate.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    validate.add_argument("--exhaustiveness", type=int, default=16)
    validate.add_argument("--num-modes", type=int, default=9)
    validate.add_argument("--energy-range", type=int, default=3)
    validate.add_argument("--timeout-per-pair", type=int, default=1800)
    validate.set_defaults(func=validate_engine)

    run_parser = subparsers.add_parser("run", help="Run docking from a fixture manifest.")
    run_parser.add_argument("--input-manifest", required=True)
    run_parser.add_argument("--pocket-mode", default="fixture")
    run_parser.add_argument("--engine", default="vina", choices=["vina"])
    run_parser.add_argument("--num-replicates", type=int, default=1)
    run_parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    run_parser.add_argument("--exhaustiveness", type=int, default=16)
    run_parser.add_argument("--num-modes", type=int, default=9)
    run_parser.add_argument("--energy-range", type=int, default=3)
    run_parser.add_argument("--timeout-per-pair", type=int, default=1800)
    run_parser.set_defaults(func=run)

    ligand_parser = subparsers.add_parser("prepare-ligands", help="Prepare real ligands as SDF and PDBQT.")
    ligand_parser.add_argument("--input-csv", required=True)
    ligand_parser.add_argument("--out-dir", default=str(DEFAULT_OUTPUT_DIR / "03_ligand_prepared"))
    ligand_parser.add_argument("--no-pubchem", action="store_true", help="Skip PubChem identity and 3D SDF lookup.")
    ligand_parser.add_argument("--random-seed", type=int, default=61453)
    ligand_parser.add_argument("--ph", type=float, default=7.4)
    ligand_parser.set_defaults(func=prepare_ligands)

    receptor_parser = subparsers.add_parser("prepare-receptors", help="Prepare receptors as cleaned PDB and PDBQT.")
    receptor_parser.add_argument("--input-csv", required=True)
    receptor_parser.add_argument("--out-dir", default=str(DEFAULT_OUTPUT_DIR / "02_receptor_prepared"))
    receptor_parser.add_argument("--min-protein-atoms", type=int, default=20)
    receptor_parser.add_argument(
        "--receptor-prep-backend",
        default="auto",
        choices=["auto", "meeko", "autodocktools", "openbabel"],
        help="Receptor PDBQT preparation backend. auto tries Meeko, optional AutoDockTools, then OpenBabel fallback.",
    )
    receptor_parser.set_defaults(func=prepare_receptors)

    box_parser = subparsers.add_parser("define-binding-sites", help="Define binding boxes from cocrystal ligands.")
    box_parser.add_argument("--receptor-qc", required=True)
    box_parser.add_argument("--out-dir", default=str(DEFAULT_OUTPUT_DIR / "04_binding_sites"))
    box_parser.add_argument("--padding", type=float, default=8.0)
    box_parser.add_argument("--min-size", type=float, default=18.0)
    box_parser.set_defaults(func=define_binding_sites)

    manifest_parser = subparsers.add_parser("build-fixture-manifest", help="Build a real fixture docking manifest.")
    manifest_parser.add_argument("--receptor-qc", required=True)
    manifest_parser.add_argument("--ligand-qc", required=True)
    manifest_parser.add_argument("--box-csv", required=True)
    manifest_parser.add_argument("--out-manifest", required=True)
    manifest_parser.set_defaults(func=build_fixture_manifest)

    select_parser = subparsers.add_parser("select-receptor-structures", help="Select local PDB receptors from network docking targets.")
    select_parser.add_argument("--docking-targets-csv", required=True)
    select_parser.add_argument("--out-dir", default=str(DEFAULT_OUTPUT_DIR / "01_target_structures"))
    select_parser.add_argument("--max-targets", type=int, default=3)
    select_parser.add_argument("--max-candidates-per-target", type=int, default=15)
    select_parser.set_defaults(func=select_receptor_structures)

    network_parser = subparsers.add_parser("run-from-network", help="Run docking from a network pharmacology run directory.")
    network_parser.add_argument("--network-run-dir", help="Network pharmacology run directory. Defaults to latest_run.json.")
    network_parser.add_argument("--out-dir", help="Output directory. Defaults to <network_run_dir>/09_docking when --network-run-dir is provided.")
    network_parser.add_argument("--max-targets", type=int, default=3)
    network_parser.add_argument("--max-ligands", type=int, default=5)
    network_parser.add_argument("--max-candidates-per-target", type=int, default=15)
    network_parser.add_argument("--ph", type=float, default=7.4)
    network_parser.set_defaults(func=run_from_network)

    validate_network_parser = subparsers.add_parser("validate-network-run", help="Validate a completed network-to-docking run output directory.")
    validate_network_parser.add_argument("--dock-dir", required=True)
    validate_network_parser.set_defaults(func=validate_network_run)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except DockingError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
