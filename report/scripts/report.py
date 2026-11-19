#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""report.py — Bundle 3 主入口：解释措辞 + 契约交付 + 图形选择 + 可复现性。

用法：
    python3 scripts/report.py build --input-dir /input --work-dir /app/work \
        --output-dir /app/output [--findings /app/work/findings.json] \
        [--outcome 列名] [--group 列名]

职责（downstream）：
  (a) 读取 case.json（case_id、output_contract.required_fields）；
  (b) 读取/验证 findings.json；缺失或不完整时调用 summarize 生成**仅描述性**结果，
      inferential 字段一律标记 not_computed，绝不虚构 p 值；
  (c) 措辞引擎：按 (p vs alpha, 效应量量级, Δ 是否已知) 生成克制的中文解释；
  (d) 图形选择：箱线+抖动点（默认）/ ECDF（非正态）/ 条形+95%CI（均值焦点且 n≥30）/
      斜率图（配对）/ 直方图（单组）；无 matplotlib 时降级为 figures.md 文字+表格；
  (e) 契约装配：result.json 覆盖全部 required_fields + report.md 中文报告；
  (f) 自检：调用 check.run_checks 并写入 quality_checks；FAIL 时退出码为 2。

设计约束：仅标准库；matplotlib 可选（try/except 降级）；确定性（无随机）；
任何异常仍产出诚实的部分 result.json（status=partial|failed + exceptions + NEXT 建议）。
"""

import argparse
import datetime
import hashlib
import json
import os
import platform
import sys
import traceback

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import summarize as sz  # noqa: E402
import check as ck      # noqa: E402

# ---------------------------------------------------------------- 常量

EFFECT_THRESHOLDS = {
    "d": (0.2, 0.5, 0.8), "cohen_d": (0.2, 0.5, 0.8),
    "hedges_g": (0.2, 0.5, 0.8), "g": (0.2, 0.5, 0.8),
    "eta2": (0.01, 0.06, 0.14), "eta_squared": (0.01, 0.06, 0.14),
    "partial_eta2": (0.01, 0.06, 0.14),
    "epsilon2": (0.01, 0.06, 0.14), "epsilon_squared": (0.01, 0.06, 0.14),
    "omega2": (0.01, 0.06, 0.14), "omega_squared": (0.01, 0.06, 0.14),
    "r": (0.1, 0.3, 0.5), "rho": (0.1, 0.3, 0.5),
    "rank_biserial": (0.1, 0.3, 0.5), "phi": (0.1, 0.3, 0.5),
    "cramer_v": (0.1, 0.3, 0.5),
}
MAGNITUDE_ZH = {"small": "小", "medium": "中等", "large": "大", "unknown": "未知"}

INFERENTIAL_KEYS = ("method", "statistic", "df", "p", "alpha",
                    "effect_size", "confidence_interval", "multiplicity",
                    "assumptions")


# ---------------------------------------------------------------- 工具

def norm(x):
    return sz.norm_num(x)


def fmt(x):
    return sz.fmt(x)


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=True)


def is_not_computed(v):
    return isinstance(v, dict) and v.get("status") == "not_computed"


def effect_family(name):
    low = str(name or "").lower()
    for fam in EFFECT_THRESHOLDS:
        if fam in low:
            return fam
    return None


def effect_magnitude(name, value):
    if value is None:
        return "unknown"
    fam = effect_family(name)
    if fam is None:
        return "unknown"
    s, m, l = EFFECT_THRESHOLDS[fam]
    a = abs(float(value))
    if a < s:
        return "small"
    if a < m:
        return "medium"
    return "large"


# ---------------------------------------------------------------- 数据加载

def load_case(input_dir, exceptions):
    path = os.path.join(input_dir, "case.json")
    case = read_json(path)
    case.setdefault("output_contract", {})
    case["output_contract"].setdefault("required_fields", [])
    return case


def load_findings(findings_path, work_dir, input_dir, spec, exceptions, notes):
    """优先使用显式 findings；其次 work-dir/findings.json；最后自行描述统计。"""
    candidates = [p for p in (findings_path,
                              os.path.join(work_dir, "findings.json")) if p]
    for p in candidates:
        if os.path.exists(p):
            try:
                f = read_json(p)
                notes.append("使用既有 findings: %s" % p)
                return f, p
            except Exception as exc:
                exceptions.append("findings 读取失败 %s: %s: %s"
                                  % (p, type(exc).__name__, exc))
    notes.append("未提供可用 findings，运行最小描述统计模块（仅描述性）。")
    try:
        f = sz.build_findings(input_dir, spec)
        p = os.path.join(work_dir, "findings.json")
        os.makedirs(work_dir, exist_ok=True)
        write_json(p, f)
        notes.append("最小描述统计已写入 %s" % p)
        return f, p
    except Exception as exc:
        exceptions.append("最小描述统计失败: %s: %s" % (type(exc).__name__, exc))
        return None, None


def normalize_findings(f, alpha_default):
    """统一 findings 结构；返回 (info dict)。inferential 不可用时字段为 None。"""
    info = {"alpha": alpha_default, "inferential": False}
    if not isinstance(f, dict):
        return info
    info["outcome"] = f.get("outcome")
    info["group_variable"] = f.get("group_variable")
    info["group_summaries"] = f.get("group_summaries") or \
        (f.get("descriptive") or {}).get("groups")
    info["descriptive"] = f.get("descriptive")
    info["cleaning"] = f.get("cleaning")
    info["paired"] = f.get("paired") if isinstance(f.get("paired"), bool) else False
    info["assumptions"] = f.get("assumptions") if isinstance(f.get("assumptions"), dict) else {}
    info["limitations"] = f.get("limitations") if isinstance(f.get("limitations"), list) else []
    if isinstance(f.get("alpha"), (int, float)) and not isinstance(f.get("alpha"), bool):
        info["alpha"] = float(f["alpha"])
    method = f.get("method")
    p = f.get("p")
    if isinstance(method, str) and isinstance(p, (int, float)) \
            and not isinstance(p, bool):
        info["inferential"] = True
        info["method"] = method
        info["statistic"] = norm(f.get("statistic")) \
            if isinstance(f.get("statistic"), (int, float)) else f.get("statistic")
        info["df"] = f.get("df")
        info["p"] = norm(p)
    es = f.get("effect_size")
    if isinstance(es, dict) and not is_not_computed(es) \
            and isinstance(es.get("value"), (int, float)):
        info["effect_size"] = {"name": es.get("name", "unknown"),
                               "value": norm(es["value"]),
                               "ci95": [norm(x) for x in es["ci95"]]
                               if isinstance(es.get("ci95"), (list, tuple))
                               and len(es["ci95"]) == 2 else None}
    mult = f.get("multiplicity")
    if isinstance(mult, dict) and not is_not_computed(mult) and mult.get("method"):
        info["multiplicity"] = {
            "method": mult["method"],
            "adjusted_p": norm(mult["adjusted_p"])
            if isinstance(mult.get("adjusted_p"), (int, float)) else None}
    d = f.get("practically_important_difference")
    if isinstance(d, (int, float)) and not isinstance(d, bool):
        info["delta"] = norm(d)
    return info


# ---------------------------------------------------------------- 措辞引擎

def build_interpretation(info):
    """按决策表生成克制的中文解释。禁用词：证明／说明有效／显著所以重要／相关即因果。"""
    outcome = info.get("outcome") or "结果变量"
    gvar = info.get("group_variable") or "分组变量"
    alpha = info.get("alpha", 0.05)
    caveats = []
    gs = info.get("group_summaries") or {}
    ns = [g.get("n") for g in gs.values() if isinstance(g, dict) and g.get("n")]
    min_n = min(ns) if ns else None
    if min_n is not None and min_n < 30:
        caveats.append("部分组样本量较小（最小组 n=%s），检验效能与估计稳定性有限。"
                       % fmt(min_n))

    if not info.get("inferential"):
        return {
            "claim_strength": "not_applicable",
            "significant": None,
            "significance_zh":
                "本组件未执行统计检验：推断性字段（方法、统计量、p 值、效应量）"
                "标记为 not_computed，需要由检验阶段提供。",
            "practical_zh":
                "在获得检验结果与领域阈值 Δ 之前，不宜对实际意义作出判断。",
            "ci_zh": "置信区间未计算。",
            "conclusion_zh":
                "当前仅能给出描述性结论；任何关于组间差异的推断性表述均待检验阶段补充。",
            "caveats_zh": caveats,
        }

    p = info["p"]
    sig = p < alpha
    es = info.get("effect_size") or {}
    es_name = es.get("name", "效应量")
    es_val = es.get("value")
    mag = effect_magnitude(es_name, es_val)
    mult = info.get("multiplicity")

    if sig:
        base = ("在 α=%s 水平下，%s各组间%s的差异具有统计学意义"
                "（%s，p=%s）。" % (fmt(alpha), gvar, outcome,
                                   info.get("method") or "检验", fmt(p)))
    else:
        base = ("在 α=%s 水平下，%s各组间%s的差异不具有统计学意义"
                "（%s，p=%s）；证据不足以支持「%s随%s不同而存在差异」的结论。"
                % (fmt(alpha), gvar, outcome, info.get("method") or "检验",
                   fmt(p), outcome, gvar))

    if mult:
        adj = mult.get("adjusted_p")
        if isinstance(adj, float):
            if adj < alpha:
                base += ("经%s校正后，调整后 p=%s，结论保持不变。"
                         % (mult["method"], fmt(adj)))
            else:
                base += ("经%s校正后，调整后 p=%s，差异不再具有统计学意义，"
                         "未校正结果不应单独作为结论依据。"
                         % (mult["method"], fmt(adj)))
                sig = False

    if sig and es_val is not None:
        mag_zh = MAGNITUDE_ZH[mag]
        if mag == "large":
            strength, tone = "strong", "表明"
            effect_zh = ("效应量 %s=%s，按常用阈值属于大效应，%s该差异可能具有实际意义；"
                         "其实际重要性仍需结合领域阈值判断。"
                         % (es_name, fmt(es_val), tone))
        elif mag == "medium":
            strength, tone = "moderate", "提示"
            effect_zh = ("效应量 %s=%s，按常用阈值属于中等效应，%s该差异可能具有实际意义；"
                         "是否达到实际重要需结合领域阈值 Δ 判断。"
                         % (es_name, fmt(es_val), tone))
        else:
            strength = "moderate"
            effect_zh = ("效应量 %s=%s，按常用阈值属于小效应；统计显著并不等同于实际重要，"
                         "该差异的实际意义有限。" % (es_name, fmt(es_val)))
    elif not sig:
        strength = "tentative"
        if min_n is not None and min_n < 30:
            effect_zh = ("阴性结果可能受样本量限制；不宜将「不具有统计学意义」"
                         "解读为「不存在差异」。")
        else:
            effect_zh = ("当前证据不支持组间存在差异；该结论以所用检验的假设为前提。")
    else:
        strength = "tentative"
        effect_zh = "效应量未提供，无法评估差异量级。"

    if isinstance(info.get("delta"), float):
        delta = info["delta"]
        if es_val is not None and abs(float(es_val)) >= abs(delta):
            practical_zh = ("相对于实际重要差异阈值 Δ=%s，观察到的效应达到或超过该阈值，"
                            "提示具有实际意义。" % fmt(delta))
        else:
            practical_zh = ("相对于实际重要差异阈值 Δ=%s，观察到的效应未达到该阈值，"
                            "实际意义有限。" % fmt(delta))
    else:
        practical_zh = ("任务未给出实际重要差异 Δ；统计显著性与实际意义必须分开表述，"
                        "实际意义需结合领域标准另行判断。")

    ci = es.get("ci95")
    if ci:
        ci_zh = ("%s的 95%% 置信区间为 [%s, %s]；该区间刻画估计量的不确定性，"
                 "并非个体取值的范围。" % (es_name, fmt(ci[0]), fmt(ci[1])))
    else:
        ci_zh = "效应量置信区间未提供，估计不确定性无法量化。"

    seen_causal = any("因果" in str(x) for x in (info.get("limitations") or []))
    for lim in info.get("limitations") or []:
        if isinstance(lim, str):
            caveats.append(lim)
    if (info.get("assumptions") or {}).get("normality") in (
            False, "non_normal", "violated", "非正态"):
        caveats.append("上游提示正态性假设不满足；结论依赖秩方法或对分布稳健的检验。")
    if not mult and isinstance(info.get("group_summaries"), dict) \
            and len(info["group_summaries"]) > 2:
        caveats.append("本分析涉及多组比较，若未进行多重比较校正，I 类错误率将膨胀。")
    if not seen_causal:
        caveats.append("数据为观察性来源，组间差异不能推断因果。")

    conclusion = base + effect_zh
    return {
        "claim_strength": strength,
        "significant": sig,
        "effect_magnitude": mag,
        "significance_zh": base,
        "practical_zh": practical_zh,
        "ci_zh": ci_zh,
        "conclusion_zh": conclusion,
        "caveats_zh": caveats,
    }


# ---------------------------------------------------------------- 图形

def select_figure_plan(info):
    """返回图形计划 dict。规则见 references/figure-selection.md。"""
    gs = info.get("group_summaries") or {}
    n_groups = len(gs)
    ns = [g.get("n") for g in gs.values() if isinstance(g, dict) and g.get("n")]
    min_n = min(ns) if ns else 0
    max_n = max(ns) if ns else 0
    non_normal = (info.get("assumptions") or {}).get("normality") in (
        False, "non_normal", "violated", "非正态") \
        or (info.get("assumptions") or {}).get("skew") in (True, "skewed", "偏态")
    if info.get("paired") and n_groups >= 2:
        return {"type": "slope", "reason": "配对设计，采用斜率图呈现组内变化。"}
    if n_groups == 1:
        return {"type": "histogram", "reason": "单组分布问题，采用直方图。"}
    if non_normal:
        return {"type": "ecdf", "reason": "上游提示非正态/偏态，采用 ECDF 叠加图，"
                                          "避免箱线图掩盖分布形态。"}
    focus_means = (info.get("assumptions") or {}).get("focus") == "means"
    if focus_means and min_n >= 30:
        return {"type": "bar_ci", "reason": "任务焦点为均值且每组 n≥30，"
                                            "采用条形图加 95% 置信区间。"}
    jitter = max_n <= 500
    return {"type": "boxplot", "jitter": jitter,
            "reason": "默认采用分组箱线图%s。" % ("叠加抖动散点" if jitter else
                                                "（组样本量超过 500，省略散点）")}


def _mpl():
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        return plt
    except Exception:
        return None


def _cjk_font(plt):
    try:
        from matplotlib import font_manager
        for name in ("PingFang SC", "Hiragino Sans GB", "STHeiti",
                     "Microsoft YaHei", "Noto Sans CJK SC", "SimHei",
                     "WenQuanYi Zen Hei", "Arial Unicode MS"):
            try:
                font_manager.findfont(font_manager.FontProperties(family=name),
                                      fallback_to_default=False)
                plt.rcParams["font.sans-serif"] = [name]
                plt.rcParams["axes.unicode_minus"] = False
                return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def render_figures(plan, ctx_groups, outcome, gvar, output_dir, notes):
    """尝试渲染；成功返回文件名列表，失败返回 None。确定性：无随机抖动。"""
    plt = _mpl()
    if plt is None:
        notes.append("matplotlib 不可用，图形降级为 figures.md 中的文字说明与数值表。")
        return None
    zh = _cjk_font(plt)
    labels = list(ctx_groups.keys())
    values = ctx_groups  # {label: [floats]}
    ylabel = ("%s" % outcome) if zh else str(outcome)
    xlabel = ("%s" % gvar) if zh else str(gvar)
    figs = []

    def save(fig, name):
        path = os.path.join(output_dir, name)
        fig.savefig(path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        figs.append(name)

    t = plan["type"]
    try:
        if t == "boxplot":
            fig, ax = plt.subplots(figsize=(7, 5))
            data = [values[k] for k in labels]
            try:
                ax.boxplot(data, tick_labels=[str(k) for k in labels],
                           showfliers=False)
            except TypeError:  # matplotlib < 3.9
                ax.boxplot(data, labels=[str(k) for k in labels],
                           showfliers=False)
            if plan.get("jitter"):
                for i, k in enumerate(labels):
                    vs = values[k]
                    n = len(vs)
                    if n:
                        offs = [(-0.08 + 0.16 * (j / max(n - 1, 1)))
                                for j in range(n)]
                        ax.scatter([i + 1 + o for o in offs], vs,
                                   s=8, alpha=0.5, color="tab:blue")
            ax.set_xlabel(xlabel)
            ax.set_ylabel(ylabel)
            ax.set_title("分组箱线图" if zh else "Grouped boxplot")
            save(fig, "fig1.png")
        elif t == "ecdf":
            fig, ax = plt.subplots(figsize=(7, 5))
            for k in labels:
                vs = sorted(values[k])
                n = len(vs)
                if n:
                    ys = [(i + 1) / n for i in range(n)]
                    ax.step(vs, ys, where="post", label=str(k))
            ax.set_xlabel(ylabel)
            ax.set_ylabel("ECDF")
            ax.legend(fontsize=8)
            ax.set_title("经验累积分布函数" if zh else "ECDF overlay")
            save(fig, "fig1.png")
        elif t == "bar_ci":
            fig, ax = plt.subplots(figsize=(7, 5))
            means, errs, xs = [], [], []
            for k in labels:
                vs = values[k]
                n = len(vs)
                m = sum(vs) / n if n else float("nan")
                sd = (sum((v - m) ** 2 for v in vs) / (n - 1)) ** 0.5 if n > 1 else 0.0
                means.append(m)
                errs.append(1.96 * sd / (n ** 0.5) if n else 0.0)
                xs.append(str(k))
            ax.bar(xs, means, yerr=errs, capsize=4, color="tab:blue", alpha=0.7)
            ax.set_xlabel(xlabel)
            ax.set_ylabel(ylabel)
            ax.set_title("均值与 95% 置信区间" if zh else "Means with 95% CI")
            save(fig, "fig1.png")
            # fig2：分布背景
            fig, ax = plt.subplots(figsize=(7, 5))
            try:
                ax.boxplot([values[k] for k in labels],
                           tick_labels=[str(k) for k in labels], showfliers=False)
            except TypeError:  # matplotlib < 3.9
                ax.boxplot([values[k] for k in labels],
                           labels=[str(k) for k in labels], showfliers=False)
            ax.set_xlabel(xlabel)
            ax.set_ylabel(ylabel)
            ax.set_title("分布背景（箱线图）" if zh else "Distribution context")
            save(fig, "fig2.png")
        elif t == "slope":
            fig, ax = plt.subplots(figsize=(7, 5))
            means = []
            for k in labels:
                vs = values[k]
                means.append(sum(vs) / len(vs) if vs else float("nan"))
            ax.plot(range(len(labels)), means, marker="o")
            ax.set_xticks(range(len(labels)))
            ax.set_xticklabels([str(k) for k in labels])
            ax.set_ylabel(ylabel)
            ax.set_title("配对斜率图（组均值连线）" if zh else "Slope graph")
            save(fig, "fig1.png")
        elif t == "histogram":
            fig, ax = plt.subplots(figsize=(7, 5))
            allv = [v for k in labels for v in values[k]]
            ax.hist(allv, bins=20, color="tab:blue", alpha=0.7)
            ax.set_xlabel(ylabel)
            ax.set_ylabel("频数" if zh else "Count")
            ax.set_title("分布直方图" if zh else "Histogram")
            save(fig, "fig1.png")
        else:
            return None
    except Exception as exc:
        notes.append("图形渲染失败，降级为文字说明: %s: %s"
                     % (type(exc).__name__, exc))
        return None
    return figs


def ascii_fallback_table(info):
    gs = info.get("group_summaries") or {}
    lines = ["| 组 | n | mean | sd | median | q1 | q3 | missing |",
             "|---|---|---|---|---|---|---|---|"]
    for k, g in gs.items():
        lines.append("| %s | %s | %s | %s | %s | %s | %s | %s |" % (
            k, g.get("n"), fmt(g.get("mean")), fmt(g.get("sd")),
            fmt(g.get("median")), fmt(g.get("q1")), fmt(g.get("q3")),
            g.get("missing", 0)))
    return "\n".join(lines)


def write_figures_md(path, plan, figs, info):
    gs = info.get("group_summaries") or {}
    ns = "，".join("%s（n=%s）" % (k, g.get("n")) for k, g in gs.items())
    lines = ["# 图形说明", "",
             "图一选择：%s。选择理由：%s" % (plan["type"], plan["reason"]), ""]
    if figs:
        lines.append("已生成：%s。" % "、".join(figs))
    else:
        lines.append("matplotlib 不可用，未生成位图；下表给出图形对应的底层数值，"
                     "可按上述图形类型自行绘制。")
    lines += ["", "各组样本量：%s。" % ns, "", ascii_fallback_table(info), ""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ---------------------------------------------------------------- 报告

def write_report_md(path, case, info, interp, plan, figs, status):
    cid = case.get("case_id")
    lines = ["# 统计分析报告：%s" % cid, "",
             "本报告由 verifiable-report 组件自动生成，措辞遵循校准语言规范。", ""]
    lines += ["## 一、数据与口径", ""]
    cleaning = info.get("cleaning") or {}
    if cleaning:
        lines.append("数据源：%s；原始 %s 行，去除完全重复 %s 行后纳入 %s 行。"
                     % (cleaning.get("primary_source"),
                        cleaning.get("n_rows_raw"),
                        cleaning.get("n_duplicates_removed"),
                        cleaning.get("n_rows_used")))
        miss = cleaning.get("missing_by_column") or {}
        if miss:
            lines.append("缺失计数（按列）：%s。"
                         % "、".join("%s=%s" % kv for kv in miss.items()))
        else:
            lines.append("未发现缺失值。")
    ngroups = len(info.get("group_summaries") or {})
    if ngroups:
        lines.append("结果变量：%s；分组变量：%s（共 %s 组）。"
                     % (info.get("outcome"), info.get("group_variable"),
                        ngroups))
    lines.append("单位与口径以原始数据为准，如有疑义需人工确认。")
    lines += ["", "## 二、描述统计", "", ascii_fallback_table(info), ""]
    ov = (info.get("descriptive") or {}).get("overall") or {}
    if ov:
        lines.append("总体：n=%s，mean=%s，sd=%s，median=%s。"
                     % (ov.get("n_valid", ov.get("n")), fmt(ov.get("mean")),
                        fmt(ov.get("sd")), fmt(ov.get("median"))))
    lines += ["", "## 三、检验方法与假设", ""]
    if info.get("inferential"):
        df = info.get("df")
        df_txt = ("[%s]" % ", ".join(fmt(x) for x in df)
                  if isinstance(df, (list, tuple)) else fmt(df))
        lines.append("方法：%s；统计量=%s；df=%s；p=%s；α=%s。"
                     % (info.get("method"), fmt(info.get("statistic")),
                        df_txt, fmt(info.get("p")), fmt(info.get("alpha"))))
        if info.get("multiplicity"):
            m = info["multiplicity"]
            lines.append("多重比较校正：%s；调整后 p=%s。"
                         % (m["method"], fmt(m.get("adjusted_p"))))
    else:
        lines.append("统计检验未执行：推断性字段标记 not_computed，"
                     "需要由检验阶段提供方法、统计量、自由度、p 值与效应量。")
    lines += ["", "## 四、效应量与置信区间", ""]
    es = info.get("effect_size")
    if es:
        lines.append("效应量 %s=%s。" % (es["name"], fmt(es["value"])))
    lines.append(interp["ci_zh"])
    lines += ["", "## 五、结果解释", "",
              interp["conclusion_zh"], "",
              interp["practical_zh"], "",
              "结论强度标签：%s。" % interp["claim_strength"], ""]
    lines += ["## 六、局限与待确认", ""]
    for c in interp.get("caveats_zh") or []:
        lines.append("- %s" % c)
    lines += ["", "## 附：图形", "",
              "图一：%s（%s）。" % (plan["type"], plan["reason"])]
    if figs:
        lines.append("文件：%s；图注与样本量见 figures.md。" % "、".join(figs))
    else:
        lines.append("位图未生成，意图与底层数值见 figures.md。")
    lines += ["", "## 附：可复现性", "",
              "输入哈希、运行环境与脚本哈希见 result.json 的 reproducibility 字段。", ""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ---------------------------------------------------------------- 契约装配

def build_contract(case, info, interp, exceptions, reproducibility):
    required = (case.get("output_contract") or {}).get("required_fields") or []
    not_computed = {"status": "not_computed",
                    "reason": "需要由检验阶段提供，本组件不虚构"}
    ht = not_computed if not info.get("inferential") else {
        "method": info.get("method"),
        "statistic": info.get("statistic"),
        "df": info.get("df"),
        "p": info.get("p"),
        "alpha": info.get("alpha"),
        "significant": bool(info["p"] < info["alpha"]),
        **({"multiplicity": info["multiplicity"]} if info.get("multiplicity") else {}),
    }
    es = info.get("effect_size")
    effect_size = not_computed if not es else dict(es)
    ci = not_computed if not (es and es.get("ci95")) else {
        "parameter": es["name"], "level": 0.95, "ci95": es["ci95"],
        "note": "区间刻画估计量不确定性，并非个体取值范围",
    }
    desc = {"outcome": info.get("outcome"),
            "group_variable": info.get("group_variable"),
            "n_groups": len(info.get("group_summaries") or {}),
            "groups": info.get("group_summaries") or {},
            "overall": (info.get("descriptive") or {}).get("overall")}
    limitations = interp.get("caveats_zh") or []
    registry = {
        "case_id": case.get("case_id"),
        "status": None,  # 稍后
        "cleaning": info.get("cleaning") or
        {"status": "not_computed", "reason": "清洗信息未提供"},
        "descriptive": desc,
        "hypothesis_test": ht,
        "effect_size": effect_size,
        "confidence_interval": ci,
        "interpretation": interp,
        "limitations": limitations,
        "sources": None,
        "uncertainty": None,
        "reproducibility": reproducibility,
        "exceptions": exceptions,
        "quality_checks": None,
    }
    sources = []
    if case.get("dataset"):
        sources.append({"dataset": case["dataset"],
                        "citation": case.get("source")})
    for fentry in (info.get("cleaning") or {}).get("source_files") or []:
        sources.append({"file": fentry.get("file"), "sha256": fentry.get("sha256")})
    registry["sources"] = sources
    uncertainty = {
        "needs_human_confirmation": [
            "单位与测量口径以原始数据为准，建议人工确认。",
            "实际重要差异阈值 Δ 未给出时，实际意义判断需领域知识。",
        ],
        "ci_misread_warning": "置信区间是估计量的不确定性区间，不是个体取值范围。",
        "claim_strength": interp.get("claim_strength"),
    }
    registry["uncertainty"] = uncertainty

    result = {"output_contract": case.get("output_contract")}
    for field in required:
        if field in registry and registry[field] is not None:
            result[field] = registry[field]
        elif field == "status":
            continue
        else:
            result[field] = {"status": "not_computed",
                             "reason": "该字段超出本组件职责范围，"
                                       "需由检验引擎或清洗审计组件提供"}
    # 状态
    if exceptions and result.get("case_id") is None:
        status = "failed"
    elif not info.get("inferential") or exceptions:
        status = "partial"
    else:
        status = "complete"
    result["status"] = status
    result.setdefault("case_id", case.get("case_id"))
    # 附加字段（契约之外，诚实补充）
    result["sources"] = sources
    result["uncertainty"] = uncertainty
    result["reproducibility"] = reproducibility
    result["exceptions"] = list(exceptions)
    result["interpretation"] = result.get("interpretation", interp)
    return result, status


# ---------------------------------------------------------------- 主流程

def build(args):
    exceptions, notes = [], []
    output_dir = args.output_dir
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(args.work_dir, exist_ok=True)
    result_path = os.path.join(output_dir, "result.json")
    try:
        case = load_case(args.input_dir, exceptions)
    except Exception as exc:
        exceptions.append("case.json 读取失败: %s: %s" % (type(exc).__name__, exc))
        case = {"case_id": None, "output_contract": {"required_fields": []}}

    spec = {}
    if args.outcome:
        spec["outcome"] = args.outcome
    if args.group:
        spec["group_variable"] = args.group

    findings, findings_path = load_findings(
        args.findings, args.work_dir, args.input_dir, spec, exceptions, notes)
    alpha_default = ((case.get("constraints") or {}).get("alpha")) or 0.05
    info = normalize_findings(findings, float(alpha_default)) if findings else \
        {"alpha": float(alpha_default), "inferential": False,
         "group_summaries": {}, "limitations": []}

    # 若 findings 缺分组统计但数据可用，补齐描述性部分
    if not info.get("group_summaries") and findings is not None \
            and not findings.get("group_summaries"):
        try:
            auto = sz.build_findings(args.input_dir, spec)
            info2 = normalize_findings(auto, float(alpha_default))
            for k in ("group_summaries", "descriptive", "cleaning",
                      "outcome", "group_variable"):
                if not info.get(k) and info2.get(k):
                    info[k] = info2[k]
            notes.append("findings 缺少分组统计，已由最小描述统计模块补齐描述性部分。")
        except Exception as exc:
            exceptions.append("描述性补齐失败: %s: %s" % (type(exc).__name__, exc))

    interp = build_interpretation(info)

    # 图形：需要原始分组取值；从数据重建（确定性）
    plan = select_figure_plan(info)
    figs = None
    try:
        ctx = sz.load_table(args.input_dir, spec)
        cols = ctx["columns"]
        oc = info.get("outcome") or ctx["outcome"]
        gc = info.get("group_variable") or ctx["group"]
        if oc in cols and gc in cols:
            ovals, gvals = cols[oc]["values"], cols[gc]["raw"]
            groups = {}
            for i in range(cols[oc]["n_rows"]):
                key = (gvals[i] or "").strip() or "NA"
                v = ovals[i]
                if v is not None:
                    groups.setdefault(key, []).append(v)
            order = list((info.get("group_summaries") or {}).keys()) or sorted(groups)
            ordered = {k: groups[k] for k in order if k in groups}
            figs = render_figures(plan, ordered, oc, gc, output_dir, notes)
    except Exception as exc:
        notes.append("图形数据准备失败: %s: %s" % (type(exc).__name__, exc))
    write_figures_md(os.path.join(output_dir, "figures.md"), plan, figs, info)

    # 可复现性
    try:
        input_hashes = []
        for p in sorted(sz.discover_files(args.input_dir)[0] +
                        sz.discover_files(args.input_dir)[1] +
                        [os.path.join(args.input_dir, "case.json"),
                         os.path.join(args.input_dir, "task.md")]):
            if os.path.exists(p):
                input_hashes.append(sz.sha256_file(p))
        combined = hashlib.sha256("".join(input_hashes).encode()).hexdigest()
        scripts_hash = {os.path.basename(p): sz.sha256_file(p)
                        for p in (os.path.join(SCRIPTS_DIR, s) for s in
                                  ("report.py", "summarize.py", "check.py"))}
    except Exception as exc:
        combined, scripts_hash = None, {}
        notes.append("哈希计算部分失败: %s" % exc)
    reproducibility = {
        "input_sha256": combined,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "script_sha256": scripts_hash,
        "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "randomness": "无（完全确定性流程）",
    }

    result, status = build_contract(case, info, interp, exceptions, reproducibility)
    result["figures"] = {"plan": plan, "files": figs or [],
                         "note": "figures.md 含图注与降级表格"}

    write_report_md(os.path.join(output_dir, "report.md"),
                    case, info, interp, plan, figs, status)
    write_json(result_path, result)

    # 自检并回写 quality_checks
    qc, _ = ck.run_checks(output_dir)
    result["quality_checks"] = qc
    write_json(result_path, result)

    # 摘要输出（供 agent 阅读）
    print("===== DIGEST =====")
    print("case_id: %s" % case.get("case_id"))
    print("status: %s" % status)
    print("findings: %s" % (findings_path or "无"))
    print("inferential: %s" % info.get("inferential"))
    print("figure_plan: %s (%s)" % (plan["type"], plan["reason"]))
    print("figures: %s" % (figs or "无（见 figures.md 降级说明）"))
    print("claim_strength: %s" % interp.get("claim_strength"))
    print("conclusion: %s" % interp.get("conclusion_zh"))
    for n in notes:
        print("note: %s" % n)
    for r in qc:
        print("%-4s [%s] %s" % (r["result"], r["name"], r["detail"]))
    hard_fail = any(r["result"] == "FAIL" for r in qc)
    if hard_fail:
        print("NEXT: 存在 FAIL 自检项，修复后重新运行本命令再交付。")
        return 2
    if exceptions:
        print("NEXT: 存在异常但已产出部分结果；请审阅 exceptions 与 limitations。")
        return 0
    print("NEXT: 结果就绪；请人工确认措辞与实际意义判断后交付。")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="verifiable-report 主入口")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--input-dir", required=True)
    b.add_argument("--work-dir", required=True)
    b.add_argument("--output-dir", required=True)
    b.add_argument("--findings", default=None)
    b.add_argument("--outcome", default=None)
    b.add_argument("--group", default=None)
    args = ap.parse_args(argv)
    try:
        return build(args)
    except Exception:
        # 最后一道防线：任何未预期异常也产出诚实的部分 result.json
        tb = traceback.format_exc()
        os.makedirs(args.output_dir, exist_ok=True)
        fallback = {
            "case_id": None,
            "status": "failed",
            "exceptions": ["未预期异常:\n%s" % tb],
            "output_contract": {"required_fields": []},
        }
        try:
            casep = os.path.join(args.input_dir, "case.json")
            if os.path.exists(casep):
                cj = read_json(casep)
                fallback["case_id"] = cj.get("case_id")
                fallback["output_contract"] = cj.get(
                    "output_contract", {"required_fields": []})
                for k in fallback["output_contract"].get("required_fields", []):
                    fallback.setdefault(k, {"status": "not_computed",
                                            "reason": "构建失败"})
        except Exception:
            pass
        write_json(os.path.join(args.output_dir, "result.json"), fallback)
        print("[report] 构建失败，已写入诚实的部分 result.json。")
        print("NEXT: 查看 result.json 的 exceptions 字段定位问题后重试。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
