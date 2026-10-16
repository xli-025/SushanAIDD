# NPFlow-Agent 当前验证记录

记录日期：2026-05-28
项目根目录：`${REPO_ROOT}`

## 1. 代码级验证

工作目录：`${REPO_ROOT}/Network Pharmacology`

语法检查：

```text
rank_core_targets.py: OK
herb_source_integrator.py: OK
fetch_herb_compounds.py: OK
generate_report.py: OK
common.py: OK
```

## 2. 网药 v2 打分系统 + 三模块全链路验证

### 金银花 + influenza（全链路）

Run 目录：

```text
${REPO_ROOT}/Network Pharmacology/results/network_pharmacology_runs/金银花_influenza_20260528_193534
```

### Network Pharmacology

```text
workflow_status: passed
active_ingredients: 71
intersection_targets: 444
docking_targets: 16（按 target_score 排序，PTGS2=0.740 排第一）
docking_ligands: 25（按 ligand_score 排序，p-Cresol=0.794 排第一）
data_sources: TCMSP + SymMap2 + HerbiV
```

### Molecular Docking

```text
Validation status: PASS
checks_total: 24
checks_passed: 24
checks_failed: 0
ligands_prepared: 5
receptors_prepared: 2
pairs: 20
successful_pairs: 20
```

### Reverse Design

```text
Status: completed
n_raw_candidates: 40
n_filtered_candidates: 12
n_vina_jobs: 12
n_accepted: 4
best_improvement: +1.987 kcal/mol（Baicalein → 甲基苯基衍生物，target=PTGS2）
```

## 3. 可交接结论

当前项目可交接给下一个 agent 继续开发。

可信基线：

```text
Network Pharmacology v2: passed（多因子加权打分 + HerbiV 数据源）
Molecular Docking: passed
Reverse Design (DeepFrag2): passed（4/40 accepted）
Network-to-Docking bridge: passed
Docking-to-ReverseDesign bridge: passed
```
