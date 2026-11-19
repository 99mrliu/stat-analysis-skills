# 检验选择决策树（infer-engine 内置规则）

## 两组独立样本
1. 各组 n≥8：做正态性检验（scipy 在场用 Shapiro-Wilk；否则 D'Agostino K²，n≥8 有效）。
2. Levene（中位数中心，即 Brown-Forsythe）方差齐性检验。
3. 两组均正态（p≥0.05）且方差齐 → **Student t**（合并方差）。
4. 均正态但方差不齐（Levene p<0.05）→ **Welch t**（Satterthwaite 自由度）。
5. 任一正态性 p<0.05 或 n<8 → **Mann-Whitney U**（主），Welch t 仅作敏感性参考并注明。
   理由：n<8 时正态性检验功效不足，参数检验的 I 类错误不可控。

## k 组独立样本（k≥3）
1. 每组 n≥8 且均正态：
   - Levene p≥0.05 → **单因素 ANOVA**；效应量 η²、ω²。
   - Levene p<0.05 → **Welch ANOVA**（Welch 1951 自由度校正）；效应量 η²。
2. 任一非正态或存在 n<8 小组 → **Kruskal-Wallis H**（tie 校正）+ ε²=(H−k+1)/(N−k)。

## 事后两两比较
- 与主检验同路线：ANOVA → 成对 Student t；Welch ANOVA → 成对 Welch t
  （逐对 Satterthwaite 自由度，即 Games-Howell 的 t 部分）；Kruskal → 成对 MW。
- 全部事后 p 值一个 Holm 家族。事后比较不可用未校正的原始 p 下结论。

## 配对设计
- 差值正态（n≥8）→ 配对 t；否则 **Wilcoxon 符号秩**（零值剔除、tie 校正、
  正态近似）；效应量 matched-pairs rank-biserial r = z/√N。

## 相关与回归
- Pearson r + Fisher z 区间（观测数据，不推断因果）。
- 任务出现“随…变化/回归/预测” → OLS：斜率、斜率 95%CI、R²、t、p。

## 比例/列联
- 分类×分类 → 卡方独立性（无 Yates 校正，文档注明）+ Cramér's V。

## 阈值与口径
- α 取 case.json constraints.alpha（缺省 0.05）。
- 效应量小/中/大：d/g 0.2/0.5/0.8；η²/ω²/ε² 0.01/0.06/0.14；r 0.1/0.3/0.5。
- 正态性 α 与主检验 α 同为 0.05；正态性边界 p∈[0.02,0.10] 时在 digest 提示复核
  （检验功效与稳健性权衡），不改变自动选择。
