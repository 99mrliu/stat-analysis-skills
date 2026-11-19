# 报告结构模板【示例】

> 以下为 report.md 的标准结构模板（数值为虚构示例）。正式报告由 scripts/report.py 自动生成，数字必须与 result.json 完全一致（check.py 强制校验）。

```markdown
# 统计分析报告：{case_id}

本报告由 verifiable-report 组件自动生成，措辞遵循校准语言规范。

## 一、数据与口径
数据源：{文件名}；原始 {n0} 行，去除完全重复 {nd} 行后纳入 {n1} 行。
缺失计数（按列）：{missing_by_column}。
结果变量：{outcome}；分组变量：{group_variable}（共 {k} 组）。
单位与口径以原始数据为准，如有疑义需人工确认。

## 二、描述统计
| 组 | n | mean | sd | median | q1 | q3 | missing |
|---|---|---|---|---|---|---|---|
| {g1} | … |
总体：n=…，mean=…，sd=…，median=…。

## 三、检验方法与假设
方法：{method}；统计量={stat}；df={df}；p={p}；α={alpha}。
多重比较校正：{method}；调整后 p={adjusted_p}。
（无检验结果时：统计检验未执行：推断性字段标记 not_computed，需要由检验阶段提供。）

## 四、效应量与置信区间
效应量 {name}={value}。
{name}的 95% 置信区间为 [{lo}, {hi}]；该区间刻画估计量的不确定性，并非个体取值的范围。

## 五、结果解释
{significance + effect 句}
{practical significance 句}
结论强度标签：{tentative|moderate|strong|not_applicable}。

## 六、局限与待确认
- {局限列表}

## 附：图形
图一：{boxplot|ecdf|bar_ci|slope|histogram}（{选择理由}）。
文件：fig1.png；图注与样本量见 figures.md。

## 附：可复现性
输入哈希、运行环境与脚本哈希见 result.json 的 reproducibility 字段。
```
