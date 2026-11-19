#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""summarize.py — 最小描述统计模块（仅 Python 标准库）。

职责边界：本模块只做“数据定位 + 类型识别 + 分组描述统计 + 最小清洗计数”。
它**不**执行任何假设检验、不计算 p 值、不计算效应量。
inferential 字段一律以 {"status": "not_computed", "note": "需要由检验阶段提供"} 占位，
绝不虚构。

可独立调用：
    python3 scripts/summarize.py --input-dir /input --work-dir /app/work \
        [--outcome strength] [--group age]

亦可被 report.py 作为模块导入（discover_table / infer_columns / summarize_groups）。
"""

import argparse
import csv
import hashlib
import json
import os
import sqlite3
import statistics
import sys

NA_TOKENS = {"", "na", "n/a", "null", "none", "nan", "?", "-", "--"}
CSV_EXTS = (".csv", ".tsv")
DB_EXTS = (".db", ".sqlite", ".sqlite3")
ENCODINGS = ("utf-8-sig", "gbk", "latin-1")
# 常见结果变量命名线索（大小写不敏感）
OUTCOME_HINTS = ("strength", "outcome", "target", "response", "score",
                 "value", "result", "measure", "y")
GROUP_HINTS = ("class", "label", "group", "category", "species", "cultivar",
               "type", "age", "sex", "gender", "treatment", "dose")


def norm_num(x):
    """归一化浮点数：所有进入 result.json / report.md 的数字都经过此函数，
    保证两边文本完全一致（8 位有效数字），供 check.py 做 1e-6 相对容差比对。"""
    if x is None:
        return None
    try:
        return float("%.8g" % float(x))
    except (TypeError, ValueError):
        return x


def fmt(x):
    """数字的规范文本形式（与 norm_num 对应）。"""
    if x is None:
        return "NA"
    return "%.8g" % float(x)


def to_float(s):
    if s is None:
        return None
    s = str(s).strip()
    if s.lower() in NA_TOKENS:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def discover_files(input_dir):
    """递归扫描输入目录，返回排序后的 (表文件, 数据库文件) 列表（确定性顺序）。"""
    tables, dbs = [], []
    for root, dirs, files in os.walk(input_dir):
        dirs.sort()
        for name in sorted(files):
            if name.startswith("."):
                continue
            low = name.lower()
            p = os.path.join(root, name)
            if low.endswith(CSV_EXTS):
                tables.append(p)
            elif low.endswith(DB_EXTS):
                dbs.append(p)
    return tables, dbs


def sniff_encoding(path):
    for enc in ENCODINGS:
        try:
            with open(path, "r", encoding=enc) as f:
                f.read()
            return enc
        except UnicodeDecodeError:
            continue
        except OSError:
            return None
    return "latin-1"


def _looks_numeric_ratio(cells):
    if not cells:
        return 0.0
    ok = sum(1 for c in cells if to_float(c) is not None)
    return ok / len(cells)


def load_csv(path):
    """读取 CSV/TSV，返回 (columns, rows, encoding, has_header)。
    表头启发式：若后续行中某列 ≥80% 为数值，而首行该单元格非数值，则判定首行为表头。"""
    enc = sniff_encoding(path)
    if enc is None:
        raise OSError("无法读取文件: %s" % path)
    with open(path, "r", encoding=enc, newline="") as f:
        sample = f.read()
    delim = "\t" if path.lower().endswith(".tsv") else ","
    first = sample.splitlines()[0] if sample.splitlines() else ""
    if "\t" in first and delim == ",":
        delim = "\t"
    reader = csv.reader(io_string(sample), delimiter=delim)
    rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        raise ValueError("CSV 为空: %s" % path)
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    body = rows[1:]
    has_header = False
    if body:
        for j in range(width):
            col_body = [r[j] for r in body]
            if _looks_numeric_ratio(col_body) >= 0.8 and to_float(rows[0][j]) is None:
                has_header = True
                break
    if has_header:
        columns = [str(c).strip() or ("col%d" % (i + 1)) for i, c in enumerate(rows[0])]
        data = body
    else:
        columns = ["col%d" % (i + 1) for i in range(width)]
        data = rows
    return columns, data, enc, has_header


def io_string(s):
    import io
    return io.StringIO(s)


def load_sqlite(path):
    """读取 SQLite 第一个数据表（表名按字典序取第一个，保证确定性）。"""
    uri = "file:%s?mode=ro" % os.path.abspath(path)
    con = sqlite3.connect(uri, uri=True)
    try:
        names = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        if not names:
            raise ValueError("SQLite 无数据表: %s" % path)
        t = names[0]
        cur = con.execute('SELECT * FROM "%s"' % t.replace('"', ""))
        columns = [d[0] for d in cur.description]
        data = [["" if v is None else str(v) for v in row] for row in cur.fetchall()]
        return columns, data, t
    finally:
        con.close()


def coerce_columns(columns, data):
    """逐列尝试数值化。返回 cols = {name: {"values": [float|None], "numeric": bool,
    "cardinality": int(原始非空去重数), "missing": int}}。"""
    cols = {}
    for j, name in enumerate(columns):
        raw = [r[j] if j < len(r) else "" for r in data]
        values = [to_float(x) for x in raw]
        nonnull = [x for x in raw if str(x).strip().lower() not in NA_TOKENS]
        numeric = len(nonnull) > 0 and sum(v is not None for v in values) >= 0.9 * len(nonnull)
        cols[name] = {
            "values": values if numeric else None,
            "raw": raw,
            "numeric": numeric,
            "cardinality": len(set(nonnull)),
            "missing": sum(1 for x in raw if str(x).strip().lower() in NA_TOKENS),
            "n_rows": len(raw),
        }
    return cols


def infer_columns(cols, spec=None):
    """推断 (结果变量, 分组变量)。
    规则：分组变量 = 基数 ≥2 且（字符串型 或 整数型且基数 ≤20）中基数最小者，
    命名线索优先；结果变量 = 命名线索优先，否则取数值列中基数最高者（并列取方差大者、
    再并列取最后一列）。spec 可强制指定（键为列名或 colN 形式）。"""
    spec = spec or {}
    names = list(cols.keys())
    if spec.get("group_variable") in names:
        group = spec["group_variable"]
    else:
        cands = []
        for nm, c in cols.items():
            if c["cardinality"] < 2 or c["cardinality"] >= max(2, c["n_rows"]):
                continue
            int_like = c["numeric"] and all(
                v is None or float(v).is_integer() for v in c["values"])
            if (not c["numeric"]) or (int_like and c["cardinality"] <= 20):
                cands.append(nm)
        if not cands:
            group = None
        else:
            def hint_key(nm):
                low = nm.lower()
                hinted = 0 if any(h in low for h in GROUP_HINTS) else 1
                return (hinted, cols[nm]["cardinality"], names.index(nm))
            group = sorted(cands, key=hint_key)[0]
    if spec.get("outcome") in names:
        outcome = spec["outcome"]
    else:
        num = [nm for nm in names if cols[nm]["numeric"] and nm != group]
        if not num:
            outcome = None
        else:
            def okey(nm):
                low = nm.lower()
                hinted = 0 if any(h in low for h in OUTCOME_HINTS) else 1
                vals = [v for v in cols[nm]["values"] if v is not None]
                var = statistics.pvariance(vals) if len(vals) > 1 else 0.0
                return (hinted, -cols[nm]["cardinality"], -var, -names.index(nm))
            outcome = sorted(num, key=okey)[0]
    return outcome, group


def _quartiles(xs):
    if not xs:
        return None, None, None
    if len(xs) == 1:
        v = xs[0]
        return v, v, v
    q1, q2, q3 = statistics.quantiles(xs, n=4, method="inclusive")
    return q1, q2, q3


def summarize_groups(cols, outcome, group):
    """分组描述统计（仅描述性）。组顺序：数值组按数值排序，否则按字典序（确定性）。"""
    oc, gc = cols[outcome], cols[group]
    pairs = []
    for i in range(oc["n_rows"]):
        gv = (gc["raw"][i] or "").strip() or "NA"
        pairs.append((gv, oc["values"][i]))
    gvals = {}
    for gv, v in pairs:
        gvals.setdefault(gv, []).append(v)
    def gsort(k):
        f = to_float(k)
        return (0, f) if f is not None else (1, k)
    summaries = {}
    for gv in sorted(gvals, key=gsort):
        vals = [v for v in gvals[gv] if v is not None]
        missing = sum(1 for v in gvals[gv] if v is None)
        q1, med, q3 = _quartiles(sorted(vals))
        summaries[gv] = {
            "n": len(gvals[gv]),
            "n_valid": len(vals),
            "missing": missing,
            "mean": norm_num(statistics.fmean(vals)) if vals else None,
            "sd": norm_num(statistics.stdev(vals)) if len(vals) > 1 else None,
            "median": norm_num(med),
            "q1": norm_num(q1),
            "q3": norm_num(q3),
        }
    allv = [v for gv in gvals for v in gvals[gv] if v is not None]
    q1, med, q3 = _quartiles(sorted(allv))
    overall = {
        "n": oc["n_rows"],
        "n_valid": len(allv),
        "missing": oc["missing"],
        "mean": norm_num(statistics.fmean(allv)) if allv else None,
        "sd": norm_num(statistics.stdev(allv)) if len(allv) > 1 else None,
        "median": norm_num(med),
        "q1": norm_num(q1),
        "q3": norm_num(q3),
    }
    return {"groups": summaries, "overall": overall, "n_groups": len(summaries)}


def minimal_cleaning(columns, data, source_path, all_paths):
    """最小清洗计数：原始行数、完全重复行数（保留首条）、缺失计数、源文件哈希。"""
    raw = [tuple(r) for r in data]
    seen, dups = set(), 0
    kept = []
    for r in raw:
        if r in seen:
            dups += 1
        else:
            seen.add(r)
            kept.append(r)
    missing_by_column = {}
    for j, nm in enumerate(columns):
        miss = sum(1 for r in raw if str(r[j]).strip().lower() in NA_TOKENS)
        if miss:
            missing_by_column[nm] = miss
    files = []
    for p in sorted(all_paths):
        try:
            files.append({"file": os.path.basename(p),
                          "sha256": sha256_file(p)})
        except OSError:
            files.append({"file": os.path.basename(p), "sha256": None})
    return {
        "n_rows_raw": len(raw),
        "n_duplicates_removed": dups,
        "n_rows_used": len(kept),
        "missing_by_column": missing_by_column,
        "source_files": files,
        "primary_source": os.path.basename(source_path),
    }


def load_table(input_dir, spec=None):
    """主入口：定位主表并返回上下文 dict。优先 CSV/TSV，其次 SQLite。"""
    tables, dbs = discover_files(input_dir)
    spec = spec or {}
    used = None
    if tables:
        path = tables[0]
        columns, data, enc, has_header = load_csv(path)
        used = {"kind": "csv", "path": path, "encoding": enc,
                "has_header": has_header}
        all_paths = tables + dbs
    elif dbs:
        path = dbs[0]
        columns, data, tname = load_sqlite(path)
        used = {"kind": "sqlite", "path": path, "table": tname,
                "encoding": None, "has_header": False}
        all_paths = dbs
    else:
        raise FileNotFoundError("输入目录中未找到 CSV/TSV/SQLite 数据文件: %s" % input_dir)
    cols = coerce_columns(columns, data)
    outcome, group = infer_columns(cols, spec)
    return {"columns": cols, "column_names": columns, "used": used,
            "all_paths": all_paths, "outcome": outcome, "group": group,
            "raw_data": data}


def build_findings(input_dir, spec=None):
    ctx = load_table(input_dir, spec)
    outcome, group = ctx["outcome"], ctx["group"]
    cleaning = minimal_cleaning(ctx["column_names"], ctx["raw_data"],
                                ctx["used"]["path"], ctx["all_paths"])
    # 按 duplicate_policy=dedupe_exact_keep_first 去重后再做描述统计
    seen, deduped = set(), []
    for r in ctx["raw_data"]:
        t = tuple(r)
        if t not in seen:
            seen.add(t)
            deduped.append(r)
    cols = coerce_columns(ctx["column_names"], deduped)
    findings = {
        "schema_version": "bundle3-descriptive-only/v1",
        "outcome": outcome,
        "group_variable": group,
        "alpha": 0.05,
        "cleaning": cleaning,
        "data_source": {"primary_source": cleaning["primary_source"],
                        "kind": ctx["used"]["kind"],
                        "encoding": ctx["used"]["encoding"],
                        "has_header": ctx["used"]["has_header"]},
    }
    if outcome and group:
        gs = summarize_groups(cols, outcome, group)
        findings["group_summaries"] = gs["groups"]
        findings["descriptive"] = {
            "outcome": outcome,
            "group_variable": group,
            "overall": gs["overall"],
            "n_groups": gs["n_groups"],
        }
    not_computed = {"status": "not_computed", "note": "需要由检验阶段提供"}
    for k in ("method", "statistic", "df", "p", "effect_size",
              "confidence_interval", "multiplicity", "assumptions",
              "paired"):
        findings.setdefault(k, dict(not_computed))
    findings["inferential_status"] = "not_computed"
    return findings


def main(argv=None):
    ap = argparse.ArgumentParser(description="最小描述统计模块（仅描述性，不做检验）")
    ap.add_argument("--input-dir", required=True)
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--outcome", default=None, help="强制指定结果变量列名")
    ap.add_argument("--group", dest="group_variable", default=None,
                    help="强制指定分组变量列名")
    args = ap.parse_args(argv)
    spec = {}
    if args.outcome:
        spec["outcome"] = args.outcome
    if args.group_variable:
        spec["group_variable"] = args.group_variable
    try:
        findings = build_findings(args.input_dir, spec)
    except Exception as exc:  # 不抛出未处理异常
        findings = {"schema_version": "bundle3-descriptive-only/v1",
                    "inferential_status": "not_computed",
                    "status": "failed",
                    "exceptions": ["%s: %s" % (type(exc).__name__, exc)]}
    os.makedirs(args.work_dir, exist_ok=True)
    out = os.path.join(args.work_dir, "findings.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(findings, f, ensure_ascii=False, indent=2, sort_keys=True)
    print("[summarize] findings 已写入 %s" % out)
    if findings.get("status") == "failed":
        print("[summarize] 数据定位失败: %s" % "; ".join(findings["exceptions"]))
        print("[summarize] 建议 NEXT: 检查 --input-dir 是否包含 CSV/TSV/SQLite 文件。")
        return 1
    d = findings.get("descriptive") or {}
    print("[summarize] 结果变量=%s 分组变量=%s 组数=%s 行数=%s" % (
        findings.get("outcome"), findings.get("group_variable"),
        d.get("n_groups"), findings.get("cleaning", {}).get("n_rows_used")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
