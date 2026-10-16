# Reverse Design 工作流逻辑详解

模块路径：`Reverse Design/`
主入口：`modules/reverse_design_after_docking/agent.py`
共用 Python：`python`

## 1. 总体流程

```text
Molecular Docking 输出 (dock_dir)
  ↓
select_seed_pairs          -- 选择 Vina 得分最优的对接复合物
  ↓
prepare_complex            -- 提取 receptor PDB + ligand SDF + 元数据
  ↓
[round 1..N 循环]
  ↓
select_attachment_points   -- RDKit 识别配体上的片段替换位点
  ↓
generate_fragments         -- DeepFrag2 推理生成候选片段
  ↓
assemble_candidates        -- 将片段组装到原始配体上
  ↓
sanitize_candidates        -- RDKit sanitize + QED + PAINS + MW 过滤
  ↓
prepare_candidates_for_vina -- 准备 PDBQT 用于重对接
  ↓
redock_candidates          -- AutoDock Vina 重对接新分子
  ↓
compare_and_accept         -- 比较新旧得分，接受改善达标的候选
  ↓
[如果没有 accepted 且还有 rounds 剩余，进入下一轮]
```

## 2. 各步骤详细逻辑

### 2.1 select_seed_pairs

输入：`dock_dir`（包含 `06_scores/docking_scores_ranked.csv`）

逻辑：
1. 读取 ranked scores CSV
2. 按 Vina score 排序，过滤达到 `--vina-score-cutoff` 的 pair
3. 如果 `require_usable_for_scientific_ranking`，只选择科学可信的 pair
4. 取 top K（`--max-seed-pairs`）作为种子
5. 提取每个 seed 的 target_id、ligand_id、best_score、pair 路径等元数据

### 2.2 prepare_complex

对每个 seed pair：
1. 定位 receptor PDBQT 文件（从 docking 输出提取）
2. 定位 parent ligand 的 best pose（SDF 格式）
3. 提取 receptor PDB（用于 DeepFrag2 输入，不是 PDBQT）
4. 生成 box JSON（记录 binding box 参数）
5. 将所有路径写入 seed dict

### 2.3 select_attachment_points

使用 RDKit 分析 parent ligand SDF：
1. 找到配体上的可用取代位点（hydrogen atoms 或 rotatable bonds）
2. 对每个位点计算 3D 坐标 (x, y, z)
3. 按优先级排序，取 top K（`--top-k-attachment-points`）
4. 输出 `attachment_points.csv`，包含 `attachment_xyz` 字段供 DeepFrag2 使用

### 2.4 generate_fragments（DeepFrag2 推理）

对每个 attachment point 调用 DeepFrag2 CLI：

```bash
deepfrag2 \
  --receptor receptor.pdb \
  --ligand parent_ligand.sdf \
  --branch_atm_loc_xyz "x,y,z" \
  --out predictions.tsv \
  --num_inference_predictions N
```

- DeepFrag2 模型：`gte_4_best`（图Transformer + ESM）
- 推理模式：`inference_single_complex`，CPU only
- 每次预测约 23 秒（Apple Silicon CPU）
- 输出 TSV 包含 fragment SMILES 和 score

### 2.5 assemble_candidates

将片段与原始配体组装：
1. 读取 fragments CSV 和 attachment points CSV
2. 用 RDKit 将片段连接到配体的对应位点
3. 生成新分子的 SMILES
4. 输出 `generated_raw.csv`

### 2.6 sanitize_candidates

过滤规则：
- **RDKit sanitize**：分子必须能被 RDKit 解析和清洗
- **QED**：Drug-likeness >= `--qed-min`（默认 0.30）
- **PAINS**：Pan-Assay INterference compoundS 检测（默认拒绝，`--allow-pains` 可放宽）
- **分子量**：MW <= `--max-mw`（默认 650）
- **母体相似性**：与原配体的 Tanimoto 相似度 >= `--min-parent-similarity`（默认 0.35）
- **重原子数变化**：|新分子 - 母体| <= `--max-heavy-atom-delta`（默认 8）

### 2.7 prepare_candidates_for_vina

1. 将过滤后的候选分子转为 SDF → PDBQT（Meeko）
2. 取 top K（`--vina-candidates-per-round`）
3. 输出 `selected_for_vina.csv`

### 2.8 redock_candidates

用 AutoDock Vina 对新分子进行重对接：
1. 使用与原对接相同的 receptor PDBQT 和 binding box
2. 每个 Vina job 有独立超时（`--timeout-per-vina`，默认 1800s）
3. 记录 new_score、score_improvement、pass_score_cutoff

### 2.9 compare_and_accept

接受标准：
- `new_score <= old_score - improvement_margin_kcal`（默认 1.0 kcal/mol）
- 即新得分必须比旧得分更负（更好）至少 1.0 kcal/mol
- 可设 `--high-confidence-margin-kcal`（默认 1.5）用于高置信度要求

## 3. 后端选择逻辑

```text
--backend deepfrag2
  ├─ .deepfrag2_env/bin/deepfrag2 存在？
  │   ├─ deepfrag2_env_ok == True？
  │   │   ├─ YES → 使用 DeepFrag2Backend
  │   │   └─ NO  → fallback 到 DryRunBackend + warning
  │   └─ 不存在 → fallback 到 DryRunBackend + warning
  └─ 推理中抛异常 → 临时 fallback 到 DryRunBackend（不影响后续 round）
```

## 4. 常用命令

### 环境检查

```bash
cd ${REPO_ROOT}
.venv/bin/python "Reverse Design/modules/reverse_design_after_docking/agent.py" check-env \
  --out-dir results/reverse_design_check \
  --skip-deepfrag2-test
```

### 运行反向设计

```bash
cd ${REPO_ROOT}
.venv/bin/python "Reverse Design/modules/reverse_design_after_docking/agent.py" run \
  --dock-dir "<dock_dir>" \
  --backend deepfrag2 \
  --skip-deepfrag2-test \
  --vina-score-cutoff -6.0 \
  --max-seed-pairs 3 \
  --fragments-per-attachment-point 10 \
  --top-k-attachment-points 3 \
  --max-rounds 3 \
  --improvement-margin-kcal 0.8 \
  --max-total-vina-jobs 30 \
  --timeout-per-vina 120
```

### 选择种子对

```bash
cd ${REPO_ROOT}
.venv/bin/python "Reverse Design/modules/reverse_design_after_docking/agent.py" select-seeds \
  --dock-dir "<dock_dir>" \
  --out-dir results/seed_select \
  --vina-score-cutoff -6.0
```

## 5. 关键参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--backend` | dry_run | 推理后端：deepfrag2 或 dry_run |
| `--vina-score-cutoff` | -6.0 | 选择 seed pair 的最低 Vina 得分 |
| `--max-seed-pairs` | 3 | 最多选择的种子对数 |
| `--fragments-per-attachment-point` | 10 | 每个位点的片段生成数 |
| `--top-k-attachment-points` | 3 | 每个配体的位点数 |
| `--max-rounds` | 3 | 最大优化轮数 |
| `--improvement-margin-kcal` | 1.0 | 接受候选的最小改善量 |
| `--qed-min` | 0.30 | 最低 QED 分数 |
| `--allow-pains` | false | 是否允许 PAINS 子结构 |
| `--max-total-vina-jobs` | 90 | 总 Vina 重对接上限 |
| `--timeout-per-vina` | 1800 | 单次 Vina 超时（秒） |
