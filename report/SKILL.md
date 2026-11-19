---
name: verifiable-report
description: 当统计分析案例需要把已算出的数字转化为可辩护、符合输出契约的交付物时使用本技能：它生成校准过的中文结果解释（calibrated interpretation），明确区分统计显著性（statistical significance）与实际意义（practical importance），按数据形态选择图形（figure selection：boxplot / violin / ECDF / bar-with-CI / slope graph），产出与 case.json 的 output_contract.required_fields 完全镜像的 result.json（contract compliance），并附带可复现性包（reproducibility pack：输入 sha256、环境、种子声明、运行日志）与数值级自洽性校验（self-consistency check：报告中的每个数字都必须能在 result.json 中找到）。它不选择统计检验方法、不计算 p 值或效应量——这些是上游检验引擎的职责；缺少检验结果时，它只用自带的最小描述统计模块补齐描述性字段，并把推断性字段诚实标记为 not_computed。仅限 Python 标准库，matplotlib 可选且缺失时自动降级为文字说明，任何异常都会产出诚实的部分结果而不是崩溃。
---

# verifiable-report（Bundle 3：解释措辞 + 契约交付 + 可复现性）

## 何时使用

- 已完成（或将要完成）数据清洗与统计检验，需要把数字写成**可交付**的 `result.json`、`report.md`、图形与可复现性包；
- 案例要求中文、克制、区分统计显著与实际意义的解释措辞；
- 案例的 `case.json` 给出了 `output_contract.required_fields`，需要逐字段满足；
- 需要在交付前做数值级自检（报告中的数字必须与 result.json 一致）。

## 职责边界（重要）

本组件**不选择检验方法、不计算 p 值、不计算效应量**。它消费 `findings.json`：
`{outcome, group_variable, group_summaries{每组 n,mean,sd,median,q1,q3}, method, statistic, df, p, alpha, effect_size{name,value,ci95:[lo,hi]}, multiplicity{method,adjusted_p}（可选）, assumptions（可选）, limitations（可选）, cleaning（可选）}`。
findings 缺失或不完整时，本组件会自己定位主表（递归扫描 /input，识别 csv/tsv/sqlite，编码 utf-8-sig→gbk→latin-1，表头启发式），推断结果变量与分组变量（可用 `--outcome` / `--group` 覆盖），计算**仅描述性**统计，并把全部推断性字段标记为 `{"status":"not_computed","note":"需要由检验阶段提供"}`——绝不虚构 p 值。

## 确切命令

```bash
# 一条命令完成全部重活（构建 + 自检），约数秒内完成
python3 /skills/weekly-skill/scripts/report.py build \
    --input-dir /input --work-dir /app/work --output-dir /app/output \
    [--findings /app/work/findings.json] [--outcome 列名] [--group 列名]

# 单独跑最小描述统计（生成 /app/work/findings.json，仅描述性）
python3 /skills/weekly-skill/scripts/summarize.py --input-dir /input --work-dir /app/work

# 单独自检（交付前必须全部 PASS；FAIL 必须先修复再交付）
python3 /skills/weekly-skill/scripts/check.py --output-dir /app/output
```

退出码：`0`=就绪（可含 WARN）；`2`=自检存在 FAIL，禁止交付；`1`=构建失败（已写入诚实的部分 result.json）。

## Agent 的工作方式（预算友好）

1. 运行上面的 build 命令一次；
2. 只阅读其输出的 **DIGEST**（case_id、status、图形计划、claim_strength、结论句、自检结果）与 `/app/output/report.md` 的「五、结果解释」一节；
3. **确认或微调措辞**：措辞规则见 `references/wording-rules.md`。可微调表述，但必须保持：区分统计显著与实际意义、不使用禁用语（证明／说明有效／显著所以重要／相关即因果）、p 值按「在 α=… 水平下差异具有/不具有统计学意义」表述、保留 CI 含义句与局限句；
4. 若改动了 report.md 或 result.json 中的任何数字，必须重新运行 `check.py`，全部 PASS 后方可交付；
5. 交付物清单：`result.json`、`report.md`、`figures.md`、`fig1.png`（及可选 `fig2.png`）。

## 输出

- `/app/output/result.json`：含 case_id + output_contract 全部必填字段 + status + limitations + sources + uncertainty + reproducibility{input_sha256, python_version, platform, script_sha256, timestamp_utc} + quality_checks + exceptions；
- `/app/output/report.md`：结构化中文报告（数据与口径→描述统计→检验方法与假设→效应量与置信区间→结果解释→局限与待确认），数字与 result.json 完全一致；
- `/app/output/figures.md`：图形选择理由、图注（含每组 n）；matplotlib 缺失时含降级数值表；
- `/app/output/fig1.png`（可选 fig2.png）：按 `references/figure-selection.md` 的规则选择。

## 错误处理

- 任何异常都不会产生未处理崩溃：result.json 仍会写出，`status` 为 `partial` 或 `failed`，`exceptions` 列表记录原因，stdout 打印 NEXT 建议；
- 无法诚实填写的必填字段一律写 `{"status":"not_computed","reason":…}`（规则见 `references/contract-mapping.md`）；
- matplotlib 不可用时图形降级为 figures.md 中的文字说明 + 数值表，不影响其余交付。

## 限制

- 核心逻辑仅标准库；不做检验、不做清洗审计（上游 Bundle 1/2 的职责）；
- 中文措辞模板覆盖两组/多组比较场景；回归、生存分析等场景的解释需 agent 在模板基础上人工扩展，且仍须过 check.py；
- 无头 CSV 的列名为 col1..colN，可用 `--outcome col3` 等显式指定，结果更可读。
