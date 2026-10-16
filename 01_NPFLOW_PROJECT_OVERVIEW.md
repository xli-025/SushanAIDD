# NPFlow-Agent 项目总览

更新时间：2026-05-28
项目根目录：`${REPO_ROOT}`

## 1. 项目定位

NPFlow-Agent 是一个本地 Python 自动化项目，由三个已跑通的工作流模块组成：

1. `Network Pharmacology`：网络药理学自动化模块。
2. `Molecular Docking`：分子对接自动化模块。
3. `Reverse Design`：反向设计（片段优化）自动化模块。

当前已实现三模块端到端贯通：

```text
用户输入中药/化合物 + 疾病
-> Network Pharmacology（三数据源：TCMSP + SymMap2 + HerbiV）
-> docking_targets.csv / docking_ligands.csv（按多因子加权评分排序）
-> Molecular Docking
-> ranked docking scores
-> Reverse Design (DeepFrag2)
-> optimized candidate molecules
```

## 2. 当前可信状态

三个模块均已通过独立验证和集成验证。

### Network Pharmacology

状态：可用。**v2 更新：多因子加权打分系统 + HerbiV 数据源。**

数据来源（三类）：

| 来源 | 权重 | 说明 |
|------|------|------|
| TCMSP | ingredient 1.00 / target 0.90 | 一级来源，OB/DL 过滤 |
| HerbiV | ingredient 0.75 / target 0.65 | 本地 CSV 导入，combined_score 驱动置信度 |
| SymMap2 | ingredient 0.65 / target 0.60 | 官方下载 + API 查询 |

打分系统（替代旧版等权均值 rank）：

- **ligand_score** = 0.30×品置信度 + 0.25×结构质量 + 0.20×靶点关联 + 0.15×疾病相关性 + 0.10×多来源支持
- **target_score** = 0.25×来源置信度 + 0.25×疾病关联 + 0.20×PPI拓扑 + 0.20×化合物覆盖 + 0.10×多来源支持

已验证能力：

- 支持中药模式：例如 `中药=金银花，疾病=influenza`。
- 支持单体化合物模式：例如 `化合物=补骨脂素，疾病=肝毒性`。
- 能自动生成：
  - `07_core_targets/docking_targets.csv`（含 target_score、selection_reason 等 19 字段）
  - `07_core_targets/docking_ligands.csv`（含 ligand_score、ingredient_confidence 等 21 字段）
  - `08_report/qc_report.json`（含 final_scoring 段）
  - `08_report/network_pharmacology_report.md`
  - `08_report/network_pharmacology_report.html`

### Molecular Docking

状态：可用。

已验证能力：

- 依赖诊断：`check-deps`
- Vina smoke fixture：`validate-engine`
- ligand preparation：CID/SMILES -> SDF/PDBQT
- receptor preparation：local/RCSB PDB -> cleaned receptor -> PQR -> PDBQT
- Meeko-first receptor backend
- PDB2PQR/PROPKA pH 7.4 protonation
- reference pocket ligand box
- network run bridge：`run-from-network`
- scientific ranking / raw ranking 分离
- validation contract：`validate-network-run`

### Reverse Design

状态：可用（DeepFrag2 真实推理已验证通过）。

已验证能力：

- DeepFrag2 v2.0.0 CPU-only 推理（独立环境隔离）。
- 从 docking 结果自动选择 seed pairs。
- RDKit 识别片段替换位点（attachment points）。
- AI 生成候选片段并组装新分子。
- QED / PAINS / MW 等质量过滤。
- AutoDock Vina 重对接验证。
- 多轮迭代优化。
- 金银花+influenza 全链路验证：40 候选 → 12 Vina 重对接 → 4 个 accepted（最佳改善 +1.987 kcal/mol）

## 3. 全链路验证案例

### 金银花 + influenza（三模块全链路）

Network Pharmacology run 目录：

```text
${REPO_ROOT}/Network Pharmacology/results/network_pharmacology_runs/金银花_influenza_20260528_193534
```

Network Pharmacology 结果：

```text
workflow_status: passed
active_ingredients: 71
intersection_targets: 444
docking_targets: 16（按 target_score 排序）
docking_ligands: 25（按 ligand_score 排序）
```

Docking 结果：

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

Reverse Design 结果：

```text
Status: completed
n_raw_candidates: 40
n_filtered_candidates: 12
n_vina_jobs: 12
n_accepted: 4
best_improvement: +1.987 kcal/mol（Baicalein 甲基苯基衍生物）
```

## 4. 当前科学默认原则

这些原则是目前系统可信的核心，不建议下一位 agent 随意改动：

- 不默认启用 blind docking。
- 没有 reference pocket ligand 的 target 跳过，并写入 QC 原因。
- receptor preparation 使用 PDB2PQR/PROPKA pH 7.4 + Meeko-first。
- OpenBabel 只能作为 fallback 或 workflow validation，不应作为高可信科学排序输入。
- receptor PDBQT 若出现 zero-charge warning，不可进入 `scientific_usable` 排名。
- raw Vina score 和 scientific usable ranking 必须分离。
- low-confidence docking pair 不得被报告为 best scientific result。
- ligand 默认 single state，不默认枚举 tautomer/protomer。
- AutoDock Vina score 只用于候选排序，不等同于实验结合自由能或疗效证明。

## 5. 推荐的下一步方向

可以继续做：

- 新增更多中药/疾病组合的稳定性验证。
- 增加一键 integrated CLI，将网药 Manager 与 docking bridge 包成一个命令。
- 强化报告中 skipped target、rejected PDB candidate、low-confidence pair 的解释。
- 增加 ligand tautomer/protomer enumeration 的可选模式，但不要默认打开。
- 后续单独规划 MD、MM/PBSA、GNINA 或 blind docking。

暂时不建议做：

- 推倒重写两个已通过模块。
- 为了凑 docking 分数启用 blind docking。
- 将 OpenBabel zero-charge fallback 当作高可信 scientific ranking。
- 在没有验证器通过前大规模全量跑所有 target/ligand。

