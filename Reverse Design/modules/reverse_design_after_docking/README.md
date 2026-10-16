# Reverse Design After Docking

This module runs after an existing Molecular Docking run. It does not modify the Network Pharmacology or Docking core modules.

Primary command:

```bash
python modules/reverse_design_after_docking/agent.py run \
  --dock-dir "<network_run_dir>/09_docking"
```

Default behavior:

- select up to 3 seed protein-ligand pairs with Vina score <= -6.0;
- use `dry_run` fragment generation unless `--backend deepfrag2` is requested and the environment is eligible; `--skip-deepfrag2-test` records the test as skipped and trusts executable presence, it does not claim that the test passed;
- automatically select ligand attachment points from the parent ligand pose/fallback SDF;
- generate fragment-extended candidates, RDKit-filter them, prepare PDBQT, and redock with the original receptor, box, and Vina parameters;
- accept candidates only when `new_score <= old_score - 0.8` by default and the candidate has real `generation_backend=deepfrag2`, `scoring_engine=vina`, and `execution_mode=production` provenance.

Each round is another candidate-generation attempt around the original docking seed. The current loop does not replace the parent with an accepted candidate, so it is not a stepwise parent-evolution loop.

Main outputs:

- `selected_seed_pairs.csv`
- `reverse_design_all_candidates.csv`
- `reverse_design_accepted_candidates.csv`
- `reverse_design_simulation_candidates.csv`
- `natural_vs_optimized_comparison.csv`
- `reverse_design_qc.json`
- `pair_*/vina_job_manifest.json` (job identity includes receptor, box, candidate PDBQT, and effective Vina-parameter fingerprint; changed inputs are never resumed as the old score)
- `reverse_design_report.html`

DeepFrag2 is not trained by this module. If DeepFrag2 is unavailable, the module records the environment status and may use the deterministic dry-run backend so the automation loop remains testable. Such rows retain per-candidate `generation_backend`, `scoring_engine`, `execution_mode`, and `fallback_reason`; they are isolated from accepted output and QC reports `simulation_only` with `valid_result=false` when no formal result exists. `auto` and `vina` redocking fail closed when Vina is unavailable; only explicit `--redock-mode dry_run` produces simulated scores.
