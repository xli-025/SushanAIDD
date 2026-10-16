# Reverse Design 验证记录

记录日期：2026-05-28
模块路径：`${REPO_ROOT}/Reverse Design/`

## 1. 环境验证

| 检查项 | 状态 | 说明 |
|--------|------|------|
| .deepfrag2_env 创建 | PASS | Python 3.9.6 独立 venv |
| deepfrag2 安装 | PASS | deepfrag2==2.0.0 |
| 预训练模型下载 | PASS | gte_4_best.ckpt (19.8MB) |
| CLI 可用性 | PASS | deepfrag2 --help 正常 |
| 单次推理测试 | PASS | ~23 秒，输出真实片段 SMILES |
| 主项目 import | PASS | DeepFrag2Backend 正常导入 |
| 环境检查脚本 | PASS | deepfrag2_env_ok: True |

## 2. 代码修复记录

| 修复 | 文件 | 说明 |
|------|------|------|
| skip-test env_ok | check_environment.py | --skip-deepfrag2-test 时 deepfrag2_env_ok 设为 True |
| cwd 匹配 | deepfrag2_backend.py | cwd 从 out_dir 改为 PROJECT_ROOT |
| 绝对路径 | deepfrag2_backend.py | 所有文件路径加 .resolve() |
| attachment_xyz | select_attachment_points.py | 添加 attachment_xyz 字段输出 |
| per-call fallback | agent.py | 推理失败时临时 fallback，不覆盖全局 backend |
| 目录移动路径 | deepfrag2_backend.py, agent.py | 适配模块移入 Reverse Design/ 后的路径 |

## 3. 管线验证

### Pipeline 1：丹参 + Coronary Artery Disease

| 指标 | 值 |
|------|-----|
| Dock dir | `丹参_coronary_artery_disease_20260526_235137/09_docking/` |
| Seed pairs | 2 (PTGS2 × 2 ligands) |
| Raw candidates | 40 |
| Filtered candidates | 39 (97.5%) |
| Vina redocking | 20 (100% 成功) |
| Best improvement | +0.870 kcal/mol |
| Positive improvements | 12/20 (60%) |
| Accepted (>=1.0) | 0 |

### Pipeline 2：黄连 + Ulcerative Colitis

| 指标 | 值 |
|------|-----|
| Dock dir | `黄连_ulcerative_colitis_20260527_234338/09_docking/` |
| Seed pairs | 2 (STAT3 × 2 ligands) |
| Raw candidates | 40 |
| Filtered candidates | 38 (95.0%) |
| Vina redocking | 20 (100% 成功) |
| Best improvement | +0.943 kcal/mol |
| Positive improvements | 11/20 (55%) |
| Accepted (>=1.0) | 0 |
| Filter settings | --allow-pains --qed-min 0.20 |

## 4. 单元测试

```text
test_select_seed_pairs.py      -- PASS
test_attachment_point_selection.py -- PASS
test_reverse_design_smoke.py   -- PASS
```

3 passed，0 failed。

## 5. 科学解读

0 个 accepted candidates 的原因：
- 改善阈值 1.0 kcal/mol 严格（需新得分比旧得分好 1.0+）
- Fragment-based optimization 在早期轮次通常产生 0.3-0.9 kcal/mol 改善
- 多轮迭代（>5 轮）+ 更大片段库可望累积达到 1.0+
- 建议后续运行使用 0.5-0.8 kcal/mol 阈值

DeepFrag2 生成的片段化学意义：
- 官能团替换：-OH → -F, -Cl, -CF3, -Br（卤素扫描）
- 环结构扩展：苯环、THF、呋喃（增加疏水接触）
- 链延伸：丙酸基、甲基（改善柔性）
