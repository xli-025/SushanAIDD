# NPFlow-Agent 工作流逻辑与运行手册

项目根目录：`${REPO_ROOT}`  
共用 Python：`python`

## 1. 总体运行逻辑

当前系统分为三个阶段：

```text
Stage A: Network Pharmacology
用户输入中药/化合物 + 疾病
-> 三数据源（TCMSP + SymMap2 + HerbiV）合并
-> 多因子加权打分（ligand_score + target_score）
-> 输出 docking_targets.csv 和 docking_ligands.csv

Stage B: Molecular Docking
读取 network run_dir
-> 配体准备
-> 靶点结构选择
-> 受体准备
-> 共晶配体定盒
-> Vina 对接
-> ranking/report/validation

Stage C: Reverse Design (DeepFrag2)
读取 dock_dir
-> 选择 seed pairs
-> 识别 attachment points
-> DeepFrag2 AI 生成候选片段
-> 组装 + 过滤 + Vina 重对接
-> 接受改善达标的候选分子
```

两个模块目前不是一个单一入口命令，而是通过文件契约连接：

```text
Network Pharmacology/results/network_pharmacology_runs/<run>/07_core_targets/docking_targets.csv
Network Pharmacology/results/network_pharmacology_runs/<run>/07_core_targets/docking_ligands.csv
```

Docking 通过 `run-from-network --network-run-dir <run_dir>` 读取这两个文件。

## 2. Network Pharmacology 运行逻辑

工作目录：

```bash
cd "${REPO_ROOT}/Network Pharmacology"
```

主入口：

```text
modules/manager_agent/qwen_npflow_agent.py
modules/network_pharmacology/agent.py
```

推荐用户入口是 Manager：

```bash
printf '中药=丹参，疾病=coronary artery disease\nexit\n' | \
  python modules/manager_agent/qwen_npflow_agent.py
```

Manager 的逻辑：

1. 解析用户输入。
2. 判断输入模式：
   - `herb`
   - `compound`
3. 生成配置文件：
   - `config/generated/<project_name>.yaml`
4. 调用：
   - `modules/network_pharmacology/agent.py run --config <generated_config>`
5. 写入 latest run：
   - `results/network_pharmacology_runs/latest_run.json`

Network Pharmacology agent 的步骤：

```text
validate_input
fetch_herb_compounds（TCMSP + SymMap2 + HerbiV 三来源合并）
fetch_disease_targets
intersect_targets
build_ppi
run_enrichment
rank_core_targets（多因子加权打分：ligand_score + target_score）
generate_report
```

### 数据来源与置信度

| 来源 | ingredient_confidence | relation_confidence | 说明 |
|------|----------------------|---------------------|------|
| TCMSP | 1.00 | 0.90 | OB/DL 过滤活性成分 |
| HerbiV | 0.75–0.85 | 0.65 | 本地 CSV 导入，combined_score 驱动 |
| SymMap2 | 0.65–0.75 | 0.60 | 官方下载 + API 查询 |
| 多来源重叠 | +0.05/源 | +0.10/源 | 交叉验证奖励，上限 1.00 |

核心输出：

```text
<network_run_dir>/07_core_targets/docking_targets.csv
<network_run_dir>/07_core_targets/docking_ligands.csv
<network_run_dir>/08_report/qc_report.json
<network_run_dir>/08_report/network_pharmacology_report.md
<network_run_dir>/08_report/network_pharmacology_report.html
```

### Network Pharmacology 打分公式

**ligand_score**（用于 docking_ligands.csv 排序）：

```text
ligand_score = 0.30 × ingredient_confidence
             + 0.25 × structure_quality_score
             + 0.20 × target_link_score
             + 0.15 × disease_relevance_score
             + 0.10 × multi_source_support_score
```

**target_score**（用于 docking_targets.csv 排序）：

```text
target_score = 0.25 × source_confidence
             + 0.25 × disease_association_norm
             + 0.20 × ppi_topology_score
             + 0.20 × compound_coverage_score
             + 0.10 × multi_source_support_score
```

阈值：`min_ligand_score=0.55`、`min_target_score=0.55`（可在 `config/params.yaml` 调整）。

## 3. Molecular Docking 运行逻辑

工作目录：

```bash
cd "${REPO_ROOT}/Molecular Docking"
```

主入口：

```text
modules/docking/agent.py
```

从 network run 进入 docking：

```bash
python modules/docking/agent.py run-from-network \
  --network-run-dir "<network_run_dir>" \
  --max-targets 3 \
  --max-ligands 5 \
  --max-candidates-per-target 3
```

如果不传 `--out-dir`，默认输出到：

```text
<network_run_dir>/09_docking
```

Docking bridge 的内部逻辑：

1. 读取 `docking_ligands.csv`。
2. 生成 `00_input_manifest/network_ligands_input.csv`。
3. 记录 `00_input_manifest/selected_ligands.csv`。
4. 运行 ligand preparation：
   - PubChem 3D SDF 优先。
   - RDKit ETKDG fallback。
   - MMFF/UFF 最小化。
   - Meeko 转 PDBQT。
5. 读取 `docking_targets.csv`。
6. 对每个 target 搜索最多 3 个 PDB candidate。
7. 对 candidate 检查是否有 reference pocket ligand。
8. 对 candidate 执行 receptor preparation 和 binding box definition。
9. 选择第一个 scientific usable receptor；否则记录 fallback 或跳过原因。
10. 生成 full docking manifest。
11. 运行 AutoDock Vina。
12. 生成 raw score、pair QC、ranked score。
13. 生成 HTML/JSON report。
14. 可运行 `validate-network-run` 做契约验收。

## 4. Docking 默认科学策略

### Ligand

输入字段来自 network：

```text
ligand_id
compound_name / source_ingredient_names
pubchem_cid
canonical_smiles
isomeric_smiles
inchikey
```

默认流程：

```text
PubChem 3D SDF if CID available
-> RDKit fallback if PubChem unavailable
-> AddHs
-> ETKDGv3
-> MMFF94/MMFF94s
-> UFF fallback
-> SDF
-> Meeko PDBQT
```

当前不默认做 tautomer/protomer enumeration。

### Target and receptor

默认流程：

```text
docking_targets.csv
-> UniProt ID
-> RCSB PDB candidate search
-> max_candidates_per_target=3
-> inspect reference pocket ligand
-> local PDB
-> select chain
-> save reference ligand
-> remove waters / salts / buffers / selected ligand
-> retain important metals/cofactors when classified
-> PDB2PQR/PROPKA pH 7.4
-> Meeko receptor PDBQT
-> PDBQT charge/atom sanity
```

关键规则：

- 没有 reference pocket ligand：不 blind docking，写 QC 原因。
- Meeko 是首选 backend。
- AutoDockTools/ADFR 是可选 fallback。
- OpenBabel fallback 只能作为 workflow validation。
- zero-charge receptor 不进入 scientific ranking。

### Binding box

默认流程：

```text
saved reference pocket ligand coordinates
-> heavy atom centroid
-> bounding box
-> padding 8 A
-> min axis size 18 A
-> max boxes per receptor 3
```

### Vina

默认 profile：

```text
profile: npflow_docking_default_v1
engine: vina
exhaustiveness: 8
num_modes: 9
energy_range: 3
cpu: 0
num_replicates: 1
seed: deterministic_pair_seed
```

Vina 输出按 target/ligand/box/replicate 分目录，避免多 box 覆盖。

## 5. Ranking 与报告逻辑

输出文件：

```text
06_scores/docking_scores_raw.csv
06_scores/docking_pair_qc.csv
06_scores/docking_scores_ranked.csv
08_docking_report/docking_qc_report.json
08_docking_report/docking_report.html
```

`docking_scores_ranked.csv` 必须包含：

```text
raw_vina_rank
scientific_usable_rank
confidence_adjusted_rank
ranking_bucket
```

排名解释：

- `raw_vina_rank`：所有 Vina success pair 按 best score 排序。
- `scientific_usable_rank`：只对 `usable_for_scientific_ranking=True` 的 pair 排序。
- `confidence_adjusted_rank`：scientific usable pair 优先，low-confidence pair 放后。
- `workflow_validation_low_confidence`：流程可跑，但不应作为科学排序结论。

报告必须区分：

- best raw pair
- best scientific usable pair
- low-confidence/workflow-validation pair count

## 6. 验收器

命令：

```bash
cd "${REPO_ROOT}/Molecular Docking"

python modules/docking/agent.py validate-network-run \
  --dock-dir "<dock_dir>"
```

验收器输出：

```text
<dock_dir>/validation/validation_status.json
<dock_dir>/validation/validation_summary.txt
```

验收器检查：

- 必需文件存在。
- manifest job 数 = `docking_pair_qc.csv` 行数。
- successful jobs 数 = pose/log/config 数。
- ranking 字段完整。
- 至少 1 个 scientific usable pair。
- scientific best 只能来自 usable pair。
- receptor QC 字段完整。
- rejected/skipped candidate 必须有原因。

## 7. 常用命令

### Docking 依赖检查

```bash
cd "${REPO_ROOT}/Molecular Docking"
python modules/docking/agent.py check-deps
```

### Docking smoke test

```bash
cd "${REPO_ROOT}/Molecular Docking"
python modules/docking/agent.py validate-engine \
  --fixture modules/docking/tests/fixtures/smoke_vina
```

### Docking unit tests

```bash
cd "${REPO_ROOT}/Molecular Docking"
python -m pytest modules/docking/tests -q
```

### 从 latest network run 进入 docking

```bash
NEW_NP_RUN_DIR=$(python - <<'PY'
import json
from pathlib import Path
p = Path('${REPO_ROOT}/Network Pharmacology/results/network_pharmacology_runs/latest_run.json')
print(json.load(open(p))['output_dir'])
PY
)

cd "${REPO_ROOT}/Molecular Docking"
python modules/docking/agent.py run-from-network \
  --network-run-dir "$NEW_NP_RUN_DIR" \
  --max-targets 3 \
  --max-ligands 5 \
  --max-candidates-per-target 3
```

### 验证 latest network docking run

```bash
cd "${REPO_ROOT}/Molecular Docking"
python modules/docking/agent.py validate-network-run \
  --dock-dir "$NEW_NP_RUN_DIR/09_docking"
```

