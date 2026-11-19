---
name: infer-engine
description: >-
  Distribution-aware inference engine for fixed research-data cases: one command
  cleans guard-rails (exact dedup, missing exclusion), detects the design from
  task.md (two-group / k-group / focused contrast / paired / correlation /
  OLS regression), checks normality (Shapiro-Wilk via scipy when present, else
  pure-Python D'Agostino K^2) and variance homogeneity (Levene), then selects
  the matching test (Student/Welch t, Mann-Whitney U, ANOVA/Welch ANOVA/Kruskal-
  Wallis with tie correction, paired t/Wilcoxon signed-rank) instead of forcing
  a default. Every primary result is bound to design-matched effect sizes
  (Cohen's d / Hedges' g / η² / ω² / ε² / rank-biserial r) with 95% CIs
  (bootstrap with fixed seed, Welch CI, Hodges-Lehmann), post-hoc contrasts use
  Holm (Games-Howell df when variances are unequal), and multiple outcomes get a
  second Holm family. Outputs /app/output/result.json mirroring the case
  output_contract, a Chinese report, boxplot figures, and a digest that leaves
  the agent only ≤5 confirmations. 统计分析与结果解读：先检验假设、再选检验、
  效应量与置信区间绑定结论、多重比较校正，数字全部可复算（纯 Python 统计库
  经 scipy 交叉校验）。
---

# infer-engine — 分布感知推断与效应量引擎

**核心思想：** 不让模型“凭感觉”选检验。一条命令完成 守卫式清洗 → 设计识别 →
正态性/方差齐性检验 → 自动选检验 → 效应量+置信区间 → 事后比较 Holm 校正 →
中文报告与 result.json。模型只读 digest 并确认至多 5 项裁决。

适用：给定 `/input/case.json` + `/input/task.md` + 数据文件（CSV/TSV/SQLite，
允许缺表头、带缺失 token、含重复记录）的组间比较/相关/回归类统计任务。

**边界（不适用）：** 深层的缺失机制分析、单位/口径审计、跨源（CSV vs SQLite）
一致性核对属于数据清洗层，本 Skill 只做“整行去重 + 缺失排除”守卫，不做插补、
不剔除异常值（秩方法与稳健效应对异常值天然稳健）；实验设计、样本量估算、
论文写作不在范围。

## 工作流——精确命令

**0. 定向（1 次请求）。**

```bash
ls -la /input/ 2>/dev/null
cat /input/case.json; cat /input/task.md
```

记下 `case_id`、`output_contract.required_fields`、`constraints.alpha`
（缺省 0.05）。

**1. 一键运行（0 次模型请求，数秒完成）。**

```bash
python3 scripts/infer.py run --input-dir /input --work-dir /app/work --output-dir /app/output
```

在 `/app/output/` 生成 `result.json`（含 case_id 与全部 contract 字段）、
`report.md`（中文报告：描述统计→假设检验→效应量→区间→事后比较→解释草稿→局限）、
`fig_boxplots.png`（matplotlib 可用时）。`/app/work/digest.md` 列出分组列/结局列
识别结果、主检验选择理由与 ≤5 项待确认裁决。任何失败都会留下诚实的
`status=failed/partial` result.json，绝不静默。

**2. 读 digest，做至多 5 项确认（1–2 次请求）。** 典型裁决：

- 分组列是否选错（无表头数据自动推断，可能把别的低基数数值列当分组）；
- 结局列是否应收窄（如任务点名 flavanoids/alcohol 两列而引擎跑了全部数值列）；
- 任务点名的两组聚焦比较（如“28 天组与 90 天组”）是否已覆盖。

**3. 需要修正时记录裁决并重跑（0–1 次请求）。**

```bash
python3 scripts/infer.py decide --key spec --value '{"group":"<列名>","outcomes":["<列1>","<列2>"]}' --reason "digest 确认"
python3 scripts/infer.py finalize --input-dir /input --work-dir /app/work --output-dir /app/output
```

**4. 交付前自查（0 次请求）。** 打开 `report.md` 核对：每个结局都有
n/均值/SD；主检验带选择理由；效应量带 CI；事后比较 p_Holm；解释草稿区分
统计显著与实际意义。确认 `result.json` 可被 `python3 -m json.tool` 解析。

## 方法决策规则（脚本内置，依据见 references/decision-tree.md）

1. **两组**：两组正态（Shapiro/K² p≥0.05）且方差齐（Levene p≥0.05）→ Student t；
   正态但方差不齐 → Welch t；任一正态性不满足或 n<8 → Mann-Whitney U（主），
   Welch t 仅作敏感性参考。n<8 时正态性检验功效不足，按非正态保守处理并注明。
2. **k 组（k≥3）**：全部正态且方差齐 → 单因素 ANOVA + η²/ω²；正态但方差不齐 →
   Welch ANOVA + η²；否则 → Kruskal-Wallis（tie 校正）+ ε²。
3. **事后两两**：与主检验同路线（t / Welch-t(Games-Howell 自由度) / MW），
   Holm 校正；多结局时全部局 p 值再做一个 Holm 家族。
4. **效应量与 CI**：两组给 Cohen's d（bootstrap 95%CI，seed=20240501, B=2000）、
   Hedges' g、rank-biserial r；均值差 Welch CI；中位数差 Hodges-Lehmann。
   k 组给 η²/ω²/ε²。相关给 Pearson r + Fisher z CI。回归给 OLS 斜率 CI。
5. **显著性口径**：α 取 case.json constraints.alpha；p 报告 4 位；p<0.0001 记
   `<0.0001`。结论措辞用“在 α=… 水平下差异具有/不具有统计学意义”，
   禁用“证明”。

## 复现证据

`result.json.reproducibility` 记录种子（20240501）、Python 版本、平台、
UTC 时间；`sources.input_sha256` 记录全部输入文件哈希；统计库
`scripts/statslib.py` 为纯标准库实现（不完全 β/γ 函数计算 t/F/χ² 分布），
已用 scipy.stats 交叉校验（t/F/χ²/Levene/ANOVA/Kruskal/Welch/paired t/
D'Agostino K² 差异 <1e-6）；scipy 在场时正态性用 Shapiro-Wilk，结论不一致时
以 digest 提示人工复核。

## 异常与失败处理

- 无数据表 → result.json `status=failed`，退出码 2，附 NEXT 建议；
- 组内方差为 0 → 正态性记“无意义”，按非正态保守处理；
- 某组 n<8 → 标注“功效不足”，不静默丢弃；
- CSV 与 SQLite 行数不一致 → 以 CSV 快照为准并写入 limitations；
- 全部中间结果在 /app/work，可重跑复算。

## 文件

- `scripts/infer.py` — 编排入口（run/decide/finalize）
- `scripts/statslib.py` — 纯 Python 统计库（分布函数、检验、效应量、CI、Holm/BH）
- `references/decision-tree.md` — 检验选择决策树与阈值
- `references/effect-sizes.md` — 效应量口径、方向与小/中/大阈值
- `references/ci-methods.md` — 各 CI 方法与含义（含“区间≠个体范围”警示）
- `references/multiple-comparisons.md` — Holm/FDR 家族划分规则
- `references/pitfalls.md` — 方法误用清单（三组塞 t 检验、p=0.04 写成证明等）
- `docs/USAGE.md`、`examples/minimal-example.md`（示意）、`templates/findings.example.json`（示例）
