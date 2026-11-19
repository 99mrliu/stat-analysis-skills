# 契约字段映射（contract-mapping）

report.py 按 `case.json → output_contract.required_fields` 逐字段装配 result.json。映射规则如下。

## 字段映射表

| 常见字段名 | 来源 | 说明 |
|---|---|---|
| `case_id` | case.json | 原样拷贝 |
| `status` | 本组件 | `complete`（检验结果齐全且无异常）/ `partial`（有 not_computed 或异常）/ `failed`（致命失败，见 exceptions） |
| `cleaning` | findings.cleaning；缺失时本组件最小扫描 | 行数、完全重复数（keep-first 去重）、按列缺失数、源文件 sha256 |
| `descriptive` | findings.group_summaries / 本组件 summarize | outcome、group_variable、每组 n/mean/sd/median/q1/q3/missing、overall |
| `hypothesis_test` | findings.method/statistic/df/p/alpha | 附 significant 布尔与 multiplicity（若有） |
| `effect_size` | findings.effect_size{name,value,ci95} | 原样透传 |
| `confidence_interval` | findings.effect_size.ci95 | {parameter, level:0.95, ci95, note}；无 ci95 时 not_computed |
| `interpretation` | 本组件措辞引擎 | {claim_strength, significant, significance_zh, practical_zh, ci_zh, conclusion_zh, caveats_zh} |
| `limitations` | findings.limitations + 自动检测 | 字符串列表 |
| `sources` | case.json + 源文件哈希 | 数据集引用 + 文件名/sha256 |
| `uncertainty` | 本组件 | needs_human_confirmation、CI 误读警示、claim_strength |

## 通用规则（最重要）

1. **绝不虚构**：任何必填字段若无法诚实填写，一律写 `{"status":"not_computed", "reason":"…"}`（不适用场景用 `"not_applicable"`），不得编造数值凑满契约；
2. 推断性字段（p 值、效应量、CI）只能来自 findings.json（检验引擎），本组件不会也不允许自行计算；
3. 契约之外的附加字段（sources/uncertainty/reproducibility/quality_checks/exceptions）一并写出，作为诚实性与可复现性的证据；
4. 本组件不负责、也不应填写的字段（如某些案例要求的模型超参数、清洗明细表）按规则 1 标记 not_computed 并注明责任组件；
5. `quality_checks` 由 check.py 写入；存在 FAIL 时禁止交付。
