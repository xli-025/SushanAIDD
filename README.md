# SushanAIDD

SushanAIDD is an agent-automated workflow for traditional Chinese medicine drug discovery. It connects network pharmacology, molecular docking, and post-docking molecular optimization in a traceable pipeline.

This repository accompanies the manuscript **SushanAIDD: An Agent-Automated Drug Discovery Workflow for Traditional Chinese Medicine**. It contains the workflow source code, automated tests, docking fixtures, dependency specifications, model checksums, and documentation required to inspect and reproduce the computational workflow.

## Workflow

1. **Network pharmacology** collects and reconciles herb-compound-target and disease-target evidence, performs enrichment and PPI analysis, and ranks ligands and targets.
2. **Molecular docking** prepares ligands and receptors, defines binding sites, runs AutoDock Vina, records quality-control evidence, and separates raw scores from scientifically usable rankings.
3. **Reverse design** uses DeepFrag2 to suggest fragment additions, filters and assembles candidates, and re-docks candidates under a matched protocol.
4. **Web application** provides authenticated task submission, progress monitoring, result packaging, and retention controls. Deployment credentials and runtime databases are intentionally excluded.

## Repository contents

- `Network Pharmacology/`: network pharmacology implementation, tests, configuration template, and source manifests.
- `Molecular Docking/`: receptor and ligand preparation, Vina execution, validation, and fixtures.
- `Reverse Design/`: DeepFrag2 integration, candidate assembly, re-docking, and model files.
- `webapp/`: optional local Web interface without deployment credentials, databases, logs, certificates, or personal profiles.
- `npflow_agent.py`: command-line orchestration entry point.
- `REPRODUCIBILITY.md`: installation, tests, and staged reproduction instructions.
- `DATA_AVAILABILITY.md`: data provenance and redistribution boundaries.
- `MODEL_MANIFEST.md`: bundled model checksums and provenance.
- `SECURITY.md`: responsible handling of credentials and deployment configuration.

## Quick verification

Create the core environment described in `environment.yml`, activate it, and run:

```bash
python -m pytest test_npflow_agent_contracts.py test_practical_runtime.py -q
python -m pytest "Network Pharmacology/modules/network_pharmacology/tests" -q
python -m pytest "Molecular Docking/modules/docking/tests" -q
python -m pytest "Reverse Design/modules/reverse_design_after_docking/tests" -q
```

For a docking-engine smoke test:

```bash
cd "Molecular Docking"
python modules/docking/agent.py check-deps
python modules/docking/agent.py validate-engine \
  --fixture modules/docking/tests/fixtures/smoke_vina
```

See `REPRODUCIBILITY.md` before attempting the full workflow. Some live network-pharmacology sources are third-party services and may be unavailable or impose their own access terms.

## Scientific interpretation

SushanAIDD is a computational prioritization workflow. Network associations, enrichment results, docking scores, and generated candidates are hypotheses for follow-up; they are not evidence of clinical efficacy, safety, or experimentally measured binding affinity.

## License status

The project authors must select and add a license for the original SushanAIDD source code before public release. DeepFrag2 is distributed under the MIT License; see `THIRD_PARTY_NOTICES.md`. Absence of a project license means no permission is granted to reuse the original SushanAIDD code beyond applicable law.

## Citation

Please cite the accompanying manuscript. Final journal citation and DOI should be added here when available.
