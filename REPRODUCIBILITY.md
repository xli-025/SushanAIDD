# Reproducibility guide

## Scope

The repository supports three levels of verification:

1. Contract and unit tests that do not require live third-party services.
2. Docking and reverse-design smoke tests using bundled fixtures and models.
3. A full network-pharmacology, docking, and reverse-design run using live or locally prepared third-party data sources.

The recorded reference deployment used Ubuntu 20.04, Python 3.11.7 for the core workflow, AutoDock Vina 1.2.5, and DeepFrag2 2.0.0 in a separate Python 3.9 environment.

## Core environment

Conda or Mamba is recommended:

```bash
conda env create -f environment.yml
conda activate sushanaidd
```

The workflow also expects AutoDock Vina and Open Babel to be discoverable on `PATH`. Verify the environment from the docking directory:

```bash
cd "Molecular Docking"
python modules/docking/agent.py check-deps
```

## DeepFrag2 environment

DeepFrag2 is isolated because its dependency stack differs from the core workflow:

```bash
conda env create -f environment-deepfrag2.yml
conda activate sushanaidd-deepfrag2
deepfrag2_test
```

The reverse-design adapter searches for a `deepfrag2` executable and the model assets documented in `MODEL_MANIFEST.md`. If DeepFrag2 is unavailable, deterministic dry-run mode can test orchestration but cannot produce scientifically accepted optimized candidates.

## Automated tests

From the repository root:

```bash
python -m pytest test_npflow_agent_contracts.py test_practical_runtime.py -q
python -m pytest "Network Pharmacology/modules/network_pharmacology/tests" -q
python -m pytest "Molecular Docking/modules/docking/tests" -q
python -m pytest "Reverse Design/modules/reverse_design_after_docking/tests" -q
python -m pytest webapp/tests -q
```

The server-side validation record supplied with this release reported 239 passing tests: 98 root tests, 54 network-pharmacology tests, 61 docking tests, and 26 reverse-design tests. Reviewers should run the commands above in their own environment; the recorded result is provenance, not a substitute for independent execution.

## Docking fixture

The fixture verifies executable discovery, input parsing, Vina invocation, output parsing, and validation without relying on a live external database:

```bash
cd "Molecular Docking"
python modules/docking/agent.py validate-engine \
  --fixture modules/docking/tests/fixtures/smoke_vina
```

## Full workflow

From the repository root, start the interactive orchestrator:

```bash
python npflow_agent.py
```

or supply a natural-language task:

```bash
python npflow_agent.py "Run the full workflow for Astragalus membranaceus and diabetic kidney disease"
```

The network stage writes ranked ligand and target tables. The docking stage consumes those tables and writes ranked scores plus quality-control evidence. The reverse-design stage consumes scientifically usable docking pairs, proposes fragment additions, and re-docks candidates with matched settings.

## Expected outputs

- Network pharmacology: compound-target matrices, ranked docking inputs, enrichment tables, PPI files, and QC report.
- Molecular docking: ligand and receptor preparation records, pair-level QC, ranked docking scores, and validation summary.
- Reverse design: candidate table, accepted structures, comparison table, provenance records, and HTML report.

## External-service variability

Results that depend on live TCMSP, SymMap, PubChem, RCSB PDB, STRING, g:Profiler, or other external services may change as those services update. The workflow records source decisions, identifiers, timestamps, fallbacks, and QC evidence. A source outage must be reported as a warning or failure; it must not be silently converted into successful evidence.

## Reference acceptance run

An internal acceptance run used Astragalus membranaceus and diabetic kidney disease. All three stages completed. The run selected three targets and five ligands, prepared one scientifically usable receptor, scored five docking pairs, performed 30 DeepFrag2/Vina candidate evaluations, and accepted five candidates using the configured improvement threshold. These values are provided as a reference signature, not a guaranteed result for future live-source runs.
