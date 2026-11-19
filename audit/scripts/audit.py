#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit.py — 数据卫生审计单入口脚本 (data hygiene audit, stdlib only).

子命令:
  run      --input-dir /input --work-dir /app/work --output-dir /app/output
  decide   --rule <rule_id> --action keep|drop|flag --reason "..." [--work-dir /app/work]
  finalize [--work-dir /app/work] [--output-dir /app/output]

设计原则: 任何失败都不抛出未捕获异常; 始终尽可能写出诚实的 result.json。
仅使用 Python 标准库。可选第三方库一律不需要。
"""

import argparse
import csv
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import traceback
import unicodedata
from datetime import datetime

# ----------------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------------

MISSING_TOKENS = {"", "na", "n/a", "nan", "null", "none", "-", ".", "?", "缺失"}
SCALE_CANDIDATES = [1e3, 1e-3, 1e6, 1e-6, 12.0, 1.0 / 12.0, 2.54, 1.0 / 2.54]
MAX_ROWS = 500000          # 防御性上限
MAX_INDEX_LIST = 500       # 报告中索引列表上限
ROW_CAP_CROSS = 200000     # 跨源逐值比对上限
SQLITE_MAGIC = b"SQLite format 3\x00"

PLAIN_NUM_RE = re.compile(r"^-?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][-+]?\d+)?$")
THOUSAND_RE = re.compile(r"^-?\d{1,3}(,\d{3})+(\.\d+)?$")
UNIT_SUFFIX_RE = re.compile(
    r"^(-?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][-+]?\d+)?)\s*([A-Za-zμµ/%°³][A-Za-z0-9μµ/%°³.\-_ ]*)$")

UNIT_NAME_TOKENS = ["mcg", "μg", "ug", "mg", "kg", "ml", "cm", "mm", "mpa",
                    "pa", "days", "day", "天", "%", "g", "l", "m"]

IMPOSSIBLE_NEGATIVE_NAMES = re.compile(
    r"(age|length|width|strength|weight|amount|percent|pct|ratio|count|浓度|含量|强度|龄期|重量)",
    re.IGNORECASE)
PERCENT_NAME_RE = re.compile(r"(percent|pct|ratio|%|百分比|占比|率$)", re.IGNORECASE)

# 合理性范围表(仅记录提示, 绝不硬拒绝)
PLAUSIBILITY_HINTS = [
    (re.compile(r"water|水", re.I), (100.0, 300.0), "kg/m3 混凝土拌合水常见范围(提示)"),
    (re.compile(r"cement|水泥", re.I), (100.0, 600.0), "kg/m3 水泥用量常见范围(提示)"),
    (re.compile(r"age|龄期", re.I), (1.0, 365.0), "天, 养护龄期常见范围(提示)"),
    (re.compile(r"strength|强度", re.I), (0.0, 120.0), "MPa 抗压强度常见范围(提示)"),
    (re.compile(r"percent|pct|%", re.I), (0.0, 100.0), "百分比应在 0-100(提示)"),
]

# ----------------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------------


def now_iso():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_float(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def quantile(sorted_x, p):
    n = len(sorted_x)
    if n == 0:
        return None
    if n == 1:
        return sorted_x[0]
    pos = (n - 1) * p
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_x[lo]
    return sorted_x[lo] + (sorted_x[hi] - sorted_x[lo]) * (pos - lo)


def sig_round(x, sig=3):
    if x is None or x == 0:
        return 0.0
    try:
        d = sig - 1 - int(math.floor(math.log10(abs(x))))
        return round(x, d)
    except (ValueError, OverflowError):
        return x


def fmt_num(x):
    if x is None:
        return None
    if isinstance(x, float):
        if x != x:  # NaN
            return None
        if abs(x - round(x)) < 1e-12 and abs(x) < 1e15:
            return int(round(x))
        return round(x, 6)
    return x


def cap_list(lst, cap=MAX_INDEX_LIST):
    if len(lst) <= cap:
        return list(lst)
    return list(lst[:cap]) + ["...(%d more)" % (len(lst) - cap)]


def is_missing(v):
    if v is None:
        return True
    s = unicodedata.normalize("NFKC", str(v)).strip()
    return s.lower() in MISSING_TOKENS


def parse_numeric(v):
    """返回 (value_or_None, kind, unit)。kind ∈ missing,num,thousand,percent,unit_suffix,text"""
    if v is None:
        return None, "missing", None
    s = unicodedata.normalize("NFKC", str(v)).strip()
    if s.lower() in MISSING_TOKENS:
        return None, "missing", None
    if PLAIN_NUM_RE.match(s):
        try:
            return float(s), "num", None
        except ValueError:
            pass
    if THOUSAND_RE.match(s):
        try:
            return float(s.replace(",", "")), "thousand", None
        except ValueError:
            pass
    if s.endswith("%") and PLAIN_NUM_RE.match(s[:-1].strip()):
        try:
            return float(s[:-1].strip()), "percent", "%"
        except ValueError:
            pass
    m = UNIT_SUFFIX_RE.match(s)
    if m:
        try:
            return float(m.group(1)), "unit_suffix", m.group(2).strip()
        except ValueError:
            pass
    return None, "text", None


def affinity(declared):
    t = (declared or "").upper()
    if "INT" in t:
        return "INTEGER"
    if any(k in t for k in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if "BLOB" in t or t == "":
        return "BLOB"
    if any(k in t for k in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    return "NUMERIC"


def unit_hint_from_name(name):
    low = str(name).lower()
    hits = []
    for tok in UNIT_NAME_TOKENS:  # 长短语优先排列
        if re.search(r"(?<![a-z])" + re.escape(tok) + r"(?![a-z])", low) or tok in ("天", "%") and tok in low:
            if tok not in hits:
                hits.append(tok)
    # 过滤被长词覆盖的短词 (mg 命中时去掉 g)
    if "mg" in hits and "g" in hits:
        hits.remove("g")
    if ("kg" in hits or "mg" in hits or "μg" in hits or "ug" in hits or "mcg" in hits) and "g" in hits:
        hits.remove("g")
    if "ml" in hits and "l" in hits:
        hits.remove("l")
    if "ml" in hits and "m" in hits:
        hits.remove("m")
    if ("cm" in hits or "mm" in hits) and "m" in hits:
        hits.remove("m")
    if "mpa" in hits and "pa" in hits:
        hits.remove("pa")
    if "mpa" in hits and "m" in hits:
        hits.remove("m")
    if "days" in hits and "day" in hits:
        hits.remove("day")
    return hits


# ----------------------------------------------------------------------------
# 文件清点
# ----------------------------------------------------------------------------


def guess_format(path):
    ext = os.path.splitext(path)[1].lower()
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        head = b""
    if head.startswith(SQLITE_MAGIC):
        return "sqlite"
    if ext in (".db", ".sqlite", ".sqlite3"):
        return "sqlite"
    if ext in (".csv",):
        return "csv"
    if ext in (".tsv", ".tab"):
        return "tsv"
    if ext in (".json",):
        return "json"
    if ext in (".md", ".txt"):
        return "text"
    if ext in (".dat", ".data"):
        return "csv-like"
    return "other"


def inventory(input_dir, exceptions):
    files = []
    for root, dirs, names in os.walk(input_dir):
        dirs.sort()
        for nm in sorted(names):
            if nm.startswith("."):
                continue
            p = os.path.join(root, nm)
            try:
                st = os.stat(p)
                files.append({
                    "name": os.path.relpath(p, input_dir),
                    "path": p,
                    "size": st.st_size,
                    "sha256": sha256_of(p),
                    "format": guess_format(p),
                })
            except OSError as e:
                exceptions.append({"stage": "inventory", "message": "%s: %s" % (nm, e)})
    return files


# ----------------------------------------------------------------------------
# CSV 读取
# ----------------------------------------------------------------------------


def read_text(path):
    for enc in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read(), enc
        except (UnicodeDecodeError, UnicodeError):
            continue
    with open(path, "r", encoding="latin-1", errors="replace") as f:
        return f.read(), "latin-1(errors=replace)"


def sniff_delimiter(text):
    lines = [l for l in text.splitlines() if l.strip()][:30]
    sample = "\n".join(lines)
    if sample:
        try:
            return csv.Sniffer().sniff(sample, delimiters=",\t;").delimiter
        except csv.Error:
            pass
    first = lines[0] if lines else ""
    counts = {d: first.count(d) for d in (",", "\t", ";")}
    best = max(counts, key=lambda k: counts[k])
    if counts[best] > 0:
        return best
    if len(first.split()) > 1:
        return "ws"
    return ","


def numeric_ratio(cells):
    if not cells:
        return 0.0
    n = 0
    for c in cells:
        v, kind, _ = parse_numeric(c)
        if kind in ("num", "thousand", "percent", "unit_suffix"):
            n += 1
    return n / float(len(cells))


def load_csv_table(path, name, exceptions):
    """返回 table dict 或 None"""
    text, encoding = read_text(path)
    delim = sniff_delimiter(text)
    raw_rows = []
    if delim == "ws":
        for line in text.splitlines():
            if line.strip():
                raw_rows.append(line.split())
    else:
        rdr = csv.reader(text.splitlines(), delimiter=delim)
        for r in rdr:
            raw_rows.append(list(r))
    # 丢弃全空行
    raw_rows = [r for r in raw_rows if any(not is_missing(c) for c in r)]
    if not raw_rows:
        return None
    # 列数对齐
    ncols = max(len(r) for r in raw_rows)
    raw_rows = [r + [""] * (ncols - len(r)) for r in raw_rows]
    if len(raw_rows) > MAX_ROWS:
        exceptions.append({"stage": "load_csv",
                           "message": "%s 超过 %d 行, 截断" % (name, MAX_ROWS)})
        raw_rows = raw_rows[:MAX_ROWS]
    # 表头探测: 首行非数值比例高 → 表头
    r0 = numeric_ratio(raw_rows[0])
    r1 = numeric_ratio(raw_rows[1]) if len(raw_rows) > 1 else 1.0
    if r0 <= 0.4 and (len(raw_rows) == 1 or r1 > r0 or r0 == 0.0):
        header = [str(h).strip() or "col_%d" % (i + 1) for i, h in enumerate(raw_rows[0])]
        data = raw_rows[1:]
        has_header = True
    else:
        header = ["col_%d" % (i + 1) for i in range(ncols)]
        data = raw_rows
        has_header = False
    # 表头去重
    seen = {}
    for i, h in enumerate(header):
        if h in seen:
            seen[h] += 1
            header[i] = "%s_%d" % (h, seen[h])
        else:
            seen[h] = 1
    return {"name": name, "source_path": path, "format": "csv",
            "encoding": encoding, "delimiter": {"\t": "tab", "ws": "whitespace"}.get(delim, delim),
            "has_header": has_header, "header": header, "rows": data,
            "declared_types": None}


# ----------------------------------------------------------------------------
# SQLite 读取
# ----------------------------------------------------------------------------


def cell_to_str(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, bytes):
        return "<blob:%d bytes>" % len(v)
    return str(v)


def load_sqlite_tables(path, name, exceptions):
    tables = []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    except sqlite3.Error:
        con = sqlite3.connect(path)
    try:
        cur = con.cursor()
        tnames = [r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for tn in tnames:
            try:
                info = cur.execute('PRAGMA table_info("%s")' % tn.replace('"', '""')).fetchall()
                cols = [r[1] for r in info]
                decl = {r[1]: (r[2] or "") for r in info}
                cnt = cur.execute('SELECT COUNT(*) FROM "%s"' % tn.replace('"', '""')).fetchone()[0]
                qrows = cur.execute('SELECT * FROM "%s" LIMIT %d'
                                    % (tn.replace('"', '""'), MAX_ROWS)).fetchall()
                rows = [[cell_to_str(v) for v in row] for row in qrows]
                tables.append({"name": "%s::%s" % (name, tn), "source_path": path,
                               "format": "sqlite", "table": tn,
                               "encoding": "n/a", "delimiter": "n/a",
                               "has_header": True, "header": cols, "rows": rows,
                               "declared_types": decl,
                               "declared_affinity": {c: affinity(decl[c]) for c in cols},
                               "db_rowcount": cnt})
            except sqlite3.Error as e:
                exceptions.append({"stage": "load_sqlite",
                                   "message": "%s::%s: %s" % (name, tn, e)})
    except sqlite3.Error as e:
        exceptions.append({"stage": "load_sqlite", "message": "%s: %s" % (name, e)})
    finally:
        con.close()
    return tables


# ----------------------------------------------------------------------------
# 列级剖析
# ----------------------------------------------------------------------------


def analyze_column(values):
    """values: list[str]。返回列画像 dict + 数值列表(带行索引)"""
    n = len(values)
    missing_idx = []
    nums = []           # (row_idx, float)
    kinds = {}
    units_from_values = {}
    texts = {}
    for i, v in enumerate(values):
        val, kind, unit = parse_numeric(v)
        kinds[kind] = kinds.get(kind, 0) + 1
        if kind == "missing":
            missing_idx.append(i)
        elif kind in ("num", "thousand", "percent", "unit_suffix"):
            nums.append((i, val))
            if unit:
                units_from_values[unit] = units_from_values.get(unit, 0) + 1
        else:
            key = unicodedata.normalize("NFKC", str(v)).strip()
            texts[key] = texts.get(key, 0) + 1
    n_valid = n - len(missing_idx)
    n_num = len(nums)
    uniq = set()
    for v in values:
        if not is_missing(v):
            uniq.add(unicodedata.normalize("NFKC", str(v)).strip())
    n_unique = len(uniq)
    # 推断类型
    if n_valid == 0:
        inferred = "empty"
    elif n_num == n_valid:
        inferred = "integer" if all(abs(x - round(x)) < 1e-9 for _, x in nums) else "float"
    elif n_num == 0:
        inferred = "categorical" if n_unique <= max(20, n_valid // 2 + 1) else "text"
    else:
        inferred = "mixed"
    prof = {"missing_count": len(missing_idx),
            "missing_pct": round(100.0 * len(missing_idx) / n, 2) if n else 0.0,
            "missing_row_indices": cap_list(missing_idx),
            "n_valid": n_valid, "n_unique": n_unique,
            "inferred_type": inferred,
            "cell_kinds": kinds,
            "unit_hint_values": None}
    if units_from_values and n_valid:
        top_unit, top_cnt = max(units_from_values.items(), key=lambda kv: kv[1])
        if top_cnt >= max(2, 0.5 * n_valid):
            prof["unit_hint_values"] = top_unit
    if nums:
        xs = sorted(x for _, x in nums)
        mean = sum(xs) / len(xs)
        var = sum((x - mean) ** 2 for x in xs) / (len(xs) - 1) if len(xs) > 1 else 0.0
        prof["numeric"] = {
            "n": len(xs), "min": fmt_num(xs[0]), "max": fmt_num(xs[-1]),
            "mean": fmt_num(mean), "sd": fmt_num(math.sqrt(var)),
            "median": fmt_num(quantile(xs, 0.5)),
            "q1": fmt_num(quantile(xs, 0.25)), "q3": fmt_num(quantile(xs, 0.75)),
        }
    if texts:
        top = sorted(texts.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
        prof["top_categories"] = [{"value": k, "count": c} for k, c in top]
    return prof, nums


def missingness_analysis(table, col_profiles):
    rows = table["rows"]
    header = table["header"]
    n = len(rows)
    per_column = {}
    for j, col in enumerate(header):
        p = col_profiles[j]
        per_column[col] = {"missing_count": p["missing_count"],
                           "missing_pct": p["missing_pct"]}
    per_row_counts = [0] * n
    patterns = {}
    for i, row in enumerate(rows):
        miss_cols = []
        for j, c in enumerate(row):
            if is_missing(c):
                miss_cols.append(header[j])
        per_row_counts[i] = len(miss_cols)
        key = tuple(miss_cols)
        patterns[key] = patterns.get(key, 0) + 1
    pat_list = sorted(patterns.items(), key=lambda kv: -kv[1])[:10]
    pattern_summary = [{"missing_columns": list(k), "row_count": v} for k, v in pat_list]
    total_cells = n * len(header) if header else 0
    total_missing = sum(per_row_counts)
    rows_with_missing = sum(1 for c in per_row_counts if c > 0)
    # 缺失与其他列观测值的启发式比较(无正式检验): 若某列缺失行在另一数值列上的均值
    # 与非缺失行均值相差 >1 个标准差, 记录提示。
    heuristic_notes = []
    numeric_cols = [j for j, p in enumerate(col_profiles) if p.get("numeric") and p["numeric"]["n"] >= 10]
    for j, col in enumerate(header):
        if col_profiles[j]["missing_count"] == 0:
            continue
        miss_set = set(col_profiles[j]["missing_row_indices"][:n]) \
            if col_profiles[j]["missing_count"] <= MAX_INDEX_LIST else None
        if miss_set is None:
            miss_set = set(i for i, r in enumerate(rows) if is_missing(r[j]))
        for k in numeric_cols:
            if k == j:
                continue
            a = [safe_float(rows[i][k]) for i in range(n) if i in miss_set]
            b = [safe_float(rows[i][k]) for i in range(n) if i not in miss_set]
            a = [x for x in a if x is not None]
            b = [x for x in b if x is not None]
            if len(a) < 5 or len(b) < 10:
                continue
            mb = sum(b) / len(b)
            sb = math.sqrt(sum((x - mb) ** 2 for x in b) / (len(b) - 1)) if len(b) > 1 else 0.0
            if sb <= 0:
                continue
            ma = sum(a) / len(a)
            if abs(ma - mb) > sb:
                heuristic_notes.append(
                    "列 %s 缺失行的 %s 均值(%.3g) 与非缺失行(%.3g) 相差超过 1 SD → 缺失或非随机(MAR/MNAR 提示, 未做正式检验)"
                    % (col, header[k], ma, mb))
    return {"per_column": per_column,
            "rows_with_missing": rows_with_missing,
            "total_cells": total_cells,
            "total_missing": total_missing,
            "total_missing_pct": round(100.0 * total_missing / total_cells, 2) if total_cells else 0.0,
            "patterns_top": pattern_summary,
            "heuristic_notes": heuristic_notes[:20]}


# ----------------------------------------------------------------------------
# 重复
# ----------------------------------------------------------------------------


def duplicates_analysis(table, col_profiles):
    rows = table["rows"]
    header = table["header"]
    key_of = lambda r: tuple(unicodedata.normalize("NFKC", str(c)).strip() for c in r)
    seen = {}
    dup_indices = []
    for i, r in enumerate(rows):
        k = key_of(r)
        if k in seen:
            dup_indices.append(i)
        else:
            seen[k] = i
    # 近似重复: 数值 3 位有效数字 + 缺失归一化
    near_seen = {}
    near_groups = []
    for i, r in enumerate(rows):
        parts = []
        for j, c in enumerate(r):
            v, kind, _ = parse_numeric(c)
            if kind == "missing":
                parts.append("<NA>")
            elif kind in ("num", "thousand", "percent", "unit_suffix"):
                parts.append("num:%s" % repr(sig_round(v, 3)))
            else:
                parts.append("txt:%s" % unicodedata.normalize("NFKC", str(c)).strip().lower())
        k = tuple(parts)
        near_seen.setdefault(k, []).append(i)
    near_groups = [sorted(g) for g in near_seen.values() if len(g) > 1]
    near_groups.sort(key=lambda g: g[0])
    return {"exact_duplicate_row_indices": cap_list(dup_indices),
            "exact_removed": len(dup_indices),
            "near_group_count": len(near_groups),
            "near_groups_preview": [cap_list(g, 20) for g in near_groups[:20]]}


# ----------------------------------------------------------------------------
# 异常值 / 不可能值
# ----------------------------------------------------------------------------


def outliers_analysis(table, col_profiles):
    header = table["header"]
    rows = table["rows"]
    n = len(rows)
    # 分组候选列: 2..10 个唯一值
    group_col = None
    for j, p in enumerate(col_profiles):
        if 2 <= p["n_unique"] <= 10 and p["inferred_type"] in ("categorical", "text", "integer"):
            group_col = j
            break
    per_column = {}
    for j, col in enumerate(header):
        p = col_profiles[j]
        if not p.get("numeric") or p["numeric"]["n"] < 8:
            continue
        vals = []
        for i in range(n):
            v, kind, _ = parse_numeric(rows[i][j])
            if kind in ("num", "thousand", "percent", "unit_suffix"):
                vals.append((i, v))
        xs = sorted(v for _, v in vals)
        q1 = quantile(xs, 0.25)
        q3 = quantile(xs, 0.75)
        iqr = q3 - q1
        med = quantile(xs, 0.5)
        mild = []
        extreme = []
        if iqr > 0:
            lo1, hi1 = q1 - 1.5 * iqr, q3 + 1.5 * iqr
            lo3, hi3 = q1 - 3.0 * iqr, q3 + 3.0 * iqr
            for i, v in vals:
                if v < lo3 or v > hi3:
                    extreme.append((i, v))
                elif v < lo1 or v > hi1:
                    mild.append((i, v))
        mad_list = []
        absdev = sorted(abs(v - med) for _, v in vals)
        mad = quantile(absdev, 0.5)
        if mad and mad > 0:
            for i, v in vals:
                mz = 0.6745 * (v - med) / mad
                if abs(mz) > 3.5:
                    mad_list.append((i, v, round(mz, 2)))
        entry = {"q1": fmt_num(q1), "q3": fmt_num(q3), "iqr": fmt_num(iqr),
                 "median": fmt_num(med), "mad": fmt_num(mad),
                 "mild_count": len(mild), "extreme_count": len(extreme),
                 "mad_count": len(mad_list),
                 "mild": cap_list([{"row": i, "value": fmt_num(v)} for i, v in mild]),
                 "extreme": cap_list([{"row": i, "value": fmt_num(v)} for i, v in extreme]),
                 "mad_flagged": cap_list([{"row": i, "value": fmt_num(v), "modified_z": z}
                                          for i, v, z in mad_list])}
        # 分组内异常值
        if group_col is not None and group_col != j:
            groups = {}
            for i in range(n):
                gk = unicodedata.normalize("NFKC", str(rows[i][group_col])).strip()
                v, kind, _ = parse_numeric(rows[i][j])
                if kind in ("num", "thousand", "percent", "unit_suffix"):
                    groups.setdefault(gk, []).append((i, v))
            g_out = {}
            for gk, gvals in sorted(groups.items()):
                if len(gvals) < 8:
                    continue
                gxs = sorted(v for _, v in gvals)
                gq1, gq3 = quantile(gxs, 0.25), quantile(gxs, 0.75)
                giqr = gq3 - gq1
                if giqr <= 0:
                    continue
                glo, ghi = gq1 - 1.5 * giqr, gq3 + 1.5 * giqr
                idx = [i for i, v in gvals if v < glo or v > ghi]
                if idx:
                    g_out[gk] = {"count": len(idx), "row_indices": cap_list(idx)}
            if g_out:
                entry["per_group_outliers"] = {"group_column": header[group_col], "groups": g_out}
        per_column[col] = entry
    # 不可能值检查(仅标记)
    impossible = []
    for j, col in enumerate(header):
        p = col_profiles[j]
        if not p.get("numeric"):
            continue
        if IMPOSSIBLE_NEGATIVE_NAMES.search(col):
            neg = [{"row": i, "value": fmt_num(v)} for i in range(n)
                   for v, kind, _u in [parse_numeric(rows[i][j])]
                   if kind in ("num", "thousand", "percent", "unit_suffix") and v is not None and v < 0]
            if neg:
                impossible.append({"column": col, "check": "negative_value",
                                   "count": len(neg), "examples": cap_list(neg, 20)})
        if PERCENT_NAME_RE.search(col) or p.get("unit_hint_values") == "%":
            big = [{"row": i, "value": fmt_num(v)} for i in range(n)
                   for v, kind, _u in [parse_numeric(rows[i][j])]
                   if kind in ("num", "thousand", "percent", "unit_suffix") and v is not None and v > 100]
            if big:
                impossible.append({"column": col, "check": "percent_gt_100",
                                   "count": len(big), "examples": cap_list(big, 20)})
    return {"per_column": per_column, "impossible_values": impossible,
            "group_column_used": header[group_col] if group_col is not None else None}


# ----------------------------------------------------------------------------
# 单位/量纲提示
# ----------------------------------------------------------------------------


def unit_scale_hints(table, col_profiles):
    header = table["header"]
    hints = []
    for j, col in enumerate(header):
        p = col_profiles[j]
        name_hits = unit_hint_from_name(col)
        uh = p.get("unit_hint_values")
        entry = {"column": col, "name_tokens": name_hits, "value_unit_hint": uh,
                 "observed_min": p.get("numeric", {}).get("min"),
                 "observed_max": p.get("numeric", {}).get("max"),
                 "plausibility": None}
        for rx, (lo, hi), note in PLAUSIBILITY_HINTS:
            if rx.search(col) and p.get("numeric"):
                mn, mx = p["numeric"]["min"], p["numeric"]["max"]
                inside = (mn is not None and mx is not None and mn >= lo and mx <= hi)
                entry["plausibility"] = {"expected_range": [lo, hi], "note": note,
                                         "within_range": bool(inside)}
        if name_hits or uh or entry["plausibility"]:
            hints.append(entry)
    return hints


# ----------------------------------------------------------------------------
# 跨源一致性
# ----------------------------------------------------------------------------


def norm_name(s):
    return re.sub(r"[^0-9a-zA-Z一-鿿]+", "", str(s)).lower()


def cross_source_consistency(csv_tables, sqlite_tables):
    pairs = []
    for ct in csv_tables:
        for st in sqlite_tables:
            if len(ct["header"]) != len(st["header"]):
                continue
            rdiff = abs(len(ct["rows"]) - len(st["rows"]))
            if rdiff > max(2, 0.02 * max(len(ct["rows"]), len(st["rows"]), 1)):
                continue
            nrows = min(len(ct["rows"]), len(st["rows"]), ROW_CAP_CROSS)
            # 列映射
            mapping = []
            used = set()
            csv_norm = {norm_name(h): j for j, h in enumerate(ct["header"])}
            for j, sh in enumerate(st["header"]):
                if sh in ct["header"] and ct["header"].index(sh) not in used:
                    jj = ct["header"].index(sh)
                    mapping.append({"db_column": sh, "csv_column": ct["header"][jj],
                                    "method": "exact"})
                    used.add(jj)
                elif norm_name(sh) in csv_norm and csv_norm[norm_name(sh)] not in used:
                    jj = csv_norm[norm_name(sh)]
                    mapping.append({"db_column": sh, "csv_column": ct["header"][jj],
                                    "method": "fuzzy"})
                    used.add(jj)
                else:
                    mapping.append({"db_column": sh, "csv_column": ct["header"][j],
                                    "method": "positional", "csv_index": j})
                    used.add(j)
            # 逐列比对
            col_reports = []
            type_mismatches = []
            numeric_in_text = []
            scale_mismatches = []
            for m in mapping:
                sj = st["header"].index(m["db_column"])
                cj = ct["header"].index(m["csv_column"])
                decl = (st.get("declared_types") or {}).get(m["db_column"], "")
                aff = affinity(decl)
                db_vals = [st["rows"][i][sj] for i in range(nrows)]
                csv_vals = [ct["rows"][i][cj] for i in range(nrows)]
                # db 内容可解析为数值的比例
                n_db_valid = 0
                n_db_num = 0
                for v in db_vals:
                    _val, kind, _u = parse_numeric(v)
                    if kind == "missing":
                        continue
                    n_db_valid += 1
                    if kind in ("num", "thousand", "percent", "unit_suffix"):
                        n_db_num += 1
                num_ratio = (n_db_num / n_db_valid) if n_db_valid else 0.0
                trap = bool(aff == "TEXT" and n_db_valid > 0 and num_ratio >= 0.95)
                if trap:
                    numeric_in_text.append({
                        "db_column": m["db_column"], "csv_column": m["csv_column"],
                        "declared_type": decl,
                        "numeric_parse_ratio": round(num_ratio, 4),
                        "note": "SQLite 声明 TEXT 但 ≥95% 可解析为数值 → numeric-in-text 陷阱"})
                if aff == "TEXT" and not trap and num_ratio >= 0.5:
                    type_mismatches.append({
                        "db_column": m["db_column"], "csv_column": m["csv_column"],
                        "declared_type": decl,
                        "note": "TEXT 列中部分值可解析为数值(混合口径)"})
                # 逐值比较
                both_num = 0
                mismatch = 0
                text_mismatch = 0
                compared_text = 0
                ratios = []
                for a, b in zip(csv_vals, db_vals):
                    va, ka, _ = parse_numeric(a)
                    vb, kb, _ = parse_numeric(b)
                    if ka == "missing" and kb == "missing":
                        continue
                    an = ka in ("num", "thousand", "percent", "unit_suffix")
                    bn = kb in ("num", "thousand", "percent", "unit_suffix")
                    if an and bn:
                        both_num += 1
                        tol = 1e-9 * max(1.0, abs(va), abs(vb))
                        if abs(va - vb) > tol:
                            mismatch += 1
                        if va != 0 and vb != 0 and (vb / va) > 0:
                            ratios.append(vb / va)
                    elif not an and not bn:
                        compared_text += 1
                        if unicodedata.normalize("NFKC", str(a)).strip() != \
                           unicodedata.normalize("NFKC", str(b)).strip():
                            text_mismatch += 1
                    else:
                        mismatch += 1
                scale = None
                if len(ratios) >= 10:
                    med_r = quantile(sorted(ratios), 0.5)
                    for cand in SCALE_CANDIDATES:
                        if abs(med_r - cand) / cand <= 0.05:
                            scale = {"db_column": m["db_column"],
                                     "csv_column": m["csv_column"],
                                     "median_ratio_db_over_csv": fmt_num(med_r),
                                     "candidate_factor": cand,
                                     "note": "中位数比值接近 %.4g → 疑似单位/量级不一致" % cand}
                            scale_mismatches.append(scale)
                            break
                col_reports.append({
                    "db_column": m["db_column"], "csv_column": m["csv_column"],
                    "declared_type": decl, "affinity": aff,
                    "numeric_parse_ratio_db": round(num_ratio, 4),
                    "numeric_pairs_compared": both_num,
                    "numeric_mismatches": mismatch,
                    "text_pairs_compared": compared_text,
                    "text_mismatches": text_mismatch,
                    "scale_suspect": scale})
            pairs.append({
                "csv": ct["name"], "sqlite_table": st["name"],
                "csv_rows": len(ct["rows"]), "db_rows": len(st["rows"]),
                "rowcount_match": len(ct["rows"]) == len(st["rows"]),
                "rowcount_diff": len(st["rows"]) - len(ct["rows"]),
                "column_mapping": mapping,
                "column_mapping_method_summary": sorted(set(m["method"] for m in mapping)),
                "numeric_in_text_columns": numeric_in_text,
                "type_mismatches": type_mismatches,
                "scale_mismatches": scale_mismatches,
                "per_column": col_reports,
            })
    return {"pairs_compared": len(pairs), "pairs": pairs}


# ----------------------------------------------------------------------------
# 清洗规则账本 + 清洗表
# ----------------------------------------------------------------------------


def build_ledger(table, col_profiles, miss, dups, outs, cross, constraints, task_text):
    ledger = []
    rid = [0]

    def nid():
        rid[0] += 1
        return "R%03d" % rid[0]

    name = table["name"]
    # --- 缺失 ---
    mp = constraints.get("missing_policy") or "report_and_exclude"
    mp_src = "case.json" if constraints.get("missing_policy") else "default(约束缺失, 已记录)"
    total_missing = miss["total_missing"]
    if total_missing > 0:
        if mp == "drop_rows":
            drop_rows = sorted({i for col, d in miss["per_column"].items() if d["missing_count"] > 0
                                for i, r in enumerate(table["rows"])
                                if is_missing(r[table["header"].index(col)])})
            action = "drop"
        elif mp.startswith("impute"):
            drop_rows = []
            action = "recorded_only"
        else:
            drop_rows = []
            action = "report_and_exclude"
        note = ""
        if mp.startswith("impute"):
            note = "约束要求插补, 但本 Skill 默认不静默插补; 交由分析阶段执行并加标记"
        ledger.append({"rule_id": nid(), "type": "missing", "scope": name,
                       "condition": "缺失标记(%s)出现 %d 个单元格" % ("/".join(sorted(MISSING_TOKENS - {""})) or "空串", total_missing),
                       "action": action, "default_action": action,
                       "policy": mp, "policy_source": mp_src,
                       "rows_affected": cap_list(drop_rows),
                       "rows_affected_count": len(drop_rows),
                       "before": "%d 缺失单元格" % total_missing,
                       "after": "保留缺失, 下游按变量有效 n" if action == "report_and_exclude"
                                else ("删除含缺失行 %d 行" % len(drop_rows) if action == "drop" else "仅记录"),
                       "note": note})
    else:
        ledger.append({"rule_id": nid(), "type": "missing", "scope": name,
                       "condition": "全表未发现缺失标记", "action": "none",
                       "default_action": "none", "policy": mp, "policy_source": mp_src,
                       "rows_affected": [], "rows_affected_count": 0,
                       "before": "0 缺失单元格", "after": "无需处理", "note": ""})
    if task_text and re.search(r"插补|imput", task_text, re.I):
        ledger.append({"rule_id": nid(), "type": "missing", "scope": name,
                       "condition": "task.md 提及插补", "action": "recorded_only",
                       "default_action": "recorded_only",
                       "policy": "task.md mention", "policy_source": "task.md",
                       "rows_affected": [], "rows_affected_count": 0,
                       "before": "", "after": "仅记录: 插补由分析阶段决定并加标记",
                       "note": "本 Skill 不执行插补"})
    # --- 重复 ---
    dp = constraints.get("duplicate_policy") or "dedupe_exact_keep_first"
    dp_src = "case.json" if constraints.get("duplicate_policy") else "default(约束缺失, 已记录)"
    dup_idx = dups["exact_duplicate_row_indices"]
    dup_idx = [i for i in dup_idx if isinstance(i, int)]
    if dup_idx:
        if dp in ("keep_all", "report_only"):
            action, drop_set = "flag", []
        elif dp == "dedupe_exact_keep_last":
            rows = table["rows"]
            key_of = lambda r: tuple(unicodedata.normalize("NFKC", str(c)).strip() for c in r)
            seen = {}
            for i, r in enumerate(rows):
                seen.setdefault(key_of(r), []).append(i)
            drop_set = sorted(i for g in seen.values() for i in g[:-1])
            action = "drop"
            dup_idx = drop_set
        else:
            action, drop_set = "drop", dup_idx
        ledger.append({"rule_id": nid(), "type": "duplicate", "scope": name,
                       "condition": "整行完全重复(标准化后)", "action": action,
                       "default_action": action, "policy": dp, "policy_source": dp_src,
                       "rows_affected": cap_list(drop_set or dup_idx),
                       "rows_affected_count": len(drop_set or dup_idx),
                       "before": "%d 行数据" % len(table["rows"]),
                       "after": ("删除重复行 %d 行(保留首次出现)" % len(dup_idx)) if action == "drop"
                                else "仅标记不删除",
                       "note": "近似重复组 %d 个(3 位有效数字键)仅供人工核查"
                               % dups["near_group_count"]})
    else:
        ledger.append({"rule_id": nid(), "type": "duplicate", "scope": name,
                       "condition": "整行完全重复", "action": "none", "default_action": "none",
                       "policy": dp, "policy_source": dp_src,
                       "rows_affected": [], "rows_affected_count": 0,
                       "before": "%d 行" % len(table["rows"]), "after": "未发现完全重复行",
                       "note": "近似重复组 %d 个" % dups["near_group_count"]})
    # --- 类型强制(numeric-in-text 来自跨源) ---
    for pair in (cross.get("pairs") or []):
        for trap in pair.get("numeric_in_text_columns") or []:
            ledger.append({"rule_id": nid(), "type": "type_coercion",
                           "scope": "%s 列 %s" % (pair["sqlite_table"], trap["db_column"]),
                           "condition": "SQLite 声明 %s 但 %.1f%% 可解析为数值"
                                        % (trap["declared_type"], 100 * trap["numeric_parse_ratio"]),
                           "action": "coerce_on_read", "default_action": "coerce_on_read",
                           "rows_affected": [], "rows_affected_count": 0,
                           "before": "TEXT 存储数值", "after": "读取时按数值解析",
                           "note": "导出陷阱: 数值以文本落库"})
    # --- 异常值: 默认 flag, 绝不自动删除 ---
    for col, oc in (outs.get("per_column") or {}).items():
        flagged = sorted({d["row"] for d in (oc.get("extreme") or []) if isinstance(d, dict)}
                         | {d["row"] for d in (oc.get("mad_flagged") or []) if isinstance(d, dict)})
        mild = sorted({d["row"] for d in (oc.get("mild") or []) if isinstance(d, dict)})
        all_idx = sorted(set(flagged) | set(mild))
        if not all_idx:
            continue
        ledger.append({"rule_id": nid(), "type": "outlier", "scope": "%s 列 %s" % (name, col),
                       "condition": "IQR 1.5×/3× 栅栏或 |modified z|>3.5 (极端 %d, 温和 %d, MAD %d)"
                                    % (oc["extreme_count"], oc["mild_count"], oc["mad_count"]),
                       "action": "flag", "default_action": "flag",
                       "rows_affected": cap_list(all_idx),
                       "rows_affected_count": len(all_idx),
                       "before": "%d 行被标记" % len(all_idx),
                       "after": "默认仅标记保留; 可 decide 覆盖为 keep/drop",
                       "note": "绝不静默删除异常值"})
    # --- 不可能值 ---
    for iv in outs.get("impossible_values") or []:
        ledger.append({"rule_id": nid(), "type": "outlier", "scope": "%s 列 %s" % (name, iv["column"]),
                       "condition": "不可能值检查: %s (%d 个)" % (iv["check"], iv["count"]),
                       "action": "flag", "default_action": "flag",
                       "rows_affected": cap_list([e["row"] for e in iv["examples"]]),
                       "rows_affected_count": iv["count"],
                       "before": "%d 个不可能值" % iv["count"],
                       "after": "仅标记, 需人工核实", "note": "物理/逻辑不可能值"})
    # --- 单位/量级 ---
    for pair in (cross.get("pairs") or []):
        for sm in pair.get("scale_mismatches") or []:
            ledger.append({"rule_id": nid(), "type": "unit",
                           "scope": "%s vs %s 列 %s" % (pair["csv"], pair["sqlite_table"], sm["db_column"]),
                           "condition": "跨源中位数比值 ≈ %.4g" % sm["median_ratio_db_over_csv"],
                           "action": "flag", "default_action": "flag",
                           "rows_affected": [], "rows_affected_count": 0,
                           "before": "疑似量级不一致", "after": "仅标记, 需确认单位口径", "note": sm["note"]})
    return ledger


def canonical_cell(v, col_is_numeric):
    """缺失→空串; 数值列→规范数值字符串; 其他→原文"""
    val, kind, _unit = parse_numeric(v)
    if kind == "missing":
        return ""
    if col_is_numeric and kind in ("num", "thousand", "percent", "unit_suffix"):
        return repr(float(val))
    return unicodedata.normalize("NFKC", str(v)).strip()


def apply_clean(table, col_profiles, ledger):
    drop = set()
    for rule in ledger:
        if rule.get("action") == "drop":
            for i in rule.get("rows_affected") or []:
                if isinstance(i, int):
                    drop.add(i)
    numeric_col = [p["inferred_type"] in ("integer", "float") for p in col_profiles]
    out_rows = []
    for i, r in enumerate(table["rows"]):
        if i in drop:
            continue
        out_rows.append([canonical_cell(c, numeric_col[j]) for j, c in enumerate(r)])
    return out_rows


def write_cleaned_csv(path, header, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow(r)


# ----------------------------------------------------------------------------
# result.json 组装
# ----------------------------------------------------------------------------

NOT_IN_SCOPE_NOTE = "本 Skill 只负责数据质量与清洗；推断统计由分析阶段完成"


def build_descriptive(data_dictionary):
    desc = {}
    for tname, cols in data_dictionary.items():
        td = {}
        for col, d in cols.items():
            if d.get("numeric"):
                td[col] = {"type": d["inferred_type"], "n_valid": d["n_valid"],
                           "missing_count": d["missing_count"], **d["numeric"]}
            else:
                td[col] = {"type": d["inferred_type"], "n_valid": d["n_valid"],
                           "missing_count": d["missing_count"], "n_unique": d["n_unique"],
                           "top_categories": d.get("top_categories")}
        desc[tname] = td
    return desc


def build_interpretation(state):
    q = state.get("quality", {})
    miss = q.get("missing", {})
    dup = q.get("duplicates", {})
    out = q.get("outliers", {})
    cross = q.get("cross_source", {})
    parts = []
    parts.append("数据概况: 共清点 %d 个输入文件, 载入 %d 张表。"
                 % (len(state.get("source_files", [])), len(state.get("tables_summary", []))))
    if miss:
        parts.append("缺失: 全表缺失单元格 %d 个(%.2f%%), 含缺失行 %d 行; 策略为不静默插补、按变量有效 n 报告。"
                     % (miss.get("total_missing", 0), miss.get("total_missing_pct", 0.0),
                        miss.get("rows_with_missing", 0)))
    if dup:
        parts.append("重复: 完全重复行 %d 条(按约束 %s 处理), 近似重复组 %d 个待人工核查。"
                     % (dup.get("exact_removed", 0),
                        state.get("constraints_applied", {}).get("duplicate_policy", "默认"),
                        dup.get("near_group_count", 0)))
    if out and out.get("per_column"):
        tot = sum(c.get("extreme_count", 0) + c.get("mild_count", 0)
                  for c in out["per_column"].values())
        parts.append("异常值: 各数值列合计 %d 个 IQR 栅栏标记(默认仅标记不删除); 不可能值检查 %d 项。"
                     % (tot, len(out.get("impossible_values", []))))
    if cross and cross.get("pairs"):
        p0 = cross["pairs"][0]
        parts.append("跨源一致性: 比较 %d 对 CSV↔SQLite 表; 行数%s一致; numeric-in-text 列 %d 个; 疑似量级不一致 %d 个。"
                     % (cross["pairs_compared"], "" if p0["rowcount_match"] else "不",
                        len(p0.get("numeric_in_text_columns", [])),
                        len(p0.get("scale_mismatches", []))))
    parts.append("说明: 本结果仅覆盖数据质量与清洗口径; 假设检验/效应量/置信区间不在本 Skill 范围, "
                 "请将 cleaned_table.csv 与数据字典交给推断统计阶段。")
    return "".join(parts)


def build_result(state):
    required = state.get("required_fields") or []
    result = {
        "case_id": state.get("case_id", "unknown-case"),
        "status": state.get("status", "partial"),
        "generated_at": now_iso(),
        "skill": "data-hygiene-audit (bundle2)",
        "source_files": [{"name": f["name"], "sha256": f["sha256"], "size": f["size"],
                          "format": f["format"]} for f in state.get("source_files", [])],
        "data_dictionary": state.get("data_dictionary", {}),
        "quality": state.get("quality", {}),
        "cleaning_ledger": state.get("cleaning_ledger", []),
        "constraints_applied": state.get("constraints_applied", {}),
        "agent_decisions": state.get("agent_decisions", []),
        "exceptions": state.get("exceptions", []),
        "uncertainty": state.get("uncertainty", []),
        "limitations": state.get("limitations", []),
        "cleaned_table": "cleaned_table.csv",
    }
    # 合同字段映射
    for f in required:
        if f in result:
            continue
        if f == "cleaning":
            result["cleaning"] = {
                "quality": result["quality"],
                "cleaning_ledger": result["cleaning_ledger"],
                "cleaned_table": "cleaned_table.csv",
                "summary": "缺失/重复/异常值/跨源一致性审计与确定性清洗",
            }
        elif f == "descriptive":
            result["descriptive"] = build_descriptive(result["data_dictionary"])
        elif f in ("hypothesis_test", "effect_size", "confidence_interval"):
            result[f] = {"status": "not_in_scope", "note": NOT_IN_SCOPE_NOTE}
        elif f == "interpretation":
            result["interpretation"] = build_interpretation(state)
        elif f == "limitations":
            result["limitations"] = result.get("limitations", [])
        elif f == "status":
            result["status"] = state.get("status", "partial")
        else:
            result[f] = {"status": "not_in_scope",
                         "note": "该字段不在数据清洗 Skill 范围内, 由分析阶段补齐"}
    if "interpretation" not in result:
        result["interpretation"] = build_interpretation(state)
    return result


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)


# ----------------------------------------------------------------------------
# 报告
# ----------------------------------------------------------------------------


def write_audit_report(path, state, result):
    L = []
    A = L.append
    q = result.get("quality", {})
    A("# 数据质量审计报告（bundle2: data-hygiene-audit）")
    A("")
    A("- 案例: `%s`" % result.get("case_id"))
    A("- 状态: **%s**" % result.get("status"))
    A("- 生成时间: %s" % result.get("generated_at"))
    A("")
    A("## 1. 数据概况")
    A("")
    A("| 文件 | 大小(B) | 格式 | sha256(前12) |")
    A("|---|---|---|---|")
    for f in result.get("source_files", []):
        A("| %s | %d | %s | %s |" % (f["name"], f["size"], f["format"], f["sha256"][:12]))
    A("")
    for ts in state.get("tables_summary", []):
        A("- 表 `%s`: %d 行 × %d 列, 编码 %s, 分隔符 %s, 表头: %s"
          % (ts["name"], ts["rows"], ts["cols"], ts.get("encoding", "?"),
             ts.get("delimiter", "?"), "有" if ts.get("has_header") else "无(自动命名 col_1..col_N)"))
    A("")
    A("## 2. 质量审计")
    A("")
    miss = q.get("missing", {})
    A("### 2.1 缺失")
    A("")
    A("- 缺失单元格合计 %d / %d (%.2f%%), 含缺失行 %d 行。"
      % (miss.get("total_missing", 0), miss.get("total_cells", 0),
         miss.get("total_missing_pct", 0.0), miss.get("rows_with_missing", 0)))
    if miss.get("per_column") and any(d["missing_count"] > 0 for d in miss["per_column"].values()):
        A("")
        A("| 列 | 缺失数 | 缺失率% |")
        A("|---|---|---|")
        for col, d in miss["per_column"].items():
            if d["missing_count"] > 0:
                A("| %s | %d | %.2f |" % (col, d["missing_count"], d["missing_pct"]))
    for note in miss.get("heuristic_notes", []):
        A("- ⚠ " + note)
    A("")
    dup = q.get("duplicates", {})
    A("### 2.2 重复")
    A("")
    A("- 完全重复行: **%d** 条（索引见 result.json）; 近似重复组(3 位有效数字键): %d 个。"
      % (dup.get("exact_removed", 0), dup.get("near_group_count", 0)))
    A("")
    out = q.get("outliers", {})
    A("### 2.3 异常值与不可能值")
    A("")
    if out.get("per_column"):
        A("| 列 | Q1 | Q3 | IQR | 温和(1.5×) | 极端(3×) | MAD |z|>3.5 |")
        A("|---|---|---|---|---|---|---|")
        for col, oc in out["per_column"].items():
            A("| %s | %s | %s | %s | %d | %d | %d |"
              % (col, oc.get("q1"), oc.get("q3"), oc.get("iqr"),
                 oc.get("mild_count", 0), oc.get("extreme_count", 0), oc.get("mad_count", 0)))
        A("")
        if out.get("group_column_used"):
            A("- 分组内异常值使用分组列: `%s`（详见 result.json）" % out["group_column_used"])
    for iv in out.get("impossible_values", []):
        A("- ⚠ 不可能值: 列 `%s` 检查 `%s` 命中 %d 个。" % (iv["column"], iv["check"], iv["count"]))
    A("")
    cross = q.get("cross_source", {})
    A("### 2.4 跨源一致性 (CSV ↔ SQLite)")
    A("")
    if not cross.get("pairs"):
        A("- 未找到可配对的 CSV/SQLite 表（形状不一致或仅单一来源）。")
    for p in cross.get("pairs", []):
        A("- 配对 `%s` ↔ `%s`: 行数 %d vs %d（%s）; 列映射方式: %s。"
          % (p["csv"], p["sqlite_table"], p["csv_rows"], p["db_rows"],
             "一致" if p["rowcount_match"] else "不一致(diff=%d)" % p["rowcount_diff"],
             "/".join(p["column_mapping_method_summary"])))
        for t in p.get("numeric_in_text_columns", []):
            A("  - ⚠ numeric-in-text: `%s` 声明 %s, %.1f%% 可解析为数值"
              % (t["db_column"], t["declared_type"], 100 * t["numeric_parse_ratio"]))
        for s in p.get("scale_mismatches", []):
            A("  - ⚠ 量级疑点: `%s` vs `%s` 中位数比值 ≈ %s（候选因子 %s）"
              % (s["db_column"], s["csv_column"], s["median_ratio_db_over_csv"], s["candidate_factor"]))
        mism = [(c["db_column"], c["numeric_mismatches"]) for c in p.get("per_column", [])
                if c.get("numeric_mismatches")]
        if mism:
            A("  - 数值不一致单元格: %s" % "; ".join("%s:%d" % m for m in mism))
    A("")
    A("## 3. 清洗规则与影响（清洗账本）")
    A("")
    A("| 规则 | 类型 | 范围 | 动作 | 影响行数 | 说明 |")
    A("|---|---|---|---|---|---|")
    for r in result.get("cleaning_ledger", []):
        A("| %s | %s | %s | %s | %d | %s |"
          % (r["rule_id"], r["type"], r["scope"], r.get("action"),
             r.get("rows_affected_count", 0), (r.get("after") or "").replace("|", "/")))
    A("")
    A("清洗后表已写入 `cleaned_table.csv`（/app/work 与 /app/output 各一份）。")
    A("")
    A("## 4. 口径与单位")
    A("")
    for h in state.get("unit_hints", []):
        if not (h.get("name_tokens") or h.get("value_unit_hint") or h.get("plausibility")):
            continue
        pl = h.get("plausibility")
        pl_txt = ""
        if pl:
            pl_txt = "；合理区间 %s（观测 %s–%s，%s）" % (
                pl["expected_range"], h.get("observed_min"), h.get("observed_max"),
                "范围内" if pl["within_range"] else "⚠超出提示范围")
        A("- 列 `%s`: 名称单位词 %s；值内单位 %s；观测范围 %s–%s%s"
          % (h["column"], h.get("name_tokens") or "无", h.get("value_unit_hint") or "无",
             h.get("observed_min"), h.get("observed_max"), pl_txt))
    A("")
    A("## 5. 遗留问题与不确定性")
    A("")
    for u in result.get("uncertainty", []):
        A("- " + u)
    for lim in result.get("limitations", []):
        A("- " + lim)
    A("")
    A("## 6. 范围声明")
    A("")
    A("本报告只覆盖数据质量与清洗。**假设检验、效应量、置信区间不在本 Skill 范围内**；"
      "请将 `cleaned_table.csv`、数据字典与本账本移交推断统计阶段。")
    A("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))


def write_digest(path, state, result, work_dir="/app/work", output_dir="/app/output"):
    L = []
    A = L.append
    q = result.get("quality", {})
    A("# 审计摘要 (digest) — 供 agent 快速阅读")
    A("")
    A("case_id: `%s` | status: **%s**" % (result.get("case_id"), result.get("status")))
    A("")
    A("## 发现了什么")
    A("")
    miss = q.get("missing", {})
    dup = q.get("duplicates", {})
    out = q.get("outliers", {})
    cross = q.get("cross_source", {})
    A("- 缺失: %d 单元格 (%.2f%%), 含缺失行 %d 行。"
      % (miss.get("total_missing", 0), miss.get("total_missing_pct", 0.0),
         miss.get("rows_with_missing", 0)))
    A("- 完全重复行: %d 条; 近似重复组: %d 个。" % (dup.get("exact_removed", 0), dup.get("near_group_count", 0)))
    tot_out = sum(c.get("extreme_count", 0) + c.get("mild_count", 0)
                  for c in (out.get("per_column") or {}).values())
    A("- 异常值标记: 合计 %d 个（默认 flag, 未删除）; 不可能值检查 %d 项。"
      % (tot_out, len(out.get("impossible_values", []))))
    if cross.get("pairs"):
        p0 = cross["pairs"][0]
        A("- 跨源: 行数%s; numeric-in-text %d 列; 量级疑点 %d 个; 列映射 %s。"
          % ("一致" if p0["rowcount_match"] else "不一致",
             len(p0.get("numeric_in_text_columns", [])),
             len(p0.get("scale_mismatches", [])),
             "/".join(p0["column_mapping_method_summary"])))
    else:
        A("- 跨源: 无可配对表。")
    A("")
    A("- 清洗表: `%s/cleaned_table.csv`; 完整报告: `%s/audit_report.md`; "
      "结构化结果: `%s/result.json`。" % (output_dir, output_dir, output_dir))
    A("")
    A("## 需要你确认的决策（至多 5 条，默认值已应用）")
    A("")
    A("| # | rule_id | 事项 | 默认动作 | 覆盖命令 |")
    A("|---|---|---|---|---|")
    decisions = []
    for r in result.get("cleaning_ledger", []):
        if r["type"] == "duplicate" and r.get("rows_affected_count"):
            decisions.append((r["rule_id"], "完全重复行 %d 条" % r["rows_affected_count"], r["action"]))
        elif r["type"] == "outlier" and r.get("rows_affected_count"):
            decisions.append((r["rule_id"], "异常值: %s（%d 行）" % (r["scope"], r["rows_affected_count"]), r["action"]))
        elif r["type"] == "type_coercion":
            decisions.append((r["rule_id"], "类型口径: %s" % r["scope"], r["action"]))
        elif r["type"] == "unit":
            decisions.append((r["rule_id"], "单位量级: %s" % r["scope"], r["action"]))
        elif r["type"] == "missing" and r.get("action") not in ("none",):
            decisions.append((r["rule_id"], "缺失策略: %s" % r["condition"][:60], r["action"]))
    decisions = decisions[:5]
    if not decisions:
        A("| - | - | 无待确认事项 | - | - |")
    for i, (rid, what, act) in enumerate(decisions, 1):
        A('| %d | %s | %s | %s | `python3 scripts/audit.py decide --rule %s --action keep\\|drop\\|flag --reason "..."` |'
          % (i, rid, what, act, rid))
    A("")
    A("确认完毕后运行: `python3 scripts/audit.py finalize --work-dir %s --output-dir %s`"
      % (work_dir, output_dir))
    A("")
    A("## 范围提醒")
    A("")
    A("本 Skill **不做**假设检验/效应量/置信区间。清洗后的表 + 数据字典可直接移交推断统计阶段。")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))


# ----------------------------------------------------------------------------
# run 主流程
# ----------------------------------------------------------------------------


def cmd_run(args):
    input_dir = args.input_dir
    work_dir = args.work_dir
    output_dir = args.output_dir
    os.makedirs(work_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    exceptions = []
    state = {"case_id": "unknown-case", "status": "partial",
             "required_fields": [], "source_files": [], "exceptions": exceptions,
             "uncertainty": [], "limitations": [], "agent_decisions": [],
             "constraints_applied": {}, "tables_summary": [], "unit_hints": [],
             "quality": {}, "data_dictionary": {}, "cleaning_ledger": []}

    # --- case.json ---
    case_path = os.path.join(input_dir, "case.json")
    case = {}
    if os.path.isfile(case_path):
        try:
            with open(case_path, "r", encoding="utf-8") as f:
                case = json.load(f)
        except (OSError, ValueError) as e:
            exceptions.append({"stage": "case.json", "message": "解析失败: %s" % e})
    else:
        exceptions.append({"stage": "case.json", "message": "未找到 case.json, 使用默认口径"})
        state["uncertainty"].append("缺少 case.json: case_id 与 output_contract 未知, 已按默认字段输出")
    state["case_id"] = case.get("case_id", "unknown-case")
    oc = case.get("output_contract") or {}
    state["required_fields"] = list(oc.get("required_fields") or [])
    constraints = dict(case.get("constraints") or {})
    state["constraints_applied"] = {
        "alpha": constraints.get("alpha", "未指定"),
        "missing_policy": constraints.get("missing_policy", "report_and_exclude(default)"),
        "duplicate_policy": constraints.get("duplicate_policy", "dedupe_exact_keep_first(default)"),
        "raw": constraints,
    }
    for k in ("missing_policy", "duplicate_policy"):
        if k not in constraints:
            state["uncertainty"].append("case.json 未提供约束 %s, 使用默认值" % k)

    # --- task.md ---
    task_text = ""
    task_path = os.path.join(input_dir, "task.md")
    if os.path.isfile(task_path):
        try:
            with open(task_path, "r", encoding="utf-8") as f:
                task_text = f.read()
        except OSError as e:
            exceptions.append({"stage": "task.md", "message": str(e)})

    # --- 清点 ---
    files = inventory(input_dir, exceptions)
    state["source_files"] = files

    # --- 载入表 ---
    csv_tables = []
    sqlite_tables = []
    for f in files:
        fmt = f["format"]
        if fmt in ("csv", "tsv", "csv-like"):
            try:
                t = load_csv_table(f["path"], f["name"], exceptions)
                if t:
                    csv_tables.append(t)
            except Exception as e:
                exceptions.append({"stage": "load_csv", "message": "%s: %s" % (f["name"], e)})
        elif fmt == "sqlite":
            try:
                sqlite_tables.extend(load_sqlite_tables(f["path"], f["name"], exceptions))
            except Exception as e:
                exceptions.append({"stage": "load_sqlite", "message": "%s: %s" % (f["name"], e)})

    all_tables = csv_tables + sqlite_tables
    if not all_tables:
        state["status"] = "partial"
        exceptions.append({"stage": "load", "message": "未发现任何可分析的数据表(CSV/SQLite)"})
        state["limitations"].append("输入目录中没有可识别的数据表")
        _finalize_run(state, None, [], None, work_dir, output_dir, task_text)
        return 0

    # --- 主表选择: 行数最多的 CSV 表, 否则最大 SQLite 表 ---
    if csv_tables:
        primary = max(csv_tables, key=lambda t: len(t["rows"]))
    else:
        primary = max(sqlite_tables, key=lambda t: len(t["rows"]))
        state["uncertainty"].append("无 CSV 快照, 主表取自 SQLite; 跨源一致性检查不可用")

    # --- 逐表剖析 ---
    data_dictionary = {}
    analyses = {}
    for t in all_tables:
        cols_profiles = []
        cols_vals = list(zip(*t["rows"])) if t["rows"] else [[] for _ in t["header"]]
        for j, col in enumerate(t["header"]):
            prof, _nums = analyze_column(list(cols_vals[j]) if j < len(cols_vals) else [])
            prof["name"] = col
            uh_name = unit_hint_from_name(col)
            prof["unit_hint"] = prof.get("unit_hint_values") or ("/".join(uh_name) if uh_name else None)
            if t.get("declared_types"):
                prof["declared_type"] = t["declared_types"].get(col)
                prof["declared_affinity"] = t.get("declared_affinity", {}).get(col)
            cols_profiles.append(prof)
        data_dictionary[t["name"]] = {p["name"]: {k: v for k, v in p.items()
                                                  if k != "missing_row_indices" or v}
                                      for p in cols_profiles}
        analyses[t["name"]] = cols_profiles
        state["data_dictionary"] = data_dictionary
        state["tables_summary"].append({
            "name": t["name"], "rows": len(t["rows"]), "cols": len(t["header"]),
            "encoding": t.get("encoding"), "delimiter": t.get("delimiter"),
            "has_header": t.get("has_header"), "format": t["format"]})

    primary_prof = analyses[primary["name"]]

    # --- 质量分析(主表) ---
    miss = missingness_analysis(primary, primary_prof)
    dups = duplicates_analysis(primary, primary_prof)
    outs = outliers_analysis(primary, primary_prof)
    state["unit_hints"] = unit_scale_hints(primary, primary_prof)

    # --- 跨源一致性 ---
    if csv_tables and sqlite_tables:
        try:
            cross = cross_source_consistency(csv_tables, sqlite_tables)
        except Exception as e:
            exceptions.append({"stage": "cross_source", "message": str(e)})
            cross = {"pairs_compared": 0, "pairs": []}
    else:
        cross = {"pairs_compared": 0, "pairs": []}
        if sqlite_tables and not csv_tables:
            pass
        elif csv_tables and not sqlite_tables:
            state["uncertainty"].append("无 SQLite 源, 跨源一致性检查跳过")

    state["quality"] = {"primary_table": primary["name"],
                        "missing": miss, "duplicates": dups,
                        "outliers": outs, "cross_source": cross,
                        "unit_scale_hints": state["unit_hints"]}

    # --- 账本 + 清洗表 ---
    constraints_for_ledger = dict(constraints)
    ledger = build_ledger(primary, primary_prof, miss, dups, outs, cross,
                          constraints_for_ledger, task_text)
    state["cleaning_ledger"] = ledger

    _finalize_run(state, primary, primary_prof, all_tables, work_dir, output_dir, task_text)
    return 0


def _finalize_run(state, primary, primary_prof, all_tables, work_dir, output_dir, task_text):
    """run 的收尾: 应用清洗、写全部产物。primary 为 None 表示无数据。"""
    # 读入已存 decisions
    decisions = []
    dec_path = os.path.join(work_dir, "decisions.json")
    if os.path.isfile(dec_path):
        try:
            with open(dec_path, "r", encoding="utf-8") as f:
                decisions = json.load(f)
        except (OSError, ValueError):
            decisions = []
    state["agent_decisions"] = decisions
    # 应用 decisions 到账本
    for d in decisions:
        for rule in state.get("cleaning_ledger", []):
            if rule["rule_id"] == d.get("rule"):
                rule["action"] = d.get("action", rule["action"])
                rule["override_reason"] = d.get("reason", "")
                rule["decided_by"] = "agent"
    if primary is not None and primary_prof is not None:
        cleaned_rows = apply_clean(primary, primary_prof, state["cleaning_ledger"])
        header = primary["header"]
        for out_dir in {work_dir, output_dir}:
            try:
                write_cleaned_csv(os.path.join(out_dir, "cleaned_table.csv"), header, cleaned_rows)
            except OSError as e:
                state["exceptions"].append({"stage": "write_cleaned", "message": str(e)})
        state["cleaned_rows"] = len(cleaned_rows)
        if not state.get("exceptions"):
            state["status"] = "complete"
    else:
        state["cleaned_rows"] = 0
    state["uncertainty"].extend([
        "表头探测为启发式(首行非数值比例), 无表头文件已命名为 col_1..col_N",
        "近似重复使用 3 位有效数字键, 仅供人工核查, 未自动删除",
        "跨源逐值比对假设两源行序一致",
        "异常值默认仅标记(flag), 是否剔除需 agent 用 decide 确认",
    ])
    state["limitations"].extend([
        "本 Skill 不执行假设检验/效应量/置信区间, 推断统计由分析阶段完成",
        "不静默插补; 缺失值保留, 下游统计使用各变量有效 n",
        "合理性区间仅为提示, 不构成硬拒绝",
    ])
    result = build_result(state)
    try:
        write_json(os.path.join(output_dir, "result.json"), result)
    except OSError as e:
        state["exceptions"].append({"stage": "write_result", "message": str(e)})
    try:
        write_audit_report(os.path.join(output_dir, "audit_report.md"), state, result)
    except OSError as e:
        state["exceptions"].append({"stage": "write_report", "message": str(e)})
    try:
        write_digest(os.path.join(work_dir, "digest.md"), state, result, work_dir, output_dir)
    except OSError as e:
        state["exceptions"].append({"stage": "write_digest", "message": str(e)})
    # 保存 state 供 finalize 使用(去除大对象, 保留主表)
    slim = dict(state)
    if primary is not None:
        slim["_primary_table"] = {"name": primary["name"], "header": primary["header"],
                                  "rows": primary["rows"], "format": primary["format"]}
        slim["_primary_profiles"] = [{"inferred_type": p["inferred_type"]} for p in (primary_prof or [])]
    try:
        write_json(os.path.join(work_dir, "state.json"), slim)
    except (OSError, TypeError, ValueError) as e:
        print("WARN: state.json 保存失败: %s" % e)
    # NEXT 提示
    print("AUDIT DONE: status=%s case_id=%s" % (result.get("status"), result.get("case_id")))
    print("NEXT: 先阅读 %s" % os.path.join(work_dir, "digest.md"))
    print("NEXT: 确认/覆盖决策: python3 scripts/audit.py decide --rule <rule_id> --action keep|drop|flag --reason \"...\"")
    print("NEXT: 然后运行: python3 scripts/audit.py finalize --work-dir %s --output-dir %s"
          % (work_dir, output_dir))
    print("NEXT: 结果文件: %s" % os.path.join(output_dir, "result.json"))


# ----------------------------------------------------------------------------
# decide / finalize
# ----------------------------------------------------------------------------


def cmd_decide(args):
    work_dir = args.work_dir
    os.makedirs(work_dir, exist_ok=True)
    dec_path = os.path.join(work_dir, "decisions.json")
    decisions = []
    if os.path.isfile(dec_path):
        try:
            with open(dec_path, "r", encoding="utf-8") as f:
                decisions = json.load(f)
        except (OSError, ValueError):
            decisions = []
    decisions.append({"rule": args.rule, "action": args.action,
                      "reason": args.reason or "", "time": now_iso()})
    write_json(dec_path, decisions)
    print("DECIDE RECORDED: %s -> %s (%s)" % (args.rule, args.action, args.reason or "-"))
    print("NEXT: python3 scripts/audit.py finalize --work-dir %s --output-dir %s"
          % (work_dir, args.output_dir))
    return 0


def cmd_finalize(args):
    work_dir = args.work_dir
    output_dir = args.output_dir
    state_path = os.path.join(work_dir, "state.json")
    if not os.path.isfile(state_path):
        # 无先前状态: 写一个诚实的失败结果
        result = {"case_id": "unknown-case", "status": "failed",
                  "exceptions": [{"stage": "finalize",
                                  "message": "未找到 %s; 请先运行 run 子命令" % state_path}],
                  "uncertainty": [], "limitations": [],
                  "cleaning_ledger": [], "quality": {}, "data_dictionary": {},
                  "source_files": [], "constraints_applied": {}, "agent_decisions": []}
        os.makedirs(output_dir, exist_ok=True)
        write_json(os.path.join(output_dir, "result.json"), result)
        print("FINALIZE FAILED: 无 state.json")
        print("NEXT: 先运行 python3 scripts/audit.py run --input-dir /input --work-dir %s --output-dir %s"
              % (work_dir, output_dir))
        return 1
    with open(state_path, "r", encoding="utf-8") as f:
        state = json.load(f)
    os.makedirs(output_dir, exist_ok=True)
    # 重建主表
    pt = state.pop("_primary_table", None)
    pp = state.pop("_primary_profiles", None)
    primary = None
    primary_prof = None
    if pt:
        primary = {"name": pt["name"], "header": pt["header"], "rows": pt["rows"],
                   "format": pt.get("format", "csv")}
        primary_prof = pp or [{"inferred_type": "text"} for _ in pt["header"]]
    _finalize_run(state, primary, primary_prof, None, work_dir, output_dir, "")
    return 0


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------


def main(argv=None):
    ap = argparse.ArgumentParser(prog="audit.py", description="数据卫生审计 (stdlib only)")
    sub = ap.add_subparsers(dest="cmd")

    p_run = sub.add_parser("run", help="运行完整审计")
    p_run.add_argument("--input-dir", default="/input")
    p_run.add_argument("--work-dir", default="/app/work")
    p_run.add_argument("--output-dir", default="/app/output")

    p_dec = sub.add_parser("decide", help="记录一条人工决策")
    p_dec.add_argument("--rule", required=True)
    p_dec.add_argument("--action", required=True, choices=["keep", "drop", "flag"])
    p_dec.add_argument("--reason", default="")
    p_dec.add_argument("--work-dir", default="/app/work")
    p_dec.add_argument("--output-dir", default="/app/output")

    p_fin = sub.add_parser("finalize", help="合并人工决策并重写 result.json")
    p_fin.add_argument("--work-dir", default="/app/work")
    p_fin.add_argument("--output-dir", default="/app/output")

    args = ap.parse_args(argv)
    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd == "decide":
        return cmd_decide(args)
    if args.cmd == "finalize":
        return cmd_finalize(args)
    ap.print_help()
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        tb = traceback.format_exc()
        # 最后的兜底: 仍尝试写出诚实的 result.json
        out_dir = "/app/output"
        for a in (sys.argv or []):
            pass
        if "--output-dir" in sys.argv:
            i = sys.argv.index("--output-dir")
            if i + 1 < len(sys.argv):
                out_dir = sys.argv[i + 1]
        try:
            os.makedirs(out_dir, exist_ok=True)
            write_json(os.path.join(out_dir, "result.json"), {
                "case_id": "unknown-case", "status": "failed",
                "exceptions": [{"stage": "main", "message": "未捕获异常", "traceback": tb}],
                "uncertainty": ["脚本在入口处崩溃, 结果不完整"],
                "limitations": ["无法完成审计"],
                "cleaning_ledger": [], "quality": {}, "data_dictionary": {},
                "source_files": [], "constraints_applied": {}, "agent_decisions": []})
            print("FATAL: 未捕获异常, 已写出 status=failed 的 result.json 到 %s" % out_dir)
        except Exception:
            print("FATAL: 未捕获异常且无法写 result.json:\n%s" % tb)
        print("NEXT: 请检查输入目录与权限后重试 run 子命令")
        sys.exit(1)
