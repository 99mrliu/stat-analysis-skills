# USAGE — verifiable-report 使用说明

## 快速开始（评测运行时）

```bash
python3 scripts/report.py build --input-dir /input --work-dir /app/work --output-dir /app/output
python3 scripts/check.py --output-dir /app/output   # 全部 PASS 后方可交付
```

## 完整流程

1. **（上游）检验引擎**产出 `/app/work/findings.json`（结构见 `templates/findings.example.json`）；
2. **本组件**消费 findings：
   ```bash
   python3 scripts/report.py build --input-dir /input --work-dir /app/work \
       --output-dir /app/output --findings /app/work/findings.json
   ```
3. agent 阅读 DIGEST 与 report.md「结果解释」一节，必要时微调措辞（规则见 `references/wording-rules.md`）；
4. 微调后重跑 `check.py --output-dir /app/output`，全部 PASS 再交付；
5. 交付物：`result.json`、`report.md`、`figures.md`、`fig1.png`（可能含 fig2.png）。

## 无 findings 模式

findings 缺失时自动运行最小描述统计：仅输出描述统计与清洗计数，inferential 字段标记 `not_computed`，status=`partial`。此模式适合先出数据摘要，待检验引擎补齐后再重建。

## 覆盖推断列

```bash
python3 scripts/summarize.py --input-dir /input --work-dir /app/work --outcome strength --group age
```

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 就绪（可含 WARN 或部分结果提示） |
| 2 | 自检 FAIL，禁止交付 |
| 1 | 构建失败（已写出诚实 partial result.json，见 exceptions） |

## 资源约束

纯文本 ≤50 文件、单文件 ≤500KB、总计 ≤2MB、无符号链接。本 bundle 共 12 个文本文件。
