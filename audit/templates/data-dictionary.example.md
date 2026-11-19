# 数据字典（示例 / EXAMPLE ONLY — 结构示意，数字均为虚构）

> 本模板展示 result.json 中 `data_dictionary` 每列字段的含义。真实输出为 JSON，此 Markdown 仅供阅读参考。

## 表 `example.csv`（200 行 × 4 列，编码 utf-8-sig，分隔符 comma，有表头）

| 列名 | inferred_type | unit_hint | missing | n_unique | 数值摘要（min/max/mean/sd/median/q1/q3） | top categories |
|---|---|---|---|---|---|---|
| batch | categorical | — | 0 (0.0%) | 3 | — | A:80, B:70, C:50 |
| cement | float | kg（来自列名 kg 词） | 2 (1.0%) | 145 | 102.0 / 540.0 / 281.5 / 104.2 / 272.9 / 192.4 / 350.0 | — |
| age | integer | days（列名） | 0 (0.0%) | 14 | 1 / 365 / 45.7 / 54.3 / 28 / 7 / 56 | — |
| strength | float | MPa（列名） | 5 (2.5%) | 190 | 2.33 / 82.6 / 35.8 / 16.7 / 34.4 / 23.7 / 46.1 | — |

## 字段说明

- `inferred_type`：`integer | float | categorical | text | mixed | empty`，按非缺失单元格的可解析比例判定。
- `unit_hint`：优先取"值内单位后缀"（如 `12 mg`），其次列名单位词；均无则 null。
- `missing_count / missing_pct`：基于缺失标记清单（见 references/missingness-rules.md）。
- `n_unique`：非缺失、NFKC 归一化后的去重数。
- 数值摘要的 `n` 恒等于 `n_valid`（缺失不参与）。
- `top_categories`：出现次数最多的前 5 个类别及计数（仅含文本/类别值的列）。
- SQLite 表额外有 `declared_type` 与 `declared_affinity` 字段，用于 TEXT-vs-REAL 口径核对。
