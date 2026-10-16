# Reverse Design 模块方案概览

更新时间：2026-05-28
模块路径：`Reverse Design/`

## 1. 模块定位

Reverse Design（反向设计）是 NPFlow-Agent 的第三个自动化模块，承接分子对接的输出结果，进行基于片段的先导化合物优化（Fragment-Based Lead Optimization）。

总体链路位置：

```text
用户输入中药/化合物 + 疾病
-> Network Pharmacology (网络药理学)
-> Molecular Docking (分子对接)
-> Reverse Design (反向设计)  ← 本模块
```

## 2. 核心目标

给定分子对接的最佳复合物（receptor-ligand complex），通过以下步骤尝试优化配体：

1. 从 docking 结果中选择得分最优的复合物作为种子（seed pairs）
2. 识别配体上适合片段替换的连接位点（attachment points）
3. 调用 DeepFrag2 模型生成候选片段（真实 AI 推理）
4. 将候选片段组装到原始配体上，生成新分子
5. 对新分子进行质量过滤（RDKit sanitize、QED、PAINS、分子量等）
6. 用 AutoDock Vina 重新对接验证新分子的结合得分
7. 比较新旧得分，接受改善达到阈值的候选分子
8. 支持多轮迭代优化

## 3. 推理后端

| 后端 | 说明 | 用途 |
|------|------|------|
| `deepfrag2` | 真实 DeepFrag2 v2.0.0 CPU 推理 | 生产使用，生成真实化学片段 |
| `dry_run` | 随机片段，不调用模型 | 开发测试、环境不可用时的 fallback |

DeepFrag2 推理环境：

```text
Reverse Design/.deepfrag2_env/    (独立 Python 3.9.6 venv)
Reverse Design/pretrained_models/ (gte_4_best.ckpt, 19.8MB)
```

与主项目 `.venv` 隔离，因为 DeepFrag2 要求旧版依赖（numpy 1.23.5、rdkit 2024.3.1）。

## 4. 输入输出

### 输入

来自 Molecular Docking 模块的输出目录，必须包含：

```text
<dock_dir>/06_scores/docking_scores_ranked.csv
<dock_dir>/02_receptor_prepared/pdbqt/
<dock_dir>/03_ligand_prepared/pdbqt/
<dock_dir>/04_binding_sites/smoke_vina/<target>/<ligand>/<box>/replicate_1/pose.pdbqt
```

### 输出

默认输出到 `<dock_dir>/09_reverse_design_after_docking/`：

```text
09_reverse_design_after_docking/
  selected_seed_pairs.csv
  reverse_design_qc.json
  reverse_design_all_candidates.csv
  reverse_design_report.html
  pair_<target>_<ligand>/
    input/
    round_1/
      attachment_points.csv
      deepfrag2_fragments.csv
      generated_raw.csv
      generated_filtered.csv
      selected_for_vina.csv
      redocking_results.csv
      accepted_candidates.csv
      redocking/
    round_2/
      ...
```

## 5. 验证状态

状态：可用（DeepFrag2 真实推理已验证通过）。

已验证案例：

- 丹参 + coronary artery disease：40 候选 → 20 Vina 重对接（100% 成功），最佳 +0.870 kcal/mol
- 黄连 + ulcerative colitis：40 候选 → 20 Vina 重对接（100% 成功），最佳 +0.943 kcal/mol
