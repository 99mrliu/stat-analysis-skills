---
name: data-hygiene-audit
description: 当统计分析案例需要把原始研究数据（CSV/TSV/SQLite，可能含缺失值、异常值、重复行、单位与 schema 口径歧义）整理成可审计的分析就绪表时使用本 Skill。Use when a statistical-analysis case requires turning raw research data into an auditable analysis-ready table: one command (`audit.py run`) 自动完成全部重活——文件清点与 sha256、CSV 编码/分隔符/表头嗅探、SQLite schema 与 type-affinity 审计、CSV↔SQLite 跨源一致性（行数、列映射、numeric-in-text 陷阱、量级 10³/10⁶/12/2.54 疑点）、缺失/重复/异常值/不可能值审计、确定性清洗账本，并一次性写出 result.json + audit_report.md + cleaned_table.csv + digest.md。Agent 随后只需阅读 digest.md，确认或覆盖至多 5 条处置决策（decide 子命令），再运行 finalize 重写 result.json；全程不得执行假设检验——推断统计移交分析阶段。
---

# data-hygiene-audit（数据清洗与 Schema 审计，Bundle 2）

## 何时使用

- 案例输入是原始数据快照：`/input/` 下同时存在 CSV 与同数据 SQLite `.db`，可能有缺失标记、完全/近似重复、异常值、单位口径歧义（如数值以 TEXT 落库、单位后缀、千分位）。
- 任务要求"数据质量审计 → 清洗规则与影响行数 → 分析就绪表"，并且输出必须可复核（确定性、sha256、清洗账本）。
- 不适用：需要假设检验、p 值、效应量、置信区间的阶段——见"范围限制"。

## 精确命令（按顺序）

```bash
# 1) 一次性完成全部审计与清洗（重活全在这一步）
python3 scripts/audit.py run --input-dir /input --work-dir /app/work --output-dir /app/output

# 2) 阅读摘要（只需读这一个文件）
#    /app/work/digest.md —— 发现了什么 + 至多 5 条待确认决策（含默认动作与覆盖命令）

# 3) 如需覆盖某条默认处置（可执行 0~5 次）
python3 scripts/audit.py decide --rule R002 --action keep|drop|flag --reason "理由" --work-dir /app/work --output-dir /app/output

# 4) 合并决策，重写 result.json 与 cleaned_table.csv
python3 scripts/audit.py finalize --work-dir /app/work --output-dir /app/output
```

运行环境约定：输入在 `/input/`（`case.json`、`task.md`、数据文件），产物在 `/app/output/`。
脚本只依赖 Python 3 标准库；任何失败都会写出诚实的 `result.json`（status=partial/failed + exceptions），并以 `NEXT:` 行提示下一步。

## 如何阅读 digest.md

1. **发现了什么**：缺失单元格/行数、完全重复行数、近似重复组数、异常值标记数、跨源一致性结论（行数是否一致、numeric-in-text 列数、量级疑点数、列映射方式 exact/fuzzy/positional）。
2. **待确认决策表（至多 5 条）**：每条给出 rule_id、事项、默认动作、覆盖命令模板。

## 裁决规则（adjudication）

- 每条清洗规则都有 `default_action` 且**已被应用**；agent 的职责是确认或覆盖，而不是从零决定。
- 动作词汇：`keep`（承认该行/值有效）、`drop`（从 cleaned_table.csv 剔除）、`flag`（仅标记保留，默认）。
- **异常值绝不静默删除**：默认 flag；只有当你能从 task.md 或领域常识确认其为录入错误时才 `drop`，并必须在 `--reason` 写明依据。
- 完全重复默认按 `case.json` 的 `duplicate_policy`（缺省 `dedupe_exact_keep_first`）删除；若任务需要保留全部观测（如重复是真实采样），覆盖为 `keep`。
- 缺失默认 `report_and_exclude`：不插补、不删行，下游统计按各变量有效 n；除非 case.json/task.md 明确指示，否则不改。
- 你最多应向用户/上游提 **5 个澄清问题**；能用默认值解决的不要问。

## 输出物

| 路径 | 内容 |
|---|---|
| `/app/output/result.json` | 合同字段全覆盖：case_id、status、source_files(sha256)、data_dictionary、quality（missing/duplicates/outliers/cross_source）、cleaning_ledger、constraints_applied、agent_decisions、exceptions、uncertainty、limitations；`hypothesis_test`/`effect_size`/`confidence_interval` 诚实标注 `not_in_scope`。 |
| `/app/output/cleaned_table.csv` | 账本确定性应用后的分析就绪表（/app/work 另有副本）。 |
| `/app/output/audit_report.md` | 结构化中文报告：数据概况→质量审计→清洗规则与影响→口径与单位→遗留问题。 |
| `/app/work/digest.md` | 一页摘要 + 待确认决策（agent 只需读它）。 |
| `/app/work/state.json` | finalize 所需状态（勿手改）。 |
| `/app/work/decisions.json` | decide 子命令累积的人工决策。 |

## 错误处理

- 脚本主流程整体 try/except 包裹；任一阶段失败记入 `exceptions` 并继续，最终 `status=partial`；入口级崩溃也会兜底写出 `status=failed` 的 result.json。
- 空输入目录、缺 case.json、无数据表均不会崩溃，result.json 说明缺什么。
- 终端输出的 `NEXT:` 行是给你的行动提示，照做即可。

## 范围限制（务必遵守）

- **本 Skill 不运行任何假设检验**：不产出 p 值、效应量、置信区间、正态性/方差齐性检验。result.json 中对应字段为 `{"status":"not_in_scope", ...}`。
- 交付物是"干净、可审计、分析就绪"的数据与质量报告；请把 `cleaned_table.csv` + `data_dictionary` + 清洗账本交给推断统计工具/阶段（如 bundle 的 inference 部分）继续分析。
- 不静默插补、不自动修单位、不硬拒绝合理性区间外的值（仅提示）。

## 参考文档（references/）

- `missingness-rules.md`：缺失标记清单、MCAR/MAR/MNAR、report_and_exclude vs 插补的适用条件。
- `duplicate-policy.md`：exact vs near、keep-first 理由、样本量膨胀风险。
- `outlier-methods.md`：IQR 栅栏、修正 z/MAD、分组上下文、处置词汇、绝不静默删除。
- `unit-glossary.md`：单位词表、10³/10⁶ 量级因子、TEXT-vs-REAL affinity、常见导出陷阱。
- `sqlite-audit.md`：PRAGMA table_info、sqlite_master、affinity 映射表、string-number 陷阱。

模板与示例见 `templates/`、`examples/minimal-example.md`（均为示意，非真实案例数字）。
