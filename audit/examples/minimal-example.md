# 最小示例（示意 / EXAMPLE ONLY — 全部为玩具数字，非真实案例输出）

## 输入：`toy.csv`（6 行，含表头）

```csv
id,dose_mg,response,group
1,12 mg,0.41,A
2,9 mg,0.35,A
3,NA,0.52,B
4,12 mg,0.41,A
5,240,0.44,B
6,-3,0.38,B
```

要点：第 3 行 `dose_mg` 缺失；第 4 行与第 1 行完全重复；第 5 行 `240` 疑为异常值；
第 6 行剂量为负（不可能值）。

## 运行

```bash
python3 scripts/audit.py run --input-dir ./toy-input --work-dir /tmp/toy-work --output-dir /tmp/toy-out
```

## 关键结果（示意）

- **缺失**：`dose_mg` 缺失 1/6（16.7%）→ R001，默认 `report_and_exclude`，该列统计 n=5。
- **重复**：第 4 行（数据行索引 3）与第 1 行完全重复 → R002，默认 `drop`，cleaned 表剩 5 行。
- **异常值**：`dose_mg` 的 `240`（Q1=6, Q3=16.5, IQR 口径下远超 3×栅栏；MAD z 同样命中）→ R003，默认 `flag` 不删。
- **不可能值**：`dose_mg = -3`（剂量/含量类列负值）→ 只标记。
- **单位**：列名命中 `mg`；值内后缀 `mg` 出现于 2 格 → unit_hint=mg；`240` 相对其他值大 20 倍 → 提示疑似单位混入（如 μg？仅提示，不换算）。

## 账本（示意）

| rule_id | type | action | 说明 |
|---|---|---|---|
| R001 | missing | report_and_exclude | 1 缺失单元格，保留 |
| R002 | duplicate | drop | 删除数据行 3，200→…（玩具：6→5 行） |
| R003 | outlier | flag | 行 4 值 240，默认标记保留 |
| R004 | outlier | flag | 行 5 值 -3，不可能值，待人工核实 |

## agent 确认流程（示意）

```bash
# 若确认 240 是录入错误：
python3 scripts/audit.py decide --rule R003 --action drop --reason "dose 240 超出量程, 判录入错误" \
    --work-dir /tmp/toy-work --output-dir /tmp/toy-out
python3 scripts/audit.py finalize --work-dir /tmp/toy-work --output-dir /tmp/toy-out
```

finalize 后 cleaned_table.csv 应用最新处置，result.json 的 `agent_decisions` 与账本
`override_reason` 留痕。之后即可把 cleaned_table.csv 交给推断统计阶段
（本 Skill 不出 p 值/效应量/置信区间）。
