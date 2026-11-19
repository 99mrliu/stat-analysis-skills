#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""infer.py — infer-engine Skill 编排脚本：从 /input 案例输入到推断结果。

用法：
  python3 scripts/infer.py run --input-dir /input --work-dir /app/work --output-dir /app/output
  python3 scripts/infer.py decide --key <k> --value <v> [--reason "…"]   # 记录人工裁决
  python3 scripts/infer.py finalize --input-dir /input --work-dir /app/work --output-dir /app/output

设计原则：
- 纯标准库；scipy / matplotlib 可用时用（Shapiro 检验、图），不可用时自动降级。
- 任何异常都写出诚实的 partial result.json（status=partial|failed），不静默失败。
- 种子固定（bootstrap seed=20240501），输入文件记录 sha256，可复算。
"""
import argparse
import csv
import hashlib
import io
import json
import math
import os
import platform
import re
import sqlite3
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import statslib as SL

SEED = 20240501
MISSING_TOKENS = {"", "na", "n/a", "nan", "null", "none", "-", ".", "?", "缺失", "missing", "NA"}
ALPHA_DEFAULT = 0.05

# ---------------------------------------------------------------- 工具

def log(msg):
    print(msg, flush=True)

def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def fmt(x, sig=4):
    """统一数值格式：4 位有效数字，极小 p 用科学计数。"""
    if x is None or (isinstance(x, float) and x != x):
        return None
    if x == 0:
        return 0
    if abs(x) < 1e-4:
        return float(f"{x:.3e}")
    return float(f"{x:.{sig}g}")

def p_fmt(p):
    if p is None:
        return None
    if p < 0.0001:
        return "<0.0001"
    return f"{p:.4f}".rstrip("0").rstrip(".")

# ---------------------------------------------------------------- 输入发现与加载

def sniff_encoding(path):
    for enc in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as f:
                f.read(1 << 20)
            return enc
        except UnicodeDecodeError:
            continue
    return "latin-1"

def looks_numeric(s):
    try:
        float(str(s).strip())
        return True
    except (ValueError, TypeError):
        return False

def load_delimited(path):
    enc = sniff_encoding(path)
    with open(path, "r", encoding=enc, errors="replace") as f:
        sample = f.read(1 << 20)
    lines = [l for l in sample.splitlines() if l.strip()]
    if not lines:
        return None
    delim = None
    for d in (",", "\t", ";"):
        counts = [l.count(d) for l in lines[:20]]
        if counts and min(counts) > 0 and max(counts) == min(counts):
            delim = d
            break
    if delim is None:
        delim = ","
    rows = list(csv.reader(io.StringIO(sample), delimiter=delim))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return None
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    # 表头启发：首行非数值比例 > 0.5 且 ≥2 列
    first = rows[0]
    nonnum = sum(1 for c in first if not looks_numeric(c) and c.strip() not in MISSING_TOKENS)
    has_header = nonnum / max(1, len(first)) > 0.5 and width > 1
    if has_header:
        header = [c.strip() or f"col_{i+1}" for i, c in enumerate(first)]
        data = rows[1:]
    else:
        header = [f"col_{i+1}" for i in range(width)]
        data = rows
    table = {"columns": header, "rows": data, "source": os.path.basename(path), "kind": "csv"}
    return table

def load_sqlite(path):
    out = []
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        cur = con.cursor()
        tables = [r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        for t in tables:
            cur.execute(f'SELECT * FROM "{t}"')
            cols = [d[0] for d in cur.description]
            rows = [[("" if v is None else str(v)) for v in r] for r in cur.fetchall()]
            out.append({"columns": cols, "rows": rows, "source": f"{os.path.basename(path)}:{t}",
                        "kind": "sqlite"})
        con.close()
    except Exception as e:
        log(f"WARN: sqlite 读取失败 {path}: {e}")
    return out

def coerce(v):
    s = str(v).strip()
    if s.lower() in MISSING_TOKENS:
        return None
    s2 = s.replace(",", "") if re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?", s) else s
    try:
        return float(s2)
    except ValueError:
        return s

def materialize(table):
    """返回 {col: [coerced values aligned to rows]} 与有效行号。"""
    cols = table["columns"]
    data = {c: [] for c in cols}
    for r in table["rows"]:
        for c, v in zip(cols, r):
            data[c].append(coerce(v))
    return data

# ---------------------------------------------------------------- 设计识别

def read_case(input_dir):
    case = {}
    case_path = os.path.join(input_dir, "case.json")
    if os.path.exists(case_path):
        try:
            case = json.load(open(case_path, encoding="utf-8"))
        except Exception:
            pass
    task = ""
    task_path = os.path.join(input_dir, "task.md")
    if os.path.exists(task_path):
        task = open(task_path, encoding="utf-8", errors="replace").read()
    return case, task

def norm_name(s):
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", str(s).lower())

# 常见中文任务措辞 → 可能的列名（中英文数据集快照通用）
CN_GLOSSARY = {
    "抗压强度": ["strength", "强度", "compressive"],
    "强度": ["strength", "强度"],
    "龄期": ["age", "龄期", "day"],
    "花瓣": ["petal", "花瓣"],
    "花萼": ["sepal", "萼片"],
    "类黄酮": ["flavanoid", "flavanoids"],
    "酒精": ["alcohol"],
    "酒精度": ["alcohol"],
}

def pretty_label(v):
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)

def find_column(token, columns):
    """按名称匹配列：精确→子串→去分隔符匹配。"""
    t = norm_name(token)
    if not t:
        return None
    for c in columns:
        if norm_name(c) == t:
            return c
    for c in columns:
        if t in norm_name(c) or norm_name(c) in t:
            return c
    return None

def is_numeric_col(values, min_frac=0.95):
    vals = [v for v in values if v is not None]
    if not vals:
        return False
    nnum = sum(1 for v in vals if isinstance(v, float))
    return nnum / len(vals) >= min_frac

def unique_nonnull(values):
    out, seen = [], set()
    for v in values:
        if v is None:
            continue
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out

def detect_design(task, columns, numeric_cols):
    """从 task.md 识别分析设计，返回 spec dict。"""
    t = task or ""
    spec = {"design": "k_group", "outcomes": [], "group": None,
            "paired_cols": None, "corr_cols": None, "regress": None,
            "focus_pairs": []}
    # 回归：强度随…变化 / 关系 / 预测
    m = re.search(r"(\w+[\u4e00-\u9fff]*)\s*(?:随|与)\s*(\w+[\u4e00-\u9fff]*)\s*(?:的)?\s*(?:关系|变化|回归)", t)
    if m:
        y = find_column(m.group(1), columns) or find_column(m.group(1) + "th", columns)
        x = find_column(m.group(2), columns)
        if y and x and y != x:
            spec["design"] = "regression"
            spec["regress"] = {"y": y, "x": x}
    if re.search(r"回归", t) and spec["design"] != "regression":
        spec.setdefault("regression_mentioned", True)
    if re.search(r"相关", t) and spec["design"] == "k_group":
        spec["design"] = "correlation"
    if re.search(r"配对|前后|重复测量|同一[批组]", t):
        spec["design"] = "paired"
    # 显式两组建组：如 “28 天组与 90 天组”
    nums = re.findall(r"(\d+(?:\.\d+)?)\s*(?:天|d\b|日)", t)
    if len(nums) >= 2 and spec["design"] == "k_group":
        spec["focus_pairs"] = [(nums[0], nums[1])]
        spec["design"] = "k_group_with_focus"
    # 结局变量：在任务文本中出现的列名（含中文措辞翻译）。
    # 注意：任务开头的“字段（单位）：…”声明行会把所有列名都带入文本，
    # 先做声明行剔除，避免所有数值列都被误认成“被点名的结局”。
    task_lines = [l for l in t.splitlines()
                  if not re.search(r"字段|列序|属性说明|单位[:：（]|columns?[:：]", l, re.I)]
    tn = norm_name("\n".join(task_lines))
    named = [c for c in columns if norm_name(c) in tn and c in numeric_cols]
    for cn, cands in CN_GLOSSARY.items():
        if norm_name(cn) in tn:
            for cand in cands:
                hit = find_column(cand, columns)
                if hit and hit in numeric_cols and hit not in named:
                    named.append(hit)
                    break
    spec["outcomes"] = named
    if not spec["outcomes"] and numeric_cols:
        spec["outcomes"] = list(numeric_cols)
    return spec

# ---------------------------------------------------------------- 推断核心

def normality_report(values):
    """返回 {method, stat, p, n, note}。scipy 可用用 Shapiro(n<=5000)，否则 K²。"""
    vals = [v for v in values if v is not None]
    n = len(vals)
    if n < 8:
        return {"method": None, "stat": None, "p": None, "n": n,
                "note": "n<8，正态性检验功效不足，按非正态保守处理"}
    try:
        from scipy import stats as st  # noqa
        if SL.variance(vals) == 0:
            return {"method": None, "stat": None, "p": None, "n": n,
                    "note": "组内方差为 0（常数），正态性无意义，按非正态保守处理"}
        stat, p = st.shapiro(vals)
        return {"method": "Shapiro-Wilk", "stat": fmt(stat), "p": fmt(p), "n": n,
                "note": "scipy.stats.shapiro"}
    except Exception:
        K2, p = SL.dagostino_k2(vals)
        if K2 is None:
            return {"method": None, "stat": None, "p": None, "n": n,
                    "note": "正态性变换不可用，按非正态保守处理"}
        return {"method": "D'Agostino K^2", "stat": fmt(K2), "p": fmt(p), "n": n,
                "note": "纯Python实现，与scipy.stats.normaltest同式"}

def es_magnitude(family, val):
    if val is None:
        return "无法判定"
    a = abs(val)
    if family == "d":
        th = [(0.2, "小"), (0.5, "中"), (0.8, "大")]
    elif family in ("eta2", "eps2", "omega2"):
        th = [(0.01, "小"), (0.06, "中"), (0.14, "大")]
    elif family == "r":
        th = [(0.1, "小"), (0.3, "中"), (0.5, "大")]
    else:
        return "—"
    for cut, lab in th:
        if a < cut:
            return lab
    return "大"

def two_group(x, y, labels, alpha, outcome=None):
    """两组比较全套：假设检验→效应量→CI→解释。"""
    res = {"outcome": outcome or "focused_two_group",
           "groups": {labels[0]: SL.describe(x), labels[1]: SL.describe(y)},
           "assumptions": {}, "tests": [], "effect_sizes": [], "posthoc": [],
           "_group_values": {labels[0]: list(x), labels[1]: list(y)}}
    nx, ny = SL.describe(x), SL.describe(y)
    norm_x = normality_report(x)
    norm_y = normality_report(y)
    W, d1, d2, p_lev = SL.levene_test([x, y])
    res["assumptions"] = {"normality": {labels[0]: norm_x, labels[1]: norm_y},
                          "variance_homogeneity": {"method": "Levene(中位数)",
                                                   "W": fmt(W), "df": [d1, d2], "p": fmt(p_lev)}}
    normal_ok = all(r["p"] is None or r["p"] >= 0.05 for r in (norm_x, norm_y))
    small_n = nx["n"] < 8 or ny["n"] < 8
    homo = (p_lev is None) or (p_lev >= 0.05)
    if small_n:
        normal_ok = False
    if normal_ok and homo:
        t, df, p = SL.ttest_student(x, y)
        res["tests"].append({"method": "Student 独立样本 t 检验", "statistic": fmt(t),
                             "df": fmt(df, 6), "p": fmt(p), "role": "primary",
                             "rationale": "两组均通过正态性检验且方差齐"})
        primary_param = True
    elif normal_ok and not homo:
        t, df, p = SL.ttest_welch(x, y)
        res["tests"].append({"method": "Welch t 检验", "statistic": fmt(t),
                             "df": fmt(df, 6), "p": fmt(p), "role": "primary",
                             "rationale": "两组正态但方差不齐（Levene p<0.05），用 Welch 校正自由度"})
        primary_param = True
    else:
        t, df, p = SL.ttest_welch(x, y)
        res["tests"].append({"method": "Welch t 检验（敏感性参考）", "statistic": fmt(t),
                             "df": fmt(df, 6), "p": fmt(p), "role": "sensitivity",
                             "rationale": "正态性不满足时参数检验仅供参考"})
        U, z, p2, n1, n2 = SL.mannwhitney_u(x, y)
        res["tests"].append({"method": "Mann-Whitney U 检验", "statistic": fmt(U), "z": fmt(z),
                             "p": fmt(p2), "role": "primary",
                             "rationale": "正态性假设不满足（或 n<8），采用秩检验；正态近似，tie 校正"})
        p = p2
        primary_param = False
    d = SL.cohen_d(x, y)
    g = SL.hedges_g(x, y)
    lo, hi, B = SL.bootstrap_ci(SL.cohen_d, (x, y), B=2000, seed=SEED)
    res["effect_sizes"].append({"name": "Cohen's d", "value": fmt(d),
                                "direction": f"{labels[0]} - {labels[1]}",
                                "ci95": [fmt(lo), fmt(hi)], "ci_method": "bootstrap 百分位, B=2000, seed=20240501",
                                "magnitude": es_magnitude("d", d)})
    res["effect_sizes"].append({"name": "Hedges' g（小样本校正）", "value": fmt(g),
                                "direction": f"{labels[0]} - {labels[1]}", "magnitude": es_magnitude("d", g)})
    wlo, whi, wdf = SL.welch_diff_ci(x, y, 1 - alpha) if alpha else (None, None, None)
    res["confidence_intervals"] = [{"estimate": "均值差（Welch）", "value": fmt(nx["mean"] - ny["mean"]),
                                    "ci95": [fmt(wlo), fmt(whi)], "df": fmt(wdf, 6)}]
    hl, hlo, hhi, _ = SL.hodges_lehmann(x, y)
    res["confidence_intervals"].append({"estimate": "中位数差（Hodges-Lehmann）", "value": fmt(hl),
                                        "ci95_approx": [fmt(hlo), fmt(hhi)],
                                        "note": "正态近似分位区间，仅供参考"})
    r_rb = SL.rank_biserial(x, y)
    res["effect_sizes"].append({"name": "rank-biserial r", "value": fmt(r_rb),
                                "direction": f"正= {labels[0]} 秩更高", "magnitude": es_magnitude("r", r_rb)})
    res["primary_p"] = p
    res["primary_parametric"] = primary_param
    return res

def welch_pair(x, y):
    n1, n2 = len(x), len(y)
    m1, m2 = SL.mean(x), SL.mean(y)
    v1, v2 = SL.variance(x), SL.variance(y)
    se2 = v1 / n1 + v2 / n2
    if se2 == 0:
        return 0.0, 1, 1.0
    t = (m1 - m2) / math.sqrt(se2)
    df = se2 ** 2 / ((v1 / n1) ** 2 / (n1 - 1) + (v2 / n2) ** 2 / (n2 - 1))
    p = 2.0 * (1.0 - SL.t_cdf(abs(t), df))
    return t, df, p

def k_group(groups, labels, outcome, alpha):
    """k 组（k>=2）全套：全局检验 + 事后两两 + 效应量。"""
    res = {"outcome": outcome,
           "groups": {str(l): SL.describe(g) for l, g in zip(labels, groups)},
           "assumptions": {}, "tests": [], "effect_sizes": [], "posthoc": [],
           "pairwise_effect_sizes": [],
           "_group_values": {str(l): list(g) for l, g in zip(labels, groups)}}
    norms = [normality_report(g) for g in groups]
    W, d1, d2, p_lev = SL.levene_test(groups)
    res["assumptions"] = {"normality": {str(l): r for l, r in zip(labels, norms)},
                          "variance_homogeneity": {"method": "Levene(中位数)",
                                                   "W": fmt(W), "df": [d1, d2], "p": fmt(p_lev)}}
    all_normal = all(r["p"] is not None and r["p"] >= 0.05 for r in norms)
    any_small = any(SL.describe(g)["n"] < 8 for g in groups)
    homo = (p_lev is None) or (p_lev >= 0.05)
    n_total = sum(len(g) for g in groups)
    k = len(groups)
    if all_normal and homo and not any_small:
        F, df1, df2, p = SL.anova_oneway(groups)
        res["tests"].append({"method": "单因素方差分析（ANOVA）", "statistic": fmt(F),
                             "df": [df1, df2], "p": fmt(p), "role": "primary",
                             "rationale": "各组正态且方差齐"})
        e2 = SL.eta_squared(groups)
        o2 = SL.omega_squared(groups)
        res["effect_sizes"].append({"name": "η²", "value": fmt(e2),
                                    "magnitude": es_magnitude("eta2", e2)})
        res["effect_sizes"].append({"name": "ω²", "value": fmt(o2),
                                    "magnitude": es_magnitude("omega2", o2)})
        pair_route = "student"
        primary_p = p
    elif all_normal and not homo and not any_small:
        F, df1, df2, p = SL.welch_anova(groups)
        res["tests"].append({"method": "Welch 方差分析", "statistic": fmt(F),
                             "df": [fmt(df1, 6), fmt(df2, 6)], "p": fmt(p), "role": "primary",
                             "rationale": "各组正态但方差不齐，Welch 校正自由度"})
        e2 = SL.eta_squared(groups)
        res["effect_sizes"].append({"name": "η²", "value": fmt(e2),
                                    "magnitude": es_magnitude("eta2", e2)})
        pair_route = "welch"
        primary_p = p
    else:
        H, df, p = SL.kruskal_wallis(groups)
        res["tests"].append({"method": "Kruskal-Wallis H 检验", "statistic": fmt(H),
                             "df": df, "p": fmt(p), "role": "primary",
                             "rationale": "正态性不满足或存在 n<8 小组，采用秩方法；H 经 tie 校正"})
        eps2 = SL.epsilon_squared(H, n_total, k)
        res["effect_sizes"].append({"name": "ε²", "value": fmt(eps2),
                                    "magnitude": es_magnitude("eps2", eps2)})
        pair_route = "mw"
        primary_p = p
    res["pair_route"] = pair_route
    # 事后两两
    raw_p = []
    pair_info = []
    for i in range(k):
        for j in range(i + 1, k):
            x, y = groups[i], groups[j]
            if pair_route == "student":
                t, df, p = SL.ttest_student(x, y)
                stat_key, stat_val, extra = "t", t, {"df": fmt(df, 6)}
            elif pair_route == "welch":
                t, df, p = welch_pair(x, y)
                stat_key, stat_val, extra = "t", t, {"df": fmt(df, 6), "method_suffix": "（Welch 校正自由度）"}
            else:
                U, z, p, n1, n2 = SL.mannwhitney_u(x, y)
                stat_key, stat_val, extra = "U", U, {"z": fmt(z)}
            es = SL.cohen_d(x, y) if pair_route != "mw" else SL.rank_biserial(x, y)
            raw_p.append(p)
            pair_info.append({"a": str(labels[i]), "b": str(labels[j]),
                              "statistic_key": stat_key, "statistic": fmt(stat_val),
                              "p_raw": fmt(p), "extra": extra,
                              "diff_mean": fmt(SL.mean(x) - SL.mean(y)),
                              "es": {"name": "Cohen's d" if pair_route != "mw" else "rank-biserial r",
                                     "value": fmt(es)}})
    adj = SL.holm_adjust(raw_p)
    for pi, pa in zip(pair_info, adj):
        pi["p_holm"] = fmt(pa)
        pi["significant_holm"] = bool(pa is not None and pa < alpha)
        res["posthoc"].append(pi)
    res["multiplicity"] = {"method": "Holm", "family": f"C({k},2)={len(raw_p)} 个两两比较",
                           "alpha": alpha}
    res["primary_p"] = primary_p
    return res

# ---------------------------------------------------------------- 解释生成

def verdict(p, alpha):
    if p is None:
        return "无法判定"
    return "差异具有统计学意义" if p < alpha else "差异不具有统计学意义"

def interpret_two(res, labels, alpha):
    t = next((x for x in res["tests"] if x["role"] == "primary"), None)
    if not t:
        return ""
    p = t["p"]
    d = next((e for e in res["effect_sizes"] if e["name"] == "Cohen's d"), None)
    ci = res["confidence_intervals"][0]
    mag = d["magnitude"] if d else "无法判定"
    s = (f"在 α={alpha} 水平下，{labels[0]} 与 {labels[1]} 的均值差为 "
         f"{fmt(ci['value'])}，95% 置信区间 [{ci['ci95'][0]}, {ci['ci95'][1]}]；"
         f"{t['method'].split('（')[0]} 得 {verdict(p, alpha)}"
         f"（{t['statistic_key'] if 'statistic_key' in t else 'p'}={p_fmt(p)}）。"
         f"标准化效应量 Cohen's d={d['value'] if d else 'NA'}（{mag}）。"
         f"置信区间表示均值差估计的不确定性范围，不是个体取值范围。"
         f"任务未给出实际意义阈值 Δ，实际重要性需结合领域判断。")
    return s

def interpret_k(res, labels, alpha):
    t = next((x for x in res["tests"] if x["role"] == "primary"), None)
    if not t:
        return ""
    es = res["effect_sizes"][0] if res["effect_sizes"] else {}
    n_sig = sum(1 for x in res["posthoc"] if x.get("significant_holm"))
    s = (f"在 α={alpha} 水平下，{res['outcome']} 在 {len(labels)} 个组间的总体比较采用 "
         f"{t['method'].split('（')[0]}，{verdict(t['p'], alpha)}"
         f"（p={p_fmt(t['p'])}）；解释比例类效应量 {es.get('name', '')}={es.get('value')}（{es.get('magnitude', '')}）。"
         f"事后两两比较共 {len(res['posthoc'])} 组，经 Holm 校正后 {n_sig} 组显著。"
         f"该结论为组间关联，不能推断因果；任务未给出实际意义阈值，实际重要性需结合领域判断。")
    return s

# ---------------------------------------------------------------- 主流程

def pick_primary_table(tables):
    """优先 CSV 快照；同shape的 SQLite 表仅用于行数核对。"""
    csvs = [t for t in tables if t["kind"] == "csv"]
    dbs = [t for t in tables if t["kind"] == "sqlite"]
    primary = None
    if csvs:
        primary = max(csvs, key=lambda t: len(t["rows"]))
    elif dbs:
        primary = max(dbs, key=lambda t: len(t["rows"]))
    cross = []
    if primary and dbs:
        for d in dbs:
            if d is primary:
                continue
            same = len(d["rows"]) == len(primary["rows"])
            cross.append({"source": d["source"], "rows": len(d["rows"]),
                          "rowcount_match": same})
    return primary, cross

def guard_clean(table):
    """轻量守卫：整行去重（keep first）、缺失不参与计算。返回 (data, cleaning_info)。"""
    cols = table["columns"]
    raw = table["rows"]
    seen = set()
    keep = []
    n_dup = 0
    for i, r in enumerate(raw):
        key = tuple(str(c).strip() for c in r)
        if key in seen:
            n_dup += 1
            continue
        seen.add(key)
        keep.append(i)
    data = materialize({"columns": cols, "rows": [raw[i] for i in keep]})
    missing_per_col = {c: sum(1 for v in data[c] if v is None) for c in cols}
    return data, {"rows_raw": len(raw), "rows_after_exact_dedup": len(keep),
                  "exact_duplicates_removed": n_dup,
                  "missing_tokens_per_column": {c: n for c, n in missing_per_col.items() if n},
                  "note": "仅做整行去重与缺失标记；异常值不做剔除，由各组结果中位数/秩方法稳健处理"}

def numeric_columns_of(data, min_frac=0.95):
    return [c for c in data if is_numeric_col(data[c], min_frac)]

def grouping_candidates(data, numeric_cols):
    out = []
    for c in data:
        if c in numeric_cols:
            continue
        vals = unique_nonnull(data[c])
        if 2 <= len(vals) <= 12:
            out.append((c, vals))
    # 数值型低基数也可作分组（如 age 14 个龄期、cultivar 3 类）
    for c in numeric_cols:
        vals = unique_nonnull(data[c])
        if 2 <= len(vals) <= 16:
            out.append((c, vals))
    return out

def split_groups(data, group_col, outcome_col):
    groups, labels = [], []
    for lv in unique_nonnull(data[group_col]):
        xs = [data[outcome_col][i] for i in range(len(data[outcome_col]))
              if data[group_col][i] == lv and data[outcome_col][i] is not None]
        if xs:
            labels.append(pretty_label(lv))
            groups.append(xs)
    return groups, labels

def build_findings(input_dir, work_dir, output_dir, overrides):
    os.makedirs(work_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)
    case, task = read_case(input_dir)
    case_id = case.get("case_id") or case.get("id") or "unknown"
    contract = (case.get("output_contract") or {}).get("required_fields") or []
    alpha = (case.get("constraints") or {}).get("alpha", ALPHA_DEFAULT)
    exceptions = []
    uncertainty = []
    tables = []
    file_hashes = {}
    for root, _, files in os.walk(input_dir):
        for fn in sorted(files):
            p = os.path.join(root, fn)
            try:
                file_hashes[fn] = sha256_of(p)
            except OSError:
                pass
            low = fn.lower()
            if low.endswith((".csv", ".tsv", ".txt", ".dat")):
                t = load_delimited(p)
                if t and len(t["rows"]) >= 2:
                    tables.append(t)
            elif low.endswith((".db", ".sqlite", ".sqlite3")):
                tables.extend(load_sqlite(p))
    if not tables:
        raise RuntimeError("未发现可用的数据表（CSV/TSV/SQLite）。请检查 /input 内容。")
    primary, cross = pick_primary_table(tables)
    log(f"主数据表: {primary['source']}  ({len(primary['rows'])} 行 × {len(primary['columns'])} 列)")
    data, cleaning = guard_clean(primary)
    numeric_cols = numeric_columns_of(data)
    spec = detect_design(task, primary["columns"], numeric_cols)
    # 人工裁决覆盖
    ov = overrides.get("spec") or {}
    for key in ("design", "group", "outcomes"):
        if key in ov:
            spec[key] = ov[key]
    results = {"case_id": case_id, "design": spec["design"], "alpha": alpha,
               "cleaning": cleaning, "cross_source_check": cross,
               "comparisons": [], "exceptions": exceptions, "uncertainty": uncertainty}
    # ---- 分组选择
    gcands = grouping_candidates(data, numeric_cols)
    group_col = spec.get("group")
    outcomes = []
    if not group_col and gcands and spec["design"] in ("k_group", "k_group_with_focus"):
        # 优先：任务文本提到的列名；否则取候选首列（分类优先）
        named = [c for c, _ in gcands if norm_name(c) in norm_name(task)]
        if named:
            group_col = named[0]
        else:
            group_col = gcands[0][0]
            for kw in ("物种", "类别", " cultivar", "class", "品种", "group"):
                hit = [c for c, _ in gcands if kw.strip().lower() in str(c).lower()]
                if hit:
                    group_col = hit[0]
                    break
    if spec["design"] in ("k_group", "k_group_with_focus") and group_col:
        outcomes = [o for o in spec["outcomes"] if o in numeric_cols and o != group_col]
        if not outcomes:
            outcomes = [c for c in numeric_cols if c != group_col]
        if spec["design"] == "k_group_with_focus" and spec["focus_pairs"] and is_numeric_col(data[group_col]):
            # 聚焦两组建组（数值分组值），对每个结局执行
            a, b = spec["focus_pairs"][0]
            fa, fb = float(a), float(b)
            mask = [i for i, v in enumerate(data[group_col]) if v in (fa, fb)]
            sub = {c: [data[c][i] for i in mask] for c in data}
            data_focus = sub
            for oc in outcomes:
                groups, labels = split_groups(data_focus, group_col, oc)
                if len(groups) == 2:
                    r = two_group(groups[0], groups[1], labels, alpha, outcome=oc)
                    r["context"] = f"聚焦比较：{group_col} ∈ {{{a}, {b}}}（任务点名两组）"
                    results["comparisons"].append(r)
        # 全局 k 组（对每个结局）
        family_p = []
        comp_refs = []
        for oc in outcomes:
            groups, labels = split_groups(data, group_col, oc)
            if len(groups) < 2:
                continue
            r = k_group(groups, labels, oc, alpha)
            results["comparisons"].append(r)
            family_p.append(r["primary_p"])
            comp_refs.append(r)
        # 多结局 → 第二重校正
        if len(family_p) > 1:
            adj = SL.holm_adjust(family_p)
            results["multiplicity_across_outcomes"] = {
                "method": "Holm", "outcomes": outcomes,
                "p_raw": [fmt(p) for p in family_p],
                "p_adjusted": [fmt(p) for p in adj]}
    elif spec["design"] == "regression" and spec.get("regress"):
        rg = spec["regress"]
        x = [v for v in data[rg["x"]] if isinstance(v, float)]
        y = [v for v in data[rg["y"]] if isinstance(v, float)]
        r = SL.linregress(x, y)
        rr, pr = SL.pearson_r(x, y)
        lo, hi = SL.fisher_z_ci(rr, len(x))
        results["regression"] = {"x": rg["x"], "y": rg["y"], **{k: fmt(v) if isinstance(v, float) else v for k, v in r.items()},
                                 "pearson_r": fmt(rr), "r_ci95": [fmt(lo), fmt(hi)],
                                 "note": "观测数据的回归关联，不推断因果"}
    elif spec["design"] == "correlation":
        nums = [c for c in numeric_cols]
        if len(nums) >= 2:
            x = [v for v in data[nums[0]] if isinstance(v, float)]
            y = [v for v in data[nums[1]] if isinstance(v, float)]
            rr, pr = SL.pearson_r(x, y)
            lo, hi = SL.fisher_z_ci(rr, len(x))
            results["correlation"] = {"x": nums[0], "y": nums[1],
                                      "pearson_r": fmt(rr), "p": fmt(pr),
                                      "ci95": [fmt(lo), fmt(hi)]}
    else:
        exceptions.append(f"未能识别设计：spec={spec}；请用 decide 指定 design/group/outcomes")
    # ---- 解释草稿
    interps = []
    for r in results.get("comparisons", []):
        labs = list(r["groups"].keys())
        if len(labs) == 2 and "context" in r:
            interps.append(interpret_two(r, labs, alpha))
        elif r.get("posthoc") is not None and len(labs) >= 2:
            interps.append(interpret_k(r, labs, alpha))
    results["interpretation_draft"] = "\n".join(interps)
    # ---- 局限
    lims = ["未对异常值做剔除，结论对极端值稳健性未单独评估"]
    if any(c.get("rowcount_match") is False for c in cross):
        lims.append("CSV 与 SQLite 行数不一致，以 CSV 快照为准")
    if results.get("multiplicity_across_outcomes"):
        lims.append("多结局比较已做 Holm 校正，结论按校正后 p 值判断")
    lims.append("观测性数据，组间关联不能推断因果")
    results["limitations"] = lims
    results["sources"] = {"primary_table": primary["source"],
                          "input_sha256": file_hashes,
                          "method_library": "statslib.py（纯Python实现，分布函数经scipy.stats交叉校验）"}
    results["uncertainty"] = uncertainty
    results["reproducibility"] = {"seed": SEED, "python": platform.python_version(),
                                  "platform": platform.platform(),
                                  "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    # ---- 输出（剥离内部字段如原始组值，避免 result.json 膨胀）
    def strip_internal(obj):
        if isinstance(obj, dict):
            return {k: strip_internal(v) for k, v in obj.items() if not str(k).startswith("_")}
        if isinstance(obj, list):
            return [strip_internal(v) for v in obj]
        return obj
    result_json = assemble_result(case, strip_internal(results))
    json.dump(result_json, open(os.path.join(output_dir, "result.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    digest = build_digest(results, spec, group_col, outcomes if spec["design"].startswith("k_group") else [])
    open(os.path.join(work_dir, "digest.md"), "w", encoding="utf-8").write(digest)
    open(os.path.join(work_dir, "state.json"), "w", encoding="utf-8").write(
        json.dumps({"spec": spec, "group_col": group_col}, ensure_ascii=False, indent=2))
    write_report(output_dir, case, results, alpha)
    make_figure(output_dir, results)
    log("NEXT: 阅读 /app/work/digest.md，确认至多 5 项裁决；必要时用 decide 子命令记录，然后 finalize。")
    return results

def assemble_result(case, results):
    case_id = results["case_id"]
    contract = (case.get("output_contract") or {}).get("required_fields") or []
    comps = results.get("comparisons", [])
    primary = comps[0] if comps else (results.get("regression") or results.get("correlation") or {})
    out = {
        "case_id": case_id,
        "status": "partial" if results["exceptions"] else "complete",
        "design": results["design"],
        "alpha": results["alpha"],
        "cleaning": results["cleaning"],
        "cross_source_check": results["cross_source_check"],
        "descriptive": {c["outcome"]: c["groups"] for c in comps if "outcome" in c} or
                       ({"regression_data": "见 regression 字段"} if "slope" in primary else {}),
        "assumptions": {c["outcome"]: c["assumptions"] for c in comps if "assumptions" in c},
        "hypothesis_test": [{"outcome": c.get("outcome", "focused_two_group"),
                             "tests": c.get("tests", []), "multiplicity": c.get("multiplicity")}
                            for c in comps] or ([{"method": "OLS 线性回归", **primary}] if "slope" in primary else []),
        "posthoc": {c["outcome"]: c.get("posthoc", []) for c in comps if c.get("posthoc")},
        "effect_size": {c["outcome"]: c.get("effect_sizes", []) for c in comps if c.get("effect_sizes")} or
                       ({"r": primary.get("pearson_r")} if "pearson_r" in primary else {}),
        "confidence_interval": [{**ci, "outcome": c.get("outcome")}
                                for c in comps for ci in c.get("confidence_intervals", [])] or
                               ([{"r_ci95": primary.get("r_ci95")}] if "r_ci95" in primary else []),
        "interpretation": results.get("interpretation_draft", ""),
        "multiplicity_across_outcomes": results.get("multiplicity_across_outcomes"),
        "limitations": results["limitations"],
        "sources": results["sources"],
        "uncertainty": results["uncertainty"],
        "exceptions": results["exceptions"],
        "reproducibility": results["reproducibility"],
    }
    # 兼容 output_contract 要求的字段名
    mapping = {"cleaning": "cleaning", "descriptive": "descriptive",
               "hypothesis_test": "hypothesis_test", "effect_size": "effect_size",
               "confidence_interval": "confidence_interval", "interpretation": "interpretation",
               "limitations": "limitations", "status": "status", "case_id": "case_id"}
    for f in contract:
        if f not in out:
            out[f] = {"status": "not_computed",
                      "note": "infer-engine 未覆盖该字段，请人工补充或说明不适用"}
    return out

def build_digest(results, spec, group_col, outcomes):
    lines = ["# infer-engine 运行摘要（digest）", ""]
    lines.append(f"- 设计识别：`{results['design']}`；分组列：`{group_col}`；结局列：{outcomes}")
    lines.append(f"- 清洗：原始 {results['cleaning']['rows_raw']} 行，整行去重 {results['cleaning']['exact_duplicates_removed']} 行，"
                 f"保留 {results['cleaning']['rows_after_exact_dedup']} 行")
    for c in results.get("comparisons", []):
        if "outcome" in c:
            labs = list(c["groups"].keys())
            lines.append(f"- 结局 `{c['outcome']}`：组 {labs}；主检验 "
                         f"{[t['method'] for t in c['tests'] if t['role']=='primary']}")
    lines.append("")
    lines.append("## 待确认（≤5，默认已按左侧执行）")
    lines.append("1. 分组列选择是否正确（decide --key spec --value '{\"group\": \"<列名>\"}'）")
    lines.append("2. 结局列是否遗漏或多算（decide 覆盖 outcomes）")
    lines.append("3. n<8 小组按非正态保守处理，是否接受")
    lines.append("4. 正态性方法：scipy 可用时为 Shapiro-Wilk，否则 D'Agostino K^2（两者结论通常一致）")
    lines.append("5. 事后比较统一用 Holm 校正；若任务指定 Tukey/Bonferroni/FDR 可覆盖")
    lines.append("")
    lines.append("确认或修正后运行 finalize 重新生成结果。")
    return "\n".join(lines)

def write_report(output_dir, case, results, alpha):
    lines = ["# 统计推断分析报告（infer-engine 自动生成）", ""]
    lines.append(f"- case_id：{results['case_id']}")
    lines.append(f"- 设计：`{results['design']}`，α={alpha}")
    cl = results["cleaning"]
    lines.append(f"- 数据：原始 {cl['rows_raw']} 行；整行去重 {cl['exact_duplicates_removed']} 行；"
                 f"分析用 {cl['rows_after_exact_dedup']} 行；缺失按列排除，未插补")
    lines.append("")
    for c in results.get("comparisons", []):
        if "outcome" not in c:
            continue
        lines.append(f"## 结局：{c['outcome']}")
        lines.append("")
        lines.append("| 组 | n | 均值 | SD | 中位数 |")
        lines.append("|---|---|---|---|---|")
        for lab, d in c["groups"].items():
            lines.append(f"| {lab} | {d['n']} | {fmt(d['mean'])} | {fmt(d['sd'])} | {fmt(d['median'])} |")
        lines.append("")
        for t in c.get("tests", []):
            role = "主检验" if t["role"] == "primary" else "参考"
            lines.append(f"- {role}：{t['method']}，statistic={t.get('statistic')}，p={p_fmt(t.get('p'))}（{t['rationale']}）")
        for e in c.get("effect_sizes", []):
            ci = f"，95%CI [{e['ci95'][0]}, {e['ci95'][1]}]" if "ci95" in e else ""
            lines.append(f"- 效应量：{e['name']}={e['value']}（{e.get('magnitude', '')}）{ci}")
        for ci in c.get("confidence_intervals", []):
            bounds = ci.get("ci95") or ci.get("ci95_approx") or [None, None]
            approx = "（近似）" if "ci95" not in ci else ""
            lines.append(f"- 区间：{ci['estimate']}={ci['value']}，95%CI{approx} [{bounds[0]}, {bounds[1]}]")
        if c.get("posthoc"):
            lines.append("")
            lines.append("事后两两（Holm 校正）：")
            lines.append("")
            lines.append("| 对比 | 均值差 | 统计量 | p_raw | p_Holm | 显著 |")
            lines.append("|---|---|---|---|---|---|")
            for ph in c["posthoc"]:
                lines.append(f"| {ph['a']} vs {ph['b']} | {ph['diff_mean']} | "
                             f"{ph['statistic_key']}={ph['statistic']} | {p_fmt(ph['p_raw'])} | "
                             f"{p_fmt(ph['p_holm'])} | {'是' if ph['significant_holm'] else '否'} |")
        lines.append("")
    if results.get("interpretation_draft"):
        lines.append("## 结果解释（草稿，需人工确认措辞）")
        lines.append("")
        lines.append(results["interpretation_draft"])
        lines.append("")
    if results.get("multiplicity_across_outcomes"):
        lines.append("## 多结局校正")
        lines.append("")
        lines.append(json.dumps(results["multiplicity_across_outcomes"], ensure_ascii=False))
        lines.append("")
    lines.append("## 局限")
    for l in results["limitations"]:
        lines.append(f"- {l}")
    open(os.path.join(output_dir, "report.md"), "w", encoding="utf-8").write("\n".join(lines))

def make_figure(output_dir, results):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        open(os.path.join(output_dir, "figures.md"), "w", encoding="utf-8").write(
            "# 图形\n\n运行环境无 matplotlib，未渲染图片；数据见 report.md 各表。\n")
        return
    comps = [c for c in results.get("comparisons", []) if "outcome" in c]
    if not comps:
        return
    n = len(comps)
    fig, axes = plt.subplots(1, n, figsize=(5 * n, 4))
    if n == 1:
        axes = [axes]
    for ax, c in zip(axes, comps):
        labs = list(c["groups"].keys())
        data = [[v for v in c["_group_values"][lab]] for lab in labs]
        try:
            ax.boxplot(data, tick_labels=labs, showfliers=False)
        except TypeError:
            ax.boxplot(data, labels=labs, showfliers=False)
        for i, g in enumerate(data):
            xs = [i + 1 + 0.04 * (j - len(g) / 2) for j in range(len(g))]
            ax.scatter(xs, g, s=6, alpha=0.4)
        ax.set_title(c["outcome"])
        ax.set_ylabel(c["outcome"])
    fig.tight_layout()
    fig.savefig(os.path.join(output_dir, "fig_boxplots.png"), dpi=150)
    plt.close(fig)

# ---------------------------------------------------------------- CLI

def load_overrides(work_dir):
    p = os.path.join(work_dir, "decisions.json")
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            return {}
    return {}

def main():
    ap = argparse.ArgumentParser(prog="infer.py")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run")
    r.add_argument("--input-dir", default="/input")
    r.add_argument("--work-dir", default="/app/work")
    r.add_argument("--output-dir", default="/app/output")
    d = sub.add_parser("decide")
    d.add_argument("--key", required=True)
    d.add_argument("--value", required=True)
    d.add_argument("--reason", default="")
    d.add_argument("--work-dir", default="/app/work")
    f = sub.add_parser("finalize")
    f.add_argument("--input-dir", default="/input")
    f.add_argument("--work-dir", default="/app/work")
    f.add_argument("--output-dir", default="/app/output")
    args = ap.parse_args()
    if args.cmd == "decide":
        ov = load_overrides(args.work_dir)
        try:
            val = json.loads(args.value)
        except json.JSONDecodeError:
            val = args.value
        ov.setdefault("decisions", []).append({"key": args.key, "value": val,
                                               "reason": args.reason,
                                               "time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        if args.key == "spec" and isinstance(val, dict):
            ov["spec"] = {**ov.get("spec", {}), **val}
        os.makedirs(args.work_dir, exist_ok=True)
        json.dump(ov, open(os.path.join(args.work_dir, "decisions.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        log(f"已记录裁决 {args.key}。运行 finalize 生效。")
        return
    if args.cmd in ("run", "finalize"):
        out_dir = args.output_dir
        os.makedirs(out_dir, exist_ok=True)
        try:
            overrides = load_overrides(args.work_dir)
            results = build_findings(args.input_dir, args.work_dir, out_dir, overrides)
            if overrides.get("decisions"):
                log(f"已应用 {len(overrides['decisions'])} 条人工裁决。")
            log(f"OK: 结果写入 {out_dir}/result.json, report.md")
        except Exception as e:
            tb = traceback.format_exc()
            log(f"ERROR: {e}")
            log(tb)
            honest = {"case_id": "unknown", "status": "failed",
                      "exceptions": [f"infer run 失败: {e}"],
                      "uncertainty": ["未能完成分析；以下为部分上下文"],
                      "traceback_tail": tb[-2000:]}
            try:
                case, _ = read_case(args.input_dir)
                if case.get("case_id"):
                    honest["case_id"] = case["case_id"]
                contract = (case.get("output_contract") or {}).get("required_fields") or []
                for fld in contract:
                    honest.setdefault(fld, {"status": "not_computed", "reason": "运行失败"})
            except Exception:
                pass
            json.dump(honest, open(os.path.join(out_dir, "result.json"), "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
            log("NEXT: 根据上方错误修复输入或用 decide 指定 spec 后重跑；result.json 已为诚实部分结果。")
            sys.exit(2)

if __name__ == "__main__":
    main()
