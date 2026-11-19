# infer-engine 使用说明

## 安装与依赖
- 复制本目录为 Skill（如 `/skills/weekly-skill`）。入口 `scripts/infer.py`。
- 依赖：Python 3.8+ 标准库。可选：scipy（正态性用 Shapiro-Wilk；缺失时自动
  退回纯 Python D'Agostino K²）、matplotlib（出图；缺失时写 figures.md 说明）。

## 快速开始
```bash
python3 scripts/infer.py run --input-dir /input --work-dir /app/work --output-dir /app/output
```
输出：
- `/app/output/result.json` — case_id + output_contract 全部字段 + 证据与不确定性
- `/app/output/report.md` — 中文分析报告（数字与 result.json 一致）
- `/app/output/fig_boxplots.png` — 分组箱线图+散点（matplotlib 在场时）
- `/app/work/digest.md` — 运行摘要与 ≤5 项待确认裁决

## 输入要求
- `/input/case.json`：case_id、output_contract.required_fields（可缺省）、
  constraints.alpha（可缺省，默认 0.05）。
- `/input/task.md`：任务文本。引擎从中识别设计（两组/三组/配对/相关/回归/
  聚焦两组如“28 天组与 90 天组”）与结局变量（中英文列名均可）。
- 数据：CSV/TSV/TXT/DAT（自动嗅探编码、分隔符、表头）与 SQLite（只读）。
  多个表时取行数最多的 CSV 为主表，SQLite 用于行数核对。

## 裁决与重跑
```bash
python3 scripts/infer.py decide --key spec --value '{"group":"age","outcomes":["strength"]}' --reason "人工确认"
python3 scripts/infer.py finalize --input-dir /input --work-dir /app/work --output-dir /app/output
```
decisions 记录在 /app/work/decisions.json，result.json 的 sources 中可追溯。

## 局限
- 不做缺失机制推断与插补；不做单位审计（交给数据清洗层）；
-  Welch ANOVA 与 Games-Howell 为正态近似；MW/Wilcoxon 用正态近似（tie 校正），
  极小样本（n<10）时注明；
- 复杂设计（区组、协变量、混合模型、生存分析）超出范围，遇到时在
  exceptions 中说明。

## 自查清单
1. result.json 可被 `python3 -m json.tool` 解析，case_id 与 case.json 一致；
2. 每个结局：n、均值、SD、正态性 p、Levene p、主检验+理由、效应量+CI；
3. 事后比较有 p_Holm；多结局有第二重校正记录；
4. 解释草稿无“证明/有效”等越权措辞；
5. cleaning 记录了去重与缺失排除数。
