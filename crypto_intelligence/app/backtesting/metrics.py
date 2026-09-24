"""Phase 5 - aggregate metrics: score ranges, signal types, liquidity
buckets, whale participation, flow strength, categories, chains, regimes,
combinations, distributions, bootstrap CIs, chronological splits.

All results are DESCRIPTIVE HISTORICAL statistics with mandatory sample_size.
Nothing here is (or claims to be) a calibrated future probability.
"""
from __future__ import annotations

import bisect
import math
import random
from collections import Counter

from config import backtest_settings as BS


# ---------------------------------------------------------------------------
# basic stats (pure python — no numpy dependency required)
# ---------------------------------------------------------------------------

def describe(values) -> dict:
    vals = sorted(v for v in values if v is not None and not _bad(v))
    n = len(vals)
    if n == 0:
        return {"sample_size": 0}
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / (n - 1) if n > 1 else 0.0
    return {
        "sample_size": n,
        "mean_return": round(mean, 4),
        "median_return": round(_pct(vals, 50), 4),
        "standard_deviation": round(math.sqrt(var), 4),
        "minimum_return": round(vals[0], 4),
        "maximum_return": round(vals[-1], 4),
        "percentile_25": round(_pct(vals, 25), 4),
        "percentile_75": round(_pct(vals, 75), 4),
        "percentile_90": round(_pct(vals, 90), 4),
    }


def _bad(v):
    try:
        return math.isnan(float(v)) or math.isinf(float(v))
    except (TypeError, ValueError):
        return True


def _pct(sorted_vals, p):
    """Linear-interpolated percentile."""
    n = len(sorted_vals)
    if n == 0:
        return float("nan")
    if n == 1:
        return sorted_vals[0]
    k = (n - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, n - 1)
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def sharpe_like(values) -> float | None:
    """STATISTICAL BACKTEST METRIC ONLY: mean/std of historical returns per
    observation. Not an annualized Sharpe ratio; not a forecast."""
    d = describe(values)
    if d["sample_size"] < 3 or not d.get("standard_deviation"):
        return None
    return round(d["mean_return"] / d["standard_deviation"], 4)


def histogram(values, bins=10, lo=None, hi=None) -> list[dict]:
    vals = [v for v in values if v is not None and not _bad(v)]
    if not vals:
        return []
    lo = min(vals) if lo is None else lo
    hi = max(vals) if hi is None else hi
    if hi <= lo:
        return [{"bin_start": lo, "bin_end": hi, "count": len(vals)}]
    step = (hi - lo) / bins
    out = []
    counts = [0] * bins
    for v in vals:
        idx = min(int((v - lo) / step), bins - 1)
        counts[idx] += 1
    for i, c in enumerate(counts):
        out.append({"bin_start": round(lo + i * step, 4),
                    "bin_end": round(lo + (i + 1) * step, 4), "count": c})
    return out


def distribution(values) -> dict:
    d = describe(values)
    d["histogram"] = histogram(values)
    classes = Counter(classify_outcome_label(v) for v in values
                      if v is not None and not _bad(v))
    total = sum(classes.values())
    d["outcome_distribution"] = {
        k: {"count": c,
            "historical_share_pct": round(100.0 * c / total, 2) if total else None}
        for k, c in sorted(classes.items())}
    return d


def classify_outcome_label(ret):
    sp, p, n, sn = (BS.strong_positive_pct, BS.positive_pct,
                    BS.negative_pct, BS.strong_negative_pct)
    if ret >= sp:
        return "STRONG_POSITIVE"
    if ret >= p:
        return "POSITIVE"
    if ret > n:
        return "NEUTRAL"
    if ret > sn:
        return "NEGATIVE"
    return "STRONG_NEGATIVE"


# ---------------------------------------------------------------------------
# hit rates — descriptive historical frequencies, never "probability"
# ---------------------------------------------------------------------------

def hit_rate(rows, field: str, target_pct: float) -> dict:
    """rows: dicts with `field` (return_percent) OR precomputed hit bool.
    Returns successful_cases / total_cases / historical_rate."""
    succ = tot = 0
    for r in rows:
        v = r.get(field)
        if v is None or _bad(v):
            continue
        tot += 1
        if float(v) >= target_pct:
            succ += 1
    return {"target_pct": target_pct,
            "successful_cases": succ, "total_cases": tot,
            "historical_rate": round(100.0 * succ / tot, 2) if tot else None,
            "wording": "Historical rate among observed signals."}


def outcome_rates(rows) -> dict:
    """positive/negative/neutral class shares from outcome_class fields."""
    cls = Counter(r.get("outcome_class") for r in rows
                  if r.get("outcome_class"))
    tot = sum(cls.values())
    pos = cls.get("STRONG_POSITIVE", 0) + cls.get("POSITIVE", 0)
    neg = cls.get("STRONG_NEGATIVE", 0) + cls.get("NEGATIVE", 0)
    neu = cls.get("NEUTRAL", 0)
    pct = lambda x: round(100.0 * x / tot, 2) if tot else None
    return {"sample_size": tot, "positive_rate": pct(pos),
            "negative_rate": pct(neg), "neutral_rate": pct(neu),
            "class_counts": dict(cls)}


# ---------------------------------------------------------------------------
# bootstrap confidence intervals (statistical, clearly labelled)
# ---------------------------------------------------------------------------

def bootstrap_ci(values, stat="median", samples=None, seed=None,
                 alpha=0.05) -> dict | None:
    vals = [v for v in values if v is not None and not _bad(v)]
    n = len(vals)
    if n < 5 or not BS.bootstrap_enabled:
        return None
    samples = samples or BS.bootstrap_samples
    rng = random.Random(seed if seed is not None else BS.bootstrap_seed)
    fn = {"median": lambda a: _pct(sorted(a), 50),
          "mean": lambda a: sum(a) / len(a),
          "rate": lambda a: sum(1 for x in a if x >= 0) / len(a)}[stat]
    est = fn(vals)
    stats = []
    for _ in range(samples):
        samp = [vals[rng.randrange(n)] for _ in range(n)]
        stats.append(fn(samp))
    stats.sort()
    lo = _pct(stats, 100 * alpha / 2)
    hi = _pct(stats, 100 * (1 - alpha / 2))
    return {"statistic": stat, "estimate": round(est, 4),
            "lower_bound": round(lo, 4), "upper_bound": round(hi, 4),
            "confidence_level": round(100 * (1 - alpha), 1),
            "bootstrap_samples": samples, "sample_size": n,
            "label": "Statistical bootstrap CI over observed history only — "
                     "NOT a guarantee about future markets."}


# ---------------------------------------------------------------------------
# grouping helpers
# ---------------------------------------------------------------------------

SCORE_RANGES = [(0, 19), (20, 39), (40, 59), (60, 74), (75, 89), (90, 100)]


def score_range_label(score) -> str:
    if score is None:
        return "UNKNOWN"
    s = float(score)
    for lo, hi in SCORE_RANGES:
        if lo <= s <= hi:
            return f"{lo}-{hi}"
    return "UNKNOWN"


def liquidity_bucket(liq_usd) -> str:
    """Configurable buckets from settings (default <$100K ... >$100M)."""
    labels = ["< $100K", "$100K-$500K", "$500K-$1M", "$1M-$10M",
              "$10M-$100M", "> $100M"]
    bounds = list(BS.liquidity_buckets)
    if liq_usd is None or _bad(liq_usd):
        return "UNKNOWN"
    v = float(liq_usd)
    for i, b in enumerate(bounds):
        if v < b:
            return labels[i]
    return labels[-1]


def whale_participation_level(participation) -> str:
    """LOW/MEDIUM/HIGH from whale participation ratio (0..1)."""
    if participation is None or _bad(participation):
        return "UNKNOWN"
    p = float(participation)
    if p < 0.2:
        return "LOW_WHALE_PARTICIPATION"
    if p < 0.5:
        return "MEDIUM_WHALE_PARTICIPATION"
    return "HIGH_WHALE_PARTICIPATION"


def anomaly_bucket(score) -> str:
    if score is None or _bad(score):
        return "UNKNOWN"
    s = float(score)
    for lo in (0, 20, 40, 60, 80):
        if s < lo + 20:
            return f"{lo}-{lo + 20}"
    return "80-100"


def impact_bucket(pct) -> str:
    if pct is None or _bad(pct):
        return "UNKNOWN"
    v = abs(float(pct))
    for ub, label in ((1, "<1%"), (5, "1-5%"), (10, "5-10%"),
                      (25, "10-25%"), (50, "25-50%")):
        if v < ub:
            return label
    return ">50%"


CATEGORY_MAP = {
    "L1": "Layer 1", "layer1": "Layer 1", "LAYER_1": "Layer 1",
    "L2": "Layer 2", "layer2": "Layer 2", "LAYER_2": "Layer 2",
    "MEME": "Meme", "meme": "Meme",
    "DEFI": "DeFi", "defi": "DeFi", "DEX": "DeFi",
    "STABLECOIN": "Stablecoin", "stable": "Stablecoin",
    "GAMING": "Gaming", "GAME": "Gaming", "GAMBLE": "Gaming",
    "AI": "AI", "RWA": "RWA",
    "NEW": "New Token", "new_token": "New Token",
}


def category_label(cat) -> str:
    if not cat:
        return "Unknown"
    return CATEGORY_MAP.get(str(cat), str(cat).title() if str(cat).isupper()
                            else "Unknown")


def group_stats(rows, key_fn, horizon_field="horizon") -> dict:
    """Generic grouped descriptive stats. Never ranks groups as 'best'."""
    groups: dict[str, list] = {}
    for r in rows:
        g = key_fn(r)
        groups.setdefault(g, []).append(r)
    out = {}
    for g, items in sorted(groups.items()):
        rets = [r.get("return_percent") for r in items]
        d = describe(rets)
        mfes = [r.get("mfe_percent") for r in items]
        maes = [r.get("mae_percent") for r in items]
        d["mean_mfe"] = (describe(mfes).get("mean_return")
                         if d.get("sample_size") else None)
        d["mean_mae"] = (describe(maes).get("mean_return")
                         if d.get("sample_size") else None)
        d.update(outcome_rates(items))
        d["hit_rates"] = {f"+{t:g}%": hit_rate(items, "return_percent", t)
                          for t in BS.hit_targets}
        d["incomplete_count"] = sum(1 for r in items
                                    if r.get("data_status") == "INCOMPLETE")
        out[g] = d
    return out


# convenience specialized analyzers ---------------------------------------

def by_score_range(rows) -> dict:
    return group_stats(rows, lambda r: score_range_label(r.get("signal_score")))


def by_signal_type(rows) -> dict:
    return group_stats(rows, lambda r: r.get("signal_type") or "UNKNOWN")


def by_chain(rows) -> dict:
    # chain identity always preserved (never merged)
    return group_stats(rows, lambda r: str(r.get("chain_id") or "UNKNOWN"))


def by_category(rows) -> dict:
    return group_stats(rows, lambda r: category_label(r.get("category")))


def by_liquidity_bucket(rows) -> dict:
    return group_stats(rows, lambda r: liquidity_bucket(r.get("liquidity_usd")))


def by_whale_participation(rows) -> dict:
    return group_stats(rows,
                       lambda r: whale_participation_level(
                           r.get("whale_participation")))


def by_flow_strength(rows, field="flow_anomaly_score") -> dict:
    return group_stats(rows, lambda r: anomaly_bucket(r.get(field)))


def by_impact(rows) -> dict:
    return group_stats(rows, lambda r: impact_bucket(r.get("price_impact_pct")))


def time_to_target_summary(rows) -> dict:
    """Average/median seconds-to-target plus reach counts. None means
    'not reached within horizon' and is excluded from timing stats but
    counted in attempts."""
    out = {}
    for key, label in (("time_to_5pct", "+5%"), ("time_to_10pct", "+10%"),
                       ("time_to_20pct", "+20%"),
                       ("time_to_minus_5pct", "-5%"),
                       ("time_to_minus_10pct", "-10%")):
        times = [r.get(key) for r in rows if r.get(key) is not None]
        measurable = sum(1 for r in rows
                         if r.get("return_percent") is not None)
        out[label] = {
            "reached": len(times), "measurable_signals": measurable,
            "historical_reach_rate": (round(100.0 * len(times) / measurable, 2)
                                      if measurable else None),
            "avg_seconds": round(sum(times) / len(times), 1) if times else None,
            "median_seconds": _pct(sorted(times), 50) if times else None,
            "not_reached": (measurable - len(times)) if measurable else None,
        }
    return out


# ---------------------------------------------------------------------------
# chronological splits & walk-forward (time-series safe)
# ---------------------------------------------------------------------------

def split_chronological(rows, train=None, val=None):
    """Sort by signal_timestamp; contiguous 60/20/20 by default. Random
    shuffling is deliberately NOT offered — it would leak future data."""
    train = train if train is not None else BS.split_train
    val = val if val is not None else BS.split_validation
    ordered = sorted(rows, key=lambda r: r["signal_timestamp"])
    n = len(ordered)
    i1 = int(n * train)
    i2 = int(n * (train + val))
    return {"TRAIN": ordered[:i1], "VALIDATION": ordered[i1:i2],
            "TEST": ordered[i2:],
            "boundaries": {
                "train_end": ordered[i1 - 1]["signal_timestamp"] if i1 else None,
                "validation_end": ordered[i2 - 1]["signal_timestamp"] if i2 > i1 else None},
            "note": "Chronological split; no random leakage across time."}


def walk_forward_windows(start, end, train_days, test_days, step_days=None):
    """Yield (window_no, train_start, train_end, test_start, test_end).
    Framework only — ML training plugs in later."""
    from datetime import timedelta
    step = step_days or test_days
    cur = start
    w = 0
    windows = []
    while cur + timedelta(days=train_days + test_days) <= end:
        te_s = cur + timedelta(days=train_days)
        te_e = te_s + timedelta(days=test_days)
        windows.append({"window": w, "train_start": cur, "train_end": te_s,
                        "test_start": te_s, "test_end": te_e})
        w += 1
        cur = cur + timedelta(days=step)
    return windows


# ---------------------------------------------------------------------------
# regime + combination analysis (configured combinations only)
# ---------------------------------------------------------------------------

def regime_of(row) -> list[str]:
    tags = []
    liq = row.get("liquidity_usd")
    if liq is not None and not _bad(liq):
        tags.append("HIGH_LIQUIDITY" if float(liq) >= 1e6 else "LOW_LIQUIDITY")
    volc = row.get("volume_change")
    if volc is not None and not _bad(volc):
        tags.append("HIGH_VOLUME" if float(volc) >= 0 else "LOW_VOLUME")
    dd = row.get("max_drawdown_percent")
    mfe = row.get("mfe_percent")
    if dd is not None and mfe is not None:
        spread = abs(float(mfe)) + abs(float(dd))
        tags.append("HIGH_VOLATILITY" if spread >= 10 else "LOW_VOLATILITY")
    ret = row.get("return_percent")
    fa = row.get("flow_anomaly_score")
    if ret is not None:
        tags.append("POSITIVE_MARKET_FLOW" if float(ret) >= 0
                    else "NEGATIVE_MARKET_FLOW")
    return tags or ["UNKNOWN_REGIME"]


def by_regime(rows) -> dict:
    exploded = []
    for r in rows:
        for tag in regime_of(r):
            rr = dict(r)
            rr["_regime"] = tag
            exploded.append(rr)
    return group_stats(exploded, lambda r: r["_regime"])


# configured feature combinations (avoids exponential blowup)
COMBINATIONS = {
    "high_whale_flow+high_imbalance": lambda r: (
        (r.get("whale_net_flow") or 0) > 0
        and (r.get("buy_sell_imbalance") or 0) >= 0.4),
    "high_anomaly+stable_liquidity": lambda r: (
        (r.get("flow_anomaly_score") or 0) >= 60
        and abs(r.get("liquidity_change_percent") or 0) < 10),
    "large_buy+whale+positive_flow": lambda r: (
        (r.get("large_buy_volume") or 0) > 0
        and (r.get("whale_participation") or 0) >= 0.2
        and (r.get("net_flow_1h") or 0) > 0),
}


def by_combination(rows, names=None) -> dict:
    out = {}
    for name, pred in COMBINATIONS.items():
        if names and name not in names:
            continue
        matched = [r for r in rows if pred(r)]
        d = describe([r.get("return_percent") for r in matched])
        d.update(outcome_rates(matched))
        d["description"] = "Configured combination — historical statistics only."
        out[name] = d
    return out


def threshold_comparison(rows, thresholds=(50, 60, 70, 75, 80, 90),
                         horizon="24h") -> list[dict]:
    """Side-by-side stats per minimum score. NO 'best' selection."""
    out = []
    sub_all = [r for r in rows if r.get("horizon") == horizon]
    for th in thresholds:
        sub = [r for r in sub_all if (r.get("signal_score") or 0) >= th]
        d = describe([r.get("return_percent") for r in sub])
        d["min_score"] = th
        d["horizon"] = horizon
        d["+10%"] = hit_rate(sub, "return_percent", 10.0)
        d["interpretation"] = ("Descriptive historical comparison; the user "
                               "decides how to interpret these numbers.")
        out.append(d)
    return out
