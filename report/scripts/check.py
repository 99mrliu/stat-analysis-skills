#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check.py — 结果自洽性校验器（仅标准库）。

独立调用：
    python3 scripts/check.py --output-dir /app/output

校验项：
  1. json_valid           result.json 为合法 JSON 且含 case_id
  2. required_fields      result.json 含 output_contract.required_fields 全部字段
  3. report_numbers_match report.md 中每个数字都能在 result.json 中找到
                          （1e-6 相对容差；% 后缀按 /100 处理；不通过则 FAIL）
  4. ci_contains_estimate 置信区间包含点估计（warn 不 FAIL）
  5. p_alpha_wording      p<alpha 与结论措辞一致（不具有/具有统计学意义）
  6. status_consistent    status 与 exceptions 一致（failed/partial ↔ exceptions）
"""

import argparse
import json
import math
import os
import re
import sys

NUM_RE = re.compile(r"-?[0-9]+(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?")
TOL = 1e-6


def flatten_numbers(obj, into=None, key_numeric=False):
    """收集 result.json 中的全部数值：JSON 数值 + 可整体解析为数值的字符串
    （如分组键 "28"、 cultivar "1"）。普通文本与 sha256 不会被误收。"""
    if into is None:
        into = []
    if isinstance(obj, bool):
        return into
    if isinstance(obj, (int, float)):
        into.append(float(obj))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str):
                m = NUM_RE.fullmatch(k.strip())
                if m:
                    into.append(float(m.group(0)))
            flatten_numbers(v, into)
    elif isinstance(obj, list):
        for v in obj:
            flatten_numbers(v, into)
    elif isinstance(obj, str):
        s = obj.strip()
        m = NUM_RE.fullmatch(s)
        if m and not re.search(r"[A-DF-Za-df-z]", s.replace("e", "").replace("E", "")):
            into.append(float(m.group(0)))
    return into


def extract_report_numbers(report_text):
    """提取 report.md 中的数字 token；紧邻 % 的按百分数处理（值/100）。
    全角数字不归一化提取（正则仅匹配半角）。目录式编号请用中文数字避免误判。"""
    out = []
    for m in NUM_RE.finditer(report_text):
        # 跳过标识符/文件名中的数字（col5、fig1.png 等）
        prev = report_text[m.start() - 1:m.start()]
        if prev and (prev.isascii() and (prev.isalpha() or prev == "_")):
            continue
        val = float(m.group(0))
        tail = report_text[m.end():m.end() + 1]
        if tail in ("%", "％"):
            val = val / 100.0
        out.append(val)
    return out


def close(a, b, tol=TOL):
    if a == b:
        return True
    denom = max(abs(a), abs(b), 1e-12)
    return abs(a - b) / denom <= tol


def find_ci_objects(obj, path=""):
    """递归找出疑似 {point/estimate, ci95:[lo,hi]} 结构。"""
    hits = []
    if isinstance(obj, dict):
        keys = set(obj.keys())
        pt = None
        for k in ("estimate", "point_estimate", "value", "mean"):
            if isinstance(obj.get(k), (int, float)) and not isinstance(obj.get(k), bool):
                pt = float(obj[k])
                break
        ci = obj.get("ci95") or obj.get("ci")
        if pt is not None and isinstance(ci, (list, tuple)) and len(ci) == 2 \
                and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in ci):
            hits.append((path, pt, float(ci[0]), float(ci[1])))
        for k, v in obj.items():
            hits.extend(find_ci_objects(v, path + "/" + str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hits.extend(find_ci_objects(v, "%s[%d]" % (path, i)))
    return hits


def run_checks(output_dir):
    results = []
    rj_path = os.path.join(output_dir, "result.json")
    rm_path = os.path.join(output_dir, "report.md")

    def rec(name, ok, level="check", detail=""):
        results.append({"name": name, "level": level,
                        "result": "PASS" if ok else ("WARN" if level == "warn" else "FAIL"),
                        "detail": detail})

    # 1. JSON 合法
    data = None
    try:
        with open(rj_path, encoding="utf-8") as f:
            data = json.load(f)
        rec("json_valid", True, detail="result.json 解析成功")
    except Exception as exc:
        rec("json_valid", False, detail="%s: %s" % (type(exc).__name__, exc))
        return results, None

    # 2. 必填字段
    case_id = data.get("case_id")
    rec("has_case_id", bool(case_id), detail="case_id=%s" % case_id)
    required = (data.get("output_contract") or {}).get("required_fields") or []
    missing = [k for k in required if k not in data]
    rec("required_fields", not missing,
        detail="缺失字段: %s" % missing if missing else "全部 %d 个必填字段齐全" % len(required))

    # 3. 报告数字与 JSON 一致
    report_text = ""
    try:
        with open(rm_path, encoding="utf-8") as f:
            report_text = f.read()
    except OSError as exc:
        rec("report_numbers_match", False, detail="无法读取 report.md: %s" % exc)
    if report_text:
        pool = flatten_numbers(data)
        bad = []
        for v in extract_report_numbers(report_text):
            if not any(close(v, p) for p in pool):
                bad.append(v)
        # 去重控制输出规模
        bad = sorted(set(bad))
        rec("report_numbers_match", not bad,
            detail=("不一致数字: %s" % bad[:20]) if bad else "报告数字均在 result.json 中（容差 1e-6）")

    # 4. CI 包含点估计（warn）
    ci_hits = find_ci_objects(data)
    ci_bad = [(p, pt, lo, hi) for p, pt, lo, hi in ci_hits
              if not (min(lo, hi) <= pt <= max(lo, hi))]
    rec("ci_contains_estimate", not ci_bad, level="warn",
        detail=("点估计落在 CI 外: %s" % ci_bad[:5]) if ci_bad
               else "%d 个置信区间均包含点估计" % len(ci_hits))

    # 5. p<alpha 与措辞一致
    ht = data.get("hypothesis_test") or {}
    interp = json.dumps(data.get("interpretation") or {}, ensure_ascii=False)
    if isinstance(ht, dict) and all(k in ht for k in ("p", "alpha")) \
            and isinstance(ht["p"], (int, float)):
        sig = ht["p"] < ht["alpha"]
        expect = "具有统计学意义" if sig else "不具有统计学意义"
        rec("p_alpha_wording", expect in interp,
            detail="p=%s alpha=%s → 期望措辞含『%s』" % (ht["p"], ht["alpha"], expect))
    else:
        rec("p_alpha_wording", True, detail="无检验结果，跳过")

    # 6. status 与 exceptions / not_computed 一致
    status = data.get("status")
    excs = data.get("exceptions") or []

    def has_not_computed(obj):
        if isinstance(obj, dict):
            if obj.get("status") == "not_computed":
                return True
            return any(has_not_computed(v) for v in obj.values())
        if isinstance(obj, list):
            return any(has_not_computed(v) for v in obj)
        return False

    nc = has_not_computed({k: v for k, v in data.items()
                           if k not in ("quality_checks",)})
    ok = (status == "failed" and len(excs) > 0) or \
         (status == "partial" and (len(excs) > 0 or nc)) or \
         (status in ("complete", "ok", "success") and len(excs) == 0 and not nc)
    rec("status_consistent", ok,
        detail="status=%s exceptions=%d not_computed=%s" % (status, len(excs), nc))
    return results, data


def main(argv=None):
    ap = argparse.ArgumentParser(description="结果自洽性校验器")
    ap.add_argument("--output-dir", required=True)
    args = ap.parse_args(argv)
    results, _ = run_checks(args.output_dir)
    for r in results:
        print("%-4s [%s] %s" % (r["result"], r["name"], r["detail"]))
    hard_fail = any(r["result"] == "FAIL" for r in results)
    if hard_fail:
        print("结论: 存在 FAIL，交付前必须修复（见各检查项 detail）。")
        return 1
    print("结论: 无 FAIL" + ("（含 WARN，建议人工复核）" if
          any(r["result"] == "WARN" for r in results) else "，全部通过。"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
