# Reverse Design 模块结构说明

模块路径：`${REPO_ROOT}/Reverse Design/`

## 1. 目录总览

```text
Reverse Design/
  06_REVERSE_DESIGN_OVERVIEW.md       -- 模块方案概览
  07_REVERSE_DESIGN_WORKFLOW.md       -- 工作流逻辑详解
  08_REVERSE_DESIGN_STRUCTURE.md      -- 本文件：目录结构说明
  09_REVERSE_DESIGN_HANDOFF.md        -- 接手说明
  10_REVERSE_DESIGN_VALIDATION.md     -- 验证记录
  .deepfrag2_env/                      -- DeepFrag2 独立 Python 环境（Python 3.9.6）
  pretrained_models/                   -- DeepFrag2 预训练模型
    gte_4_best.ckpt                    -- 主模型权重（19.8MB）
    gte_4_all_sets_frags.smi           -- 片段库 SMILES
    gte_4_all_sets_frags.smi_rdk10_fps.bin  -- 片段指纹
  modules/
    reverse_design_after_docking/      -- 核心代码
      agent.py                         -- CLI 主入口
      README.md                        -- 模块说明
      __init__.py
      schemas/                         -- 数据 schema 定义
      backends/                        -- 推理后端
        __init__.py
        base.py                        -- 抽象基类
        dry_run_backend.py             -- 干跑后端（随机片段）
        deepfrag2_backend.py           -- DeepFrag2 真实推理后端
        future_decompopt_backend.py    -- 预留：分解优化后端
        future_diffleop_backend.py     -- 预留：差异优化后端
      scripts/                         -- 功能脚本
        __init__.py
        common.py                      -- 公共工具函数
        check_environment.py           -- 环境检查
        select_seed_pairs.py           -- 选择种子对接对
        prepare_complex.py             -- 准备复合物文件
        select_attachment_points.py    -- 识别片段替换位点
        assemble_candidates.py         -- 组装候选分子
        sanitize_candidates.py         -- 质量过滤
        prepare_candidates_for_vina.py -- 准备 Vina 输入
        redock_candidates.py           -- Vina 重对接
        compare_and_accept.py          -- 比较并接受候选
        summarize_reverse_design.py    -- 生成汇总报告
        run_deepfrag2.py               -- DeepFrag2 运行辅助
      tests/                           -- 单元测试
        test_select_seed_pairs.py
        test_attachment_point_selection.py
        test_reverse_design_smoke.py
```

## 2. 核心文件说明

### `agent.py`（CLI 主入口）

三个子命令：

| 命令 | 说明 |
|------|------|
| `check-env` | 检查 DeepFrag2 环境是否可用 |
| `select-seeds` | 从 docking 结果选择种子对 |
| `run` | 运行完整反向设计流程 |

关键函数：

- `_backend()`：根据环境状态选择 dry_run 或 deepfrag2 后端
- `run()`：主流程，串联所有步骤
- `check_env()`：环境检查
- `select_seeds()`：单独测试种子选择

### `backends/deepfrag2_backend.py`

DeepFrag2 真实推理后端，通过 subprocess 调用 CLI。

关键设计：
- `_PROJECT_ROOT`：设置为 `Reverse Design/` 目录，DeepFrag2 从 `{cwd}/pretrained_models/` 读取模型
- `_DEEPFRAG2_ENV_BIN`：指向 `Reverse Design/.deepfrag2_env/bin/`
- `_find_executable()`：查找 deepfrag2 可执行文件
- `generate_fragments()`：对每个 attachment point 调用 DeepFrag2，解析 TSV 输出
- `_parse_tsv()`：将 DeepFrag2 TSV 转为标准 fragment rows

### `backends/dry_run_backend.py`

干跑后端，生成随机片段用于测试。同时定义了 `FRAGMENT_FIELDS`（fragment 输出的标准字段列表）。

### `scripts/` 各脚本

| 脚本 | 输入 | 输出 | 说明 |
|------|------|------|------|
| `check_environment.py` | 无 | environment dict | 检测 DeepFrag2 环境状态 |
| `select_seed_pairs.py` | dock_dir | selected_seed_pairs.csv | 从 ranked scores 选种子 |
| `prepare_complex.py` | seed dict | 更新后的 seed dict | 提取 receptor/ligand 文件路径 |
| `select_attachment_points.py` | ligand SDF, receptor PDB | attachment_points.csv | RDKit 识别取代位点 |
| `assemble_candidates.py` | fragments + attachments | generated_raw.csv | 片段组装 |
| `sanitize_candidates.py` | generated_raw.csv | generated_filtered.csv | QED/PAINS/MW 过滤 |
| `prepare_candidates_for_vina.py` | filtered CSV | selected_for_vina.csv + PDBQT | Meeko 转 PDBQT |
| `redock_candidates.py` | selected_for_vina CSV | redocking_results.csv | AutoDock Vina 重对接 |
| `compare_and_accept.py` | redocking_results.csv | accepted_candidates.csv | 比较得分，接受达标候选 |
| `summarize_reverse_design.py` | 各轮数据 | reverse_design_qc.json + all_candidates.csv | 生成最终汇总 |

## 3. 环境隔离说明

```
主项目 .venv (Python 3.9.6)          Reverse Design/.deepfrag2_env (Python 3.9.6)
├── numpy 2.x                        ├── numpy 1.23.5
├── rdkit 2025.x                     ├── rdkit 2024.3.1
├── pandas 2.x                       ├── pandas 1.4.4
├── scipy 1.13.x                     ├── scipy 1.11.4
├── meeko, biopython                 ├── torch 1.11.0
├── (orchestration)                  ├── deepfrag2 2.0.0
└── (used by agent.py)               └── (used by subprocess only)
```

主项目 .venv 负责：数据读取、RDKit 操作、Vina 调用、结果汇总。
.deepfrag2_env 仅在 subprocess 中被 deepfrag2 CLI 使用。

## 4. 输出目录结构

完整的反向设计输出：

```text
<dock_dir>/09_reverse_design_after_docking/
  selected_seed_pairs.csv              -- 选中的种子对
  reverse_design_qc.json               -- QC 汇总报告
  reverse_design_all_candidates.csv    -- 所有候选分子（含得分）
  reverse_design_report.html           -- 可视化报告
  pair_<target>_<inchikey>/
    input/
      pocket_box.json
      receptor.pdb
      parent_ligand.pdbqt
      parent_ligand.sdf
      parent_best_pose.sdf
      parent_best_pose.pdbqt
      parent_metadata.json
      receptor.pdbqt
    round_1/
      attachment_points.csv            -- 取代位点
      deepfrag2_fragments.csv          -- DeepFrag2 生成的片段
      generated_raw.csv                -- 组装后的原始候选
      generated_filtered.csv           -- 过滤后的候选
      selected_for_vina.csv            -- 送入 Vina 的候选
      redocking_results.csv            -- Vina 重对接结果
      accepted_candidates.csv          -- 达标候选
      redocking/
        cand_xxx/
          pose.pdbqt
          vina.log
          config.txt
    round_2/
      ...
```

## 5. 哪些文件可以重新生成

可以重新生成：

- `09_reverse_design_after_docking/` 下的所有内容
- `results/` 下的测试和检查文件

不应随意删除：

- `modules/reverse_design_after_docking/` 下的代码
- `.deepfrag2_env/`
- `pretrained_models/`
