# 最小示例【示例】（6 行玩具数据）

> 以下为演示用玩具示例，数据为虚构，仅展示命令与输出形态。

输入 `/input/case.json`：

```json
{
  "case_id": "case-toy-demo",
  "output_contract": {"required_fields": ["case_id", "status", "descriptive", "hypothesis_test", "interpretation"]},
  "constraints": {"alpha": 0.05}
}
```

输入 `/input/toy.csv`（6 行，无表头；第 2 列为结果变量，第 3 列为分组）：

```csv
1,10.2,A
2,11.0,A
3,9.8,A
4,12.5,B
5,13.1,B
6,12.8,B
```

命令：

```bash
python3 scripts/report.py build --input-dir /input --work-dir /app/work --output-dir /app/output --outcome col2 --group col3
```

关键输出（节选）：

- DIGEST：`status: partial`、`inferential: False`、`figure_plan: boxplot`；
- `result.json`：`descriptive.groups` 含 A（n=3，mean=10.333333）、B（n=3，mean=12.8）；`hypothesis_test` 为 `{"status":"not_computed", ...}`；
- `report.md`：「统计检验未执行：推断性字段标记 not_computed，需要由检验阶段提供…」；
- `check.py`：全部 PASS。

当检验引擎随后提供 findings.json（含 method/statistic/df/p/effect_size）后，以 `--findings` 重跑，status 变为 `complete`，解释段出现「在 α=0.05 水平下……具有统计学意义」句式与 claim_strength 标签。
