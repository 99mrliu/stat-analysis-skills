#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""statslib — 纯标准库统计推断库（infer-engine Skill 核心）

所有函数仅依赖 Python 标准库；若 scipy 可用，infer.py 会用它做正态性检验
（Shapiro-Wilk），否则退回 D'Agostino K^2 检验（n>=8）。

分布函数基于正则化不完全 Beta/Gamma 函数（Numerical Recipes 算法），
经 scipy 交叉校验（见 tests/crosscheck）。
"""
import math
import random
from statistics import mean as _mean

# ---------------------------------------------------------------- 基础统计

def n_valid(xs):
    return sum(1 for x in xs if x is not None)

def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else float("nan")

def variance(xs, sample=True):
    xs = [x for x in xs if x is not None]
    n = len(xs)
    if n < (2 if sample else 1):
        return float("nan")
    m = sum(xs) / n
    ss = sum((x - m) ** 2 for x in xs)
    return ss / (n - 1) if sample else ss / n

def sd(xs, sample=True):
    v = variance(xs, sample)
    return math.sqrt(v) if v == v else v

def median(xs):
    xs = sorted(x for x in xs if x is not None)
    n = len(xs)
    if n == 0:
        return float("nan")
    mid = n // 2
    if n % 2:
        return xs[mid]
    return (xs[mid - 1] + xs[mid]) / 2.0

def quantile(xs, q):
    """线性插值分位数，q in [0,1]（R 默认 type-7 口径）。"""
    xs = sorted(x for x in xs if x is not None)
    n = len(xs)
    if n == 0:
        return float("nan")
    if n == 1:
        return xs[0]
    pos = q * (n - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return xs[lo] + frac * (xs[hi] - xs[lo])

def skewness(xs):
    xs = [x for x in xs if x is not None]
    n = len(xs)
    if n < 3:
        return float("nan")
    m = sum(xs) / n
    m2 = sum((x - m) ** 2 for x in xs) / n
    m3 = sum((x - m) ** 3 for x in xs) / n
    if m2 <= 0:
        return 0.0
    return m3 / (m2 ** 1.5)

def kurtosis_excess(xs):
    xs = [x for x in xs if x is not None]
    n = len(xs)
    if n < 4:
        return float("nan")
    m = sum(xs) / n
    m2 = sum((x - m) ** 2 for x in xs) / n
    m4 = sum((x - m) ** 4 for x in xs) / n
    if m2 <= 0:
        return 0.0
    return m4 / (m2 ** 2) - 3.0

def mad(xs):
    """中位数绝对偏差（乘以 1.4826 后为 SD 的稳健估计）。"""
    xs = [x for x in xs if x is not None]
    if not xs:
        return float("nan")
    med = median(xs)
    return median([abs(x - med) for x in xs])

def rankdata(xs):
    """平均秩（ties 取平均），返回与 xs 等长的秩列表；None 保持 None。"""
    idx = [i for i, x in enumerate(xs) if x is not None]
    vals = sorted((xs[i], i) for i in idx)
    ranks = [None] * len(xs)
    k = 0
    while k < len(vals):
        j = k
        while j + 1 < len(vals) and vals[j + 1][0] == vals[k][0]:
            j += 1
        avg = (k + j) / 2.0 + 1.0
        for t in range(k, j + 1):
            ranks[vals[t][1]] = avg
        k = j + 1
    return ranks

# ---------------------------------------------------------------- 特殊函数

def _betacf(a, b, x):
    MAXIT, EPS, FPMIN = 200, 3e-14, 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < FPMIN:
        d = FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        de = d * c
        h *= de
        if abs(de - 1.0) < EPS:
            break
    return h

def betai(a, b, x):
    """正则化不完全 Beta 函数 I_x(a,b)。"""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    bt = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
                  + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b

def _gammq(a, x):
    """正则化上不完全 Gamma Q(a,x)（级数/连分式，Numerical Recipes）。"""
    if x < 0 or a <= 0:
        return float("nan")
    if x == 0:
        return 1.0
    if x < a + 1.0:
        # 级数计算 P，再返回 1-P
        ap, s, delta = a, 1.0 / a, 1.0 / a
        for _ in range(1000):
            ap += 1.0
            delta *= x / ap
            s += delta
            if abs(delta) < abs(s) * 3e-14:
                break
        return 1.0 - s * math.exp(-x + a * math.log(x) - math.lgamma(a))
    b, c, d = x + 1.0 - a, 1e300, 1.0 / (x + 1.0 - a)
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < 1e-300:
            d = 1e-300
        c = b + an / c
        if abs(c) < 1e-300:
            c = 1e-300
        d = 1.0 / d
        h *= d * c
        if abs(d * c - 1.0) < 3e-14:
            break
    return math.exp(-x + a * math.log(x) - math.lgamma(a)) * h

# ---------------------------------------------------------------- 分布函数

def t_cdf(t, df):
    x = df / (df + t * t)
    ib = betai(df / 2.0, 0.5, x)
    return 1.0 - 0.5 * ib if t >= 0 else 0.5 * ib

def t_ppf(p, df):
    if p <= 0:
        return -1e12
    if p >= 1:
        return 1e12
    lo, hi = -1e6, 1e6
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if t_cdf(mid, df) < p:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)

def f_cdf(f, d1, d2):
    if f <= 0:
        return 0.0
    return betai(d1 / 2.0, d2 / 2.0, d1 * f / (d1 * f + d2))

def f_ppf(p, d1, d2):
    if p <= 0:
        return 0.0
    if p >= 1:
        return 1e12
    lo, hi = 0.0, 1e6
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if f_cdf(mid, d1, d2) < p:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)

def chi2_cdf(x, df):
    if x <= 0:
        return 0.0
    return 1.0 - _gammq(df / 2.0, x / 2.0)

def chi2_ppf(p, df):
    if p <= 0:
        return 0.0
    if p >= 1:
        return 1e12
    lo, hi = 0.0, 1e6
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if chi2_cdf(mid, df) < p:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)

def norm_cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))

def norm_ppf(p):
    if p <= 0:
        return -1e12
    if p >= 1:
        return 1e12
    lo, hi = -12.0, 12.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if norm_cdf(mid) < p:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)

# ---------------------------------------------------------------- 假设检验

def dagostino_k2(xs):
    """D'Agostino K^2 正态性综合检验（基于偏度与峰度），n>=8 有效。

    返回 (K2, p)；n<8 时返回 (None, None)。
    """
    xs = [x for x in xs if x is not None]
    n = len(xs)
    if n < 8:
        return None, None
    g1 = skewness(xs)
    g2 = kurtosis_excess(xs) + 3.0  # Anscombe–Glynn 口径：非超额峰度
    y = g1 * math.sqrt(((n + 1) * (n + 3)) / (6.0 * (n - 2)))
    b2 = 3.0 * (n ** 2 + 27 * n - 70) * (n + 1) * (n + 3) / \
        ((n - 2.0) * (n + 5) * (n + 7) * (n + 9))
    w2 = -1.0 + math.sqrt(2.0 * (b2 - 1.0))
    delta = 1.0 / math.sqrt(0.5 * math.log(w2))
    a = math.sqrt(2.0 / (w2 - 1.0))
    z1 = delta * math.log(y / a + math.sqrt((y / a) ** 2 + 1.0))
    E = 3.0 * (n - 1) / (n + 1)
    var2 = 24.0 * n * (n - 2) * (n - 3) / ((n + 1.0) ** 2 * (n + 3) * (n + 5))
    x2 = (g2 - E) / math.sqrt(var2)
    b = (6.0 * (n * n - 5 * n + 2)) / ((n + 7.0) * (n + 9.0)) * \
        math.sqrt((6.0 * (n + 3) * (n + 5)) / (n * (n - 2) * (n - 3)))
    A = 6.0 + 8.0 / b * (2.0 / b + math.sqrt(1.0 + 4.0 / (b * b)))
    if A <= 4.0:
        return None, None  # 样本量过小，K^2 峰度变换不可用
    denom = 1.0 + x2 * math.sqrt(2.0 / (A - 4.0))
    if denom == 0.0:
        return None, None
    term2 = (1.0 if denom > 0 else -1.0) * (abs(1.0 - 2.0 / A) / abs(denom)) ** (1.0 / 3.0)
    z2 = (1.0 - 2.0 / (9.0 * A) - term2) / math.sqrt(2.0 / (9.0 * A))
    K2 = z1 * z1 + z2 * z2
    p = 1.0 - chi2_cdf(K2, 2)
    return K2, p

def levene_test(groups, center="median"):
    """Levene/Brown-Forsythe 方差齐性检验。groups 为 >=2 组样本列表。
    返回 (W, df1, df2, p)。"""
    k = len(groups)
    zs = []
    for g in groups:
        if center == "median":
            c = median(g)
        else:
            c = mean(g)
        zs.append([abs(x - c) for x in g if x is not None])
    ni = [len(z) for z in zs]
    N = sum(ni)
    if k < 2 or N <= k:
        return None, None, None, None
    zbari = [sum(z) / n for z, n in zip(zs, ni)]
    zbar = sum(sum(z) for z in zs) / N
    num = sum(n * (zb - zbar) ** 2 for n, zb in zip(ni, zbari))
    den = sum(sum((z - zb) ** 2 for z in zz) for zz, zb in zip(zs, zbari))
    df1 = k - 1
    df2 = N - k
    if den <= 0:
        return 0.0, df1, df2, 1.0
    W = (N - k) * num / (df1 * den)
    p = 1.0 - f_cdf(W, df1, df2)
    return W, df1, df2, p

def ttest_student(x, y):
    n1, n2 = len(x), len(y)
    m1, m2 = mean(x), mean(y)
    v1, v2 = variance(x), variance(y)
    sp2 = ((n1 - 1) * v1 + (n2 - 1) * v2) / (n1 + n2 - 2)
    se = math.sqrt(sp2 * (1.0 / n1 + 1.0 / n2))
    if se == 0:
        return (0.0 if m1 == m2 else (1e12 if m1 > m2 else -1e12)), n1 + n2 - 2, 1.0 if m1 == m2 else 0.0
    t = (m1 - m2) / se
    df = n1 + n2 - 2
    p = 2.0 * (1.0 - t_cdf(abs(t), df))
    return t, df, p

def ttest_welch(x, y):
    n1, n2 = len(x), len(y)
    m1, m2 = mean(x), mean(y)
    v1, v2 = variance(x), variance(y)
    se2 = v1 / n1 + v2 / n2
    if se2 == 0:
        return (0.0 if m1 == m2 else (1e12 if m1 > m2 else -1e12)), 1, 1.0 if m1 == m2 else 0.0
    t = (m1 - m2) / math.sqrt(se2)
    df = se2 ** 2 / ((v1 / n1) ** 2 / (n1 - 1) + (v2 / n2) ** 2 / (n2 - 1))
    p = 2.0 * (1.0 - t_cdf(abs(t), df))
    return t, df, p

def mannwhitney_u(x, y):
    """Mann-Whitney U（tie 校正正态近似）。返回 (U, z, p, n1, n2)。"""
    n1, n2 = len(x), len(y)
    combined = list(x) + list(y)
    r = rankdata(combined)
    R1 = sum(r[:n1])
    U1 = R1 - n1 * (n1 + 1) / 2.0
    U2 = n1 * n2 - U1
    U = min(U1, U2)
    # tie 校正
    vals = sorted(combined)
    tie = 0.0
    k = 0
    while k < len(vals):
        j = k
        while j + 1 < len(vals) and vals[j + 1] == vals[k]:
            j += 1
        t = j - k + 1
        if t > 1:
            tie += t ** 3 - t
        k = j + 1
    mu = n1 * n2 / 2.0
    sigma2 = n1 * n2 / 12.0 * ((n1 + n2 + 1) - tie / ((n1 + n2) * (n1 + n2 - 1)))
    if sigma2 <= 0:
        return U, 0.0, 1.0, n1, n2
    z = (U - mu) / math.sqrt(sigma2)
    p = 2.0 * (1.0 - norm_cdf(abs(z)))
    return U, z, p, n1, n2

def anova_oneway(groups):
    """单因素方差分析。返回 (F, df1, df2, p)。"""
    k = len(groups)
    ni = [len(g) for g in groups]
    N = sum(ni)
    means = [mean(g) for g in groups]
    gm = sum(m * n for m, n in zip(means, ni)) / N
    ssb = sum(n * (m - gm) ** 2 for n, m in zip(ni, means))
    ssw = sum(sum((x - mean(g)) ** 2 for x in g) for g in groups)
    df1 = k - 1
    df2 = N - k
    if df2 <= 0 or ssw <= 0:
        return None, df1, df2, None
    F = (ssb / df1) / (ssw / df2)
    p = 1.0 - f_cdf(F, df1, df2)
    return F, df1, df2, p

def welch_anova(groups):
    """Welch 方差分析（方差不齐）。返回 (F, df1, df2, p)。"""
    k = len(groups)
    ni = [len(g) for g in groups]
    means = [mean(g) for g in groups]
    vi = [variance(g) for g in groups]
    wi = [n / v for n, v in zip(ni, vi) if v > 0]
    if len(wi) < k:
        return None, None, None, None
    sw = sum(wi)
    xw = sum(w * m for w, m in zip(wi, means)) / sw
    num = sum(w * (m - xw) ** 2 for w, m in zip(wi, means)) / (k - 1)
    lam = 3.0 * sum((1.0 - w / sw) ** 2 / (n - 1) for w, n in zip(wi, ni))
    df1 = k - 1
    df2 = (k * k - 1) / (3.0 * lam) if lam > 0 else 1e9
    F = num / (1.0 + 2.0 * (k - 2) * lam / (k * k - 1))
    p = 1.0 - f_cdf(F, df1, df2)
    return F, df1, df2, p

def kruskal_wallis(groups):
    """Kruskal-Wallis H（tie 校正）。返回 (H, df, p)。"""
    k = len(groups)
    combined = [x for g in groups for x in g]
    r = rankdata(combined)
    ni = [len(g) for g in groups]
    N = sum(ni)
    R = []
    pos = 0
    for n in ni:
        R.append(sum(r[pos:pos + n]))
        pos += n
    H = 12.0 / (N * (N + 1)) * sum(Ri ** 2 / n for Ri, n in zip(R, ni)) - 3 * (N + 1)
    vals = sorted(combined)
    tie = 0.0
    i = 0
    while i < len(vals):
        j = i
        while j + 1 < len(vals) and vals[j + 1] == vals[i]:
            j += 1
        t = j - i + 1
        if t > 1:
            tie += t ** 3 - t
        i = j + 1
    c = 1.0 - tie / (N ** 3 - N)
    if c <= 0:
        return None, k - 1, None
    H = H / c
    df = k - 1
    p = 1.0 - chi2_cdf(H, df)
    return H, df, p

def paired_t(d):
    n = len(d)
    if n < 2:
        return None, None, None
    md = mean(d)
    sd_ = sd(d)
    if sd_ == 0:
        return (0.0 if md == 0 else 1e12), n - 1, 1.0 if md == 0 else 0.0
    t = md / (sd_ / math.sqrt(n))
    p = 2.0 * (1.0 - t_cdf(abs(t), n - 1))
    return t, n - 1, p

def wilcoxon_signed(d):
    """Wilcoxon 符号秩（正态近似，零值剔除，tie 校正）。返回 (W, z, p, n)。"""
    ds = [x for x in d if x != 0]
    n = len(ds)
    if n < 6:
        return None, None, None, n
    absd = [abs(x) for x in ds]
    r = rankdata(absd)
    Wp = sum(ri for ri, x in zip(r, ds) if x > 0)
    W = min(Wp, n * (n + 1) / 2.0 - Wp)
    vals = sorted(absd)
    tie = 0.0
    i = 0
    while i < len(vals):
        j = i
        while j + 1 < len(vals) and vals[j + 1] == vals[i]:
            j += 1
        t = j - i + 1
        if t > 1:
            tie += t ** 3 - t
        i = j + 1
    mu = n * (n + 1) / 4.0
    sigma2 = n * (n + 1) * (2 * n + 1) / 24.0 - tie / 48.0
    if sigma2 <= 0:
        return W, 0.0, 1.0, n
    z = (W - mu) / math.sqrt(sigma2)
    p = 2.0 * (1.0 - norm_cdf(abs(z)))
    return W, z, p, n

def chi2_independence(table):
    """列联表卡方独立性检验。table: r×c 计数矩阵。返回 (chi2, df, p, cramers_v)。"""
    r = len(table)
    c = len(table[0])
    rs = [sum(row) for row in table]
    cs = [sum(table[i][j] for i in range(r)) for j in range(c)]
    n = sum(rs)
    chi2 = 0.0
    for i in range(r):
        for j in range(c):
            e = rs[i] * cs[j] / n
            if e > 0:
                chi2 += (table[i][j] - e) ** 2 / e
    df = (r - 1) * (c - 1)
    p = 1.0 - chi2_cdf(chi2, df)
    v = math.sqrt(chi2 / (n * min(r - 1, c - 1))) if min(r - 1, c - 1) > 0 else None
    return chi2, df, p, v

# ---------------------------------------------------------------- 效应量

def cohen_d(x, y):
    n1, n2 = len(x), len(y)
    sp = math.sqrt(((n1 - 1) * variance(x) + (n2 - 1) * variance(y)) / (n1 + n2 - 2))
    if sp == 0:
        return None
    return (mean(x) - mean(y)) / sp

def hedges_g(x, y):
    n1, n2 = len(x), len(y)
    d = cohen_d(x, y)
    if d is None:
        return None
    J = 1.0 - 3.0 / (4.0 * (n1 + n2) - 9.0)
    return J * d

def rank_biserial(x, y):
    """由 Mann-Whitney U 计算 rank-biserial correlation r = 1 - 2U/(n1*n2)。
    返回 (r, 方向说明)。"""
    n1, n2 = len(x), len(y)
    U, _, _, _, _ = mannwhitney_u(x, y)
    r = 1.0 - 2.0 * U / (n1 * n2)  # 正值表示 x 组秩更高
    return r

def eta_squared(groups):
    """ANOVA η² = SS_between / SS_total。"""
    k = len(groups)
    ni = [len(g) for g in groups]
    N = sum(ni)
    means = [mean(g) for g in groups]
    gm = sum(m * n for m, n in zip(means, ni)) / N
    ssb = sum(n * (m - gm) ** 2 for n, m in zip(ni, means))
    sst = ssb + sum(sum((x - mean(g)) ** 2 for x in g) for g in groups)
    return ssb / sst if sst > 0 else None

def omega_squared(groups):
    F, df1, df2, _ = anova_oneway(groups)
    if F is None:
        return None
    return (df1 * (F - 1)) / (df1 * F + df2 + 1)

def epsilon_squared(H, n, k):
    if H is None:
        return None
    return (H - k + 1) / (n - k)

def cohens_f(groups):
    e2 = eta_squared(groups)
    if e2 is None or e2 >= 1:
        return None
    return math.sqrt(e2 / (1 - e2))

def matched_pairs_rank_biserial(d):
    """配对 Wilcoxon 的 r = Z/√N。"""
    _, z, p, n = wilcoxon_signed(d)
    if z is None:
        return None
    return z / math.sqrt(n)

# ---------------------------------------------------------------- 置信区间

def bootstrap_ci(stat_func, data_args, B=2000, alpha=0.05, seed=20240501):
    """百分位 bootstrap CI。data_args 为传给 stat_func 的样本元组。
    返回 (lo, hi, B_used)。任何重采样无法计算时返回 (None, None, B)。"""
    rng = random.Random(seed)
    stats = []
    for _ in range(B):
        try:
            resampled = []
            for a in data_args:
                n = len(a)
                idx = [rng.randrange(n) for _ in range(n)]
                resampled.append([a[i] for i in idx])
            s = stat_func(*resampled)
            if s is not None and s == s:
                stats.append(s)
        except Exception:
            continue
    if len(stats) < B // 2:
        return None, None, len(stats)
    stats.sort()
    lo = stats[int((alpha / 2) * len(stats))]
    hi = stats[min(len(stats) - 1, int((1 - alpha / 2) * len(stats)))]
    return lo, hi, len(stats)

def welch_diff_ci(x, y, alpha=0.05):
    """Welch 均值差 CI。返回 (lo, hi, df)。"""
    n1, n2 = len(x), len(y)
    v1, v2 = variance(x), variance(y)
    se2 = v1 / n1 + v2 / n2
    if se2 == 0:
        return None, None, None
    df = se2 ** 2 / ((v1 / n1) ** 2 / (n1 - 1) + (v2 / n2) ** 2 / (n2 - 1))
    tcrit = t_ppf(1 - alpha / 2, df)
    diff = mean(x) - mean(y)
    return diff - tcrit * math.sqrt(se2), diff + tcrit * math.sqrt(se2), df

def hodges_lehmann(x, y):
    """Hodges-Lehmann 位置差估计：所有两两差的中位数。返回 (est, lo, hi, k_index)。"""
    diffs = sorted(a - b for a in x for b in y)
    n = len(diffs)
    if n == 0:
        return None, None, None, None
    est = median(diffs)
    # 正态近似分位（Hettmansperger–McKean 口径），仅作区间参考
    k = int(math.floor(0.5 * n - norm_ppf(0.975) * math.sqrt(n / 4.0)))
    k = max(0, min(n - 1, k))
    lo = diffs[k]
    hi = diffs[n - 1 - k]
    return est, lo, hi, k

def pearson_r(x, y):
    n = len(x)
    if n < 3:
        return None, None
    mx, my = mean(x), mean(y)
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx <= 0 or syy <= 0:
        return None, None
    r = sxy / math.sqrt(sxx * syy)
    if abs(r) >= 1:
        p = 0.0
    else:
        t = r * math.sqrt((n - 2) / (1 - r * r))
        p = 2.0 * (1.0 - t_cdf(abs(t), n - 2))
    return r, p

def fisher_z_ci(r, n, alpha=0.05):
    if r is None or abs(r) >= 1 or n < 4:
        return None, None
    z = 0.5 * math.log((1 + r) / (1 - r))
    se = 1.0 / math.sqrt(n - 3)
    zc = norm_ppf(1 - alpha / 2)
    lo = math.tanh(z - zc * se)
    hi = math.tanh(z + zc * se)
    return lo, hi

def linregress(x, y):
    """简单 OLS 回归 y = b0 + b1 x。返回 dict 或 None。"""
    n = len(x)
    if n < 3:
        return None
    mx, my = mean(x), mean(y)
    sxx = sum((a - mx) ** 2 for a in x)
    if sxx <= 0:
        return None
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    b1 = sxy / sxx
    b0 = my - b1 * mx
    resid = [b - (b0 + b1 * a) for a, b in zip(x, y)]
    sse = sum(e * e for e in resid)
    sst = sum((b - my) ** 2 for b in y)
    r2 = 1 - sse / sst if sst > 0 else None
    sigma2 = sse / (n - 2)
    se_b1 = math.sqrt(sigma2 / sxx)
    t = b1 / se_b1 if se_b1 > 0 else (0.0 if b1 == 0 else 1e12)
    p = 2.0 * (1.0 - t_cdf(abs(t), n - 2))
    tcrit = t_ppf(0.975, n - 2)
    return {
        "intercept": b0, "slope": b1, "slope_se": se_b1,
        "slope_ci95": [b1 - tcrit * se_b1, b1 + tcrit * se_b1],
        "t": t, "df": n - 2, "p": p, "r2": r2,
        "n": n, "resid_sd": math.sqrt(sigma2),
    }

# ---------------------------------------------------------------- 多重比较

def holm_adjust(pvals):
    """Holm 逐步校正。返回校正后 p 值列表（与输入等长，None 保持 None）。"""
    m = len(pvals)
    order = sorted(range(m), key=lambda i: (pvals[i] is None, pvals[i] if pvals[i] is not None else 0))
    adj = [None] * m
    run_max = 0.0
    for rank, idx in enumerate(order):
        p = pvals[idx]
        if p is None:
            continue
        val = min(1.0, (m - rank) * p)
        run_max = max(run_max, val)
        adj[idx] = run_max
    return adj

def fdr_bh(pvals):
    """Benjamini-Hochberg FDR 校正。"""
    m = len(pvals)
    order = sorted(range(m), key=lambda i: (pvals[i] is None, pvals[i] if pvals[i] is not None else 0))
    adj = [None] * m
    run_min = 1.0
    for rank in range(m - 1, -1, -1):
        idx = order[rank]
        p = pvals[idx]
        if p is None:
            continue
        val = min(run_min, p * m / (rank + 1))
        run_min = val
        adj[idx] = val
    return adj

# ---------------------------------------------------------------- 描述汇总

def describe(xs):
    xs = [x for x in xs if x is not None]
    n = len(xs)
    if n == 0:
        return None
    return {
        "n": n,
        "mean": mean(xs),
        "sd": sd(xs),
        "median": median(xs),
        "q1": quantile(xs, 0.25),
        "q3": quantile(xs, 0.75),
        "min": min(xs),
        "max": max(xs),
        "skewness": skewness(xs) if n >= 3 else None,
    }
