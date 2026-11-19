# data-hygiene-audit 使用文档（Bundle 2：数据清洗与 Schema 审计）

## 1. 定位

统计分析流水线的**第一段**：把 `/input/` 下的原始数据（CSV 快照 + 同源 SQLite 库）变成
"干净、可审计、分析就绪"的表，并产出质量报告。**不做假设检验、效应量、置信区间**——
那些属于推断统计阶段，本包的 result.json 会以 `not_in_scope` 诚实标注。

## 2. 运行环境约定

- 评测环境把本 Skill 装到 `/skills/weekly-skill`，输入在 `/input/`（`case.json`、`task.md`、
  CSV、`.db`），输出必须写到 `/app/output/`。
- 只依赖 Python 3 标准库；无网络、无第三方包也能跑。
- 本地测试可把 `--input-dir` 指向任意目录。

## 3. 三步用法

```bash
# 第 1 步：完整审计（唯一的一条重命令；通常 <30 秒）
python3 scripts/audit.py run --input-dir /input --work-dir /app/work --output-dir /app/output

# 第 2 步：读摘要
cat /app/work/digest.md
# → "发现了什么" + 至多 5 条待确认决策（每条带默认动作和覆盖命令模板）

# 第 3a 步（可选）：覆盖默认处置，可 0~5 次
python3 scripts/audit.py decide --rule R003 --action keep --reason "该列偏态属真实分布" \
    --work-dir /app/work --output-dir /app/output

# 第 3b 步：合并决策并重写 result.json 与 cleaned_table.csv
python3 scripts/audit.py finalize --work-dir /app/work --output-dir /app/output
```

`run` 结束后终端会打印 `NEXT:` 提示行，照做即可。

## 4. 输入解析行为

- **case.json**：回显 `case_id`；解析 `output_contract.required_fields`（result.json 保证每个键存在）；
  采用 `constraints` 中的 `missing_policy`（默认 `report_and_exclude`）与 `duplicate_policy`
  （默认 `dedupe_exact_keep_first`），缺失时在 `uncertainty` 中声明。
- **文件清点**：递归扫描，记录 name/size/sha256；格式按扩展名 + magic bytes
  （`SQLite format 3\0`）判定。
- **CSV**：编码依次尝试 utf-8-sig → utf-8 → gbk → latin-1；分隔符嗅探 comma/tab/分号/空白；
  表头按"首行非数值比例"启发式判定，无表头则命名 `col_1..col_N`；全空行丢弃。
- **SQLite**：`sqlite_master` 列表、`PRAGMA table_info` 取声明类型并映射 affinity、
  行数、全量读取（上限 50 万行防御截断）。

## 5. 审计内容速查

| 模块 | 产出位置（result.json） | 要点 |
|---|---|---|
| 缺失 | `quality.missing` | 每列/每行计数与百分比、Top 缺失模式、MAR/MNAR 启发式提示；不静默插补 |
| 重复 | `quality.duplicates` | 完全重复（行索引、`exact_removed`）+ 近似重复组（3 位有效数字键，只报告） |
| 异常值 | `quality.outliers` | 每列 Q1/Q3/IQR、1.5×/3× 栅栏、MAD 修正 z>3.5；≤10 唯一值分组列的组内口径；不可能值检查 |
| 跨源 | `quality.cross_source` | CSV↔SQLite 形状配对、exact/fuzzy/positional 列映射、行数 diff、逐值比对、numeric-in-text、量级疑点（10³/10⁶/12/2.54 ±5%） |
| 单位 | `quality.unit_scale_hints` | 列名单位词、值内单位后缀、合理性区间（仅提示） |
| 数据字典 | `data_dictionary` | 每列 inferred_type、unit_hint、missing、n_unique、数值七项摘要或 top 类别 |
| 清洗账本 | `cleaning_ledger` | 有序规则：rule_id/type/scope/condition/action/rows_affected/before/after |

## 6. 裁决约定

- 所有规则已按 `default_action` 应用；agent 只确认或覆盖。
- `keep` / `drop` / `flag` 三选一；覆盖必须写 `--reason`，落进 `agent_decisions` 与账本 `override_reason`。
- 异常值默认 `flag`，绝不静默删除；完全重复默认按约束删除；缺失默认报告+按变量有效 n。
- 澄清问题最多 5 个，默认值能覆盖的不问。

## 7. 输出契约

`/app/output/`：

- `result.json`：case_id、status（complete/partial/failed）、source_files、data_dictionary、
  quality、cleaning_ledger、constraints_applied、agent_decisions、exceptions、uncertainty、
  limitations，以及 `output_contract.required_fields` 中的每一个键：
  `cleaning`→质量+账本；`descriptive`→数据字典数值摘要；`interpretation`→中文质量小结；
  `hypothesis_test/effect_size/confidence_interval`→`{"status":"not_in_scope", ...}`。
- `audit_report.md`：中文结构化报告（数据概况→质量审计→清洗规则与影响→口径与单位→遗留问题）。
- `cleaned_table.csv`：账本确定性应用后的分析就绪表（work 目录有副本）。

## 8. 错误处理与诚实性

- 主流程全 try/except；阶段失败记入 `exceptions`，状态降级 `partial`；入口级崩溃兜底写
  `status=failed` 的 result.json。
- 空输入目录、缺 case.json、无数据表均正常退出（exit 0）并说明缺什么。
- `finalize` 找不到 `state.json` 时写 `failed` 结果并返回 exit 1，提示先跑 `run`。

## 9. 已知局限

- 跨源逐值比对假设两源行序一致。
- 表头探测是启发式；近似重复仅供人工核查。
- 合理性区间只提示不拒绝；单位不自动换算。
- 这些局限同时写入 result.json 的 `limitations`/`uncertainty`。
