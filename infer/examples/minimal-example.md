# 最小调用示例（示意，非真实案例结果）

输入（玩具数据，`/input/toy.csv`）：
```
group,value
A,10.2
A,9.8
A,11.5
B,12.1
B,13.0
B,12.6
```

命令：
```bash
python3 scripts/infer.py run --input-dir /input --work-dir /app/work --output-dir /app/output
```

示意输出（节选，数字仅为演示流程）：
- digest：`设计 k_group；分组列 group；结局 value`
- report.md：两组 n=3/3（n<8 → 正态性记“功效不足”），主检验 Mann-Whitney U，
  效应量 Cohen's d（bootstrap CI），均值差 Welch CI
- result.json：`status: complete`，含 cleaning（去重 0、缺失 0）、descriptive、
  hypothesis_test、effect_size、confidence_interval、interpretation、limitations

注意：上列数值为接口示意，真实数字由运行时数据决定。
