# SQLite 审计要点（sqlite audit）

## 基本命令（Python sqlite3 等价）

```sql
SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';
PRAGMA table_info("表名");          -- cid, name, type, notnull, dflt_value, pk
SELECT COUNT(*) FROM "表名";
```

文件识别：前 16 字节为 `SQLite format 3\0`（magic bytes），比扩展名可靠。

## Type affinity 映射表

| 声明类型包含 | affinity | 存储行为 |
|---|---|---|
| `INT` | INTEGER | 可存整数值 |
| `CHAR`/`CLOB`/`TEXT` | TEXT | **数值也按文本存** → numeric-in-text 陷阱 |
| `BLOB` 或空 | BLOB | 原样存储 |
| `REAL`/`FLOA`/`DOUB` | REAL | 浮点 |
| 其他（如 `NUMERIC`, `DECIMAL`, `DATE`） | NUMERIC | 尽量转数值，转不了按文本 |

## string-number 陷阱（重点）

- 列声明 TEXT 而内容全是数字字符串：`WHERE col > 9` 按字符串比较（`'10' < '9'`）；`AVG(col)` 在 SQLite 中会尝试转换但口径隐晦；导出到 pandas 得到 object dtype。
- 审计判据：affinity=TEXT 且非空值中 ≥95% 可解析为数值 → 记 `numeric_in_text`，账本生成 `type_coercion` 规则（action=coerce_on_read：读取时按数值解析，不改库）。
- 部分可解析（50–95%）→ 记 `type_mismatch`（混合口径），提示可能有单位后缀或千分位混入。

## CSV↔SQLite 同源核对流程（本 Skill 实现）

1. 形状配对：列数相等且行数差 ≤ max(2, 2%)。
2. 列映射：列名 exact → 归一化 fuzzy（去非字母数字、小写）→ positional（如 db 用 c0..c8 泛化列名）。映射方式必须报告。
3. 行数 diff 报告；逐列逐值比对（假设行序一致，已在 uncertainty 中声明）：
   - 数值对相对容差 1e-9；不一致计数。
   - 比值中位数对照 10³/10⁶/12/2.54（及其倒数）±5% → scale suspect。
   - 文本列按 NFKC+strip 精确比较。
4. 每列报告 declared_type/affinity/可解析比例/不一致数，全部进 result.json `quality.cross_source`。

## 只读原则

审计用 `mode=ro` URI 打开（失败退回普通连接但绝不写库）；任何 coercion 只发生在内存与 cleaned_table.csv。
