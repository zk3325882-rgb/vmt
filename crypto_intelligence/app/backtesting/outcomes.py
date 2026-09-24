"""Phase 5 - outcome calculation (pure functions, unit-testable).

All functions here are deterministic and side-effect free: given the price
path observed AFTER a signal at time T, they compute return / MFE / MAE /
drawdown / time-to-target. They never touch the original signal row.

Interpretation rules:
- Positive-direction signal (UP): favorable = price up.
  MFE = max % gain after T; MAE = min % change after T (<=0 typically).
- Negative-direction signal (DOWN): favorable = price down.
  Directional MFE = max % drop (as positive number); directional MAE = worst
  adverse rally. Raw mfe/mae always store highest/lowest % move so both
  interpretations are recoverable.
"""
from __future__ import annotations

import math


def _valid_price(p):
    try:
        p = float(p)
    except (TypeError, ValueError):
        return None
    if math.isnan(p) or math.isinf(p) or p <= 0:
        return None
    return p


def return_percent(price_at_signal, price_after):
    """((after/at)-1)*100 with missing/zero/invalid handling -> None."""
    a = _valid_price(price_at_signal)
    b = _valid_price(price_after)
    if a is None or b is None:
        return None
    return (b / a - 1.0) * 100.0


def path_returns(price_at_signal, prices):
    """List of % moves for each post-signal price point (None entries kept)."""
    out = []
    for p in prices:
        out.append(return_percent(price_at_signal, p))
    return out


def mfe_mae(price_at_signal, prices, direction: str = "UP"):
    """Return dict with raw + directional MFE/MAE (%).

    `prices` must be ordered by time ascending, all strictly after T.
    Returns None metrics when there is no usable data (never 0!).
    """
    rets = [r for r in path_returns(price_at_signal, prices) if r is not None]
    if not rets:
        return {"mfe_percent": None, "mae_percent": None,
                "mfe_directional": None, "mae_directional": None}
    hi = max(rets)
    lo = min(rets)
    if direction == "UP":
        mfe_d, mae_d = hi, lo
    elif direction == "DOWN":
        # favorable = falling price; adverse = rally
        mfe_d = -lo          # e.g. price fell 8% -> +8 favorable
        mae_d = -hi          # rally hurts shorts -> negative value
    else:
        mfe_d = abs(hi) if abs(hi) >= abs(lo) else abs(lo)
        mae_d = -min(abs(hi), abs(lo))
    return {"mfe_percent": hi, "mae_percent": lo,
            "mfe_directional": mfe_d, "mae_directional": mae_d}


def max_drawdown(price_at_signal, prices):
    """Max peak-to-trough decline (%) within the horizon, measured on the
    running maximum of the price path starting from entry price."""
    a = _valid_price(price_at_signal)
    if a is None:
        return None
    peak = a
    worst = 0.0
    seen = False
    for p in prices:
        p = _valid_price(p)
        if p is None:
            continue
        seen = True
        if p > peak:
            peak = p
        dd = (p / peak - 1.0) * 100.0
        if dd < worst:
            worst = dd
    return worst if seen else None


def drawdown_before_target(price_at_signal, prices, target_pct: float):
    """Max drawdown accrued BEFORE the first time the price crossed
    `target_pct` (signed). If never reached, equals full-horizon drawdown."""
    a = _valid_price(price_at_signal)
    if a is None:
        return None
    peak = a
    worst = 0.0
    for p in prices:
        p = _valid_price(p)
        if p is None:
            continue
        pct = (p / a - 1.0) * 100.0
        if pct >= target_pct:
            break  # target reached — stop measuring
        if p > peak:
            peak = p
        dd = (p / peak - 1.0) * 100.0
        if dd < worst:
            worst = dd
    return worst


def _drawdown_before_favorable(price_at_signal, prices, favorable_target_pct):
    """Direction-aware wrapper: for DOWN signals the favorable move is a
    decline, so measure drawdown (on inverted path) before the price falls
    by |target|%."""
    a = _valid_price(price_at_signal)
    if a is None:
        return None
    if favorable_target_pct >= 0:
        return drawdown_before_target(a, prices, favorable_target_pct)
    # DOWN: invert returns so declines become gains, reuse same logic
    inv = []
    for p in prices:
        pv = _valid_price(p)
        inv.append(None if pv is None else a * (2 - pv / a))  # mirror around a
    return drawdown_before_target(a, inv, -favorable_target_pct)


def time_to_threshold(price_at_signal, timestamps, prices, threshold_pct,
                      direction_up: bool = True):
    """Seconds from signal until price first crosses threshold.

    `timestamps` are absolute datetimes; the signal time itself must be the
    FIRST element (t0) so times are measured from T, not from the first
    post-signal sample. threshold_pct is a positive magnitude; direction_up
    selects sign. Returns None when never reached or data unusable.
    """
    a = _valid_price(price_at_signal)
    if a is None or len(timestamps) != len(prices) or len(timestamps) < 2:
        return None
    t0 = timestamps[0]
    target = threshold_pct if direction_up else -threshold_pct
    for ts, p in zip(timestamps[1:], prices[1:]):
        r = return_percent(a, p)
        if r is None:
            continue
        if r >= target:
            delta = (ts - t0).total_seconds()
            return int(delta) if delta >= 0 else None
    return None


def classify_outcome(ret_pct, strong_pos=None, pos=None, neg=None,
                     strong_neg=None):
    """Configurable outcome labels; defaults come from settings — business
    logic never hardcodes thresholds."""
    if ret_pct is None:
        return None
    if strong_pos is None:
        from config import backtest_settings as BS
        strong_pos, pos = BS.strong_positive_pct, BS.positive_pct
        neg, strong_neg = BS.negative_pct, BS.strong_negative_pct
    if ret_pct >= strong_pos:
        return "STRONG_POSITIVE"
    if ret_pct >= pos:
        return "POSITIVE"
    if ret_pct > neg:
        return "NEUTRAL"
    if ret_pct > strong_neg:
        return "NEGATIVE"
    return "STRONG_NEGATIVE"


def hit_and_time(price_at_signal, timestamps, prices, targets=(5.0, 10.0, 20.0)):
    """(hits, times) dicts. `timestamps[0]` MUST be the signal time T with a
    valid entry price at that index; later entries are post-signal samples.
    hit_* is True/False when measurable, None when data unusable."""
    hits, times = {}, {}
    measurable = (_valid_price(price_at_signal) is not None and len(prices) > 1)
    for tgt in targets:
        tt = time_to_threshold(price_at_signal, timestamps, prices, tgt, True)
        hits[f"hit_{tgt:g}"] = (tt is not None) if measurable else None
        times[f"t_{tgt:g}"] = tt
    for tgt in (5.0, 10.0):
        tt = time_to_threshold(price_at_signal, timestamps, prices, tgt, False)
        hits[f"hit_-{tgt:g}"] = (tt is not None) if measurable else None
        times[f"t_-{tgt:g}"] = tt
    return hits, times


def compute_outcome(signal_timestamp, price_at_signal, path, direction="UP",
                    horizon_seconds=3600, targets=(5.0, 10.0, 20.0)):
    """Full outcome bundle for one (signal, horizon).

    `path`: list of (timestamp, price) tuples strictly AFTER signal time and
    within the horizon, ordered ascending. Missing/invalid points are
    tolerated; if nothing usable exists every metric is None and
    completeness is 0 (INCOMPLETE — never silently zero).
    """
    pts = [(ts, p) for ts, p in path
           if signal_timestamp < ts
           and (ts - signal_timestamp).total_seconds() <= horizon_seconds]
    pts.sort(key=lambda x: x[0])
    # prepend the signal anchor point so time-to-target measures from T
    timestamps = [signal_timestamp] + [ts for ts, _ in pts]
    prices = [price_at_signal] + [p for _, p in pts]
    valid_prices = [_valid_price(p) for p in prices]
    usable = [p for p in valid_prices[1:] if p is not None]
    completeness = round(100.0 * len(usable) / len(pts), 2) if pts else 0.0

    end_price = valid_prices[-1] if len(valid_prices) > 1 else None
    fallback_used = False
    if end_price is None and usable:
        end_price = usable[-1]
        fallback_used = True
    ret = return_percent(price_at_signal, end_price)
    mm = mfe_mae(price_at_signal, usable, direction)
    dd = max_drawdown(price_at_signal, usable)
    # drawdown accrued before the FAVORABLE target (direction-aware):
    # UP signals chase +targets[0]; DOWN signals chase -targets[0]
    fav_target = targets[0] if direction != "DOWN" else -targets[0]
    ddt = _drawdown_before_favorable(price_at_signal, usable, fav_target)
    hits, times = hit_and_time(price_at_signal, timestamps, valid_prices,
                               targets)
    return {
        "price_after": end_price,
        "return_percent": ret,
        **mm,
        "max_drawdown_percent": dd,
        "drawdown_before_target": ddt,
        "time_to_5pct": times.get("t_5"),
        "time_to_10pct": times.get("t_10"),
        "time_to_20pct": times.get("t_20"),
        "time_to_minus_5pct": times.get("t_-5"),
        "time_to_minus_10pct": times.get("t_-10"),
        "hit_5pct": hits.get("hit_5"),
        "hit_10pct": hits.get("hit_10"),
        "hit_20pct": hits.get("hit_20"),
        "outcome_class": classify_outcome(ret),
        "price_data_completeness": completeness,
        "path_points": len(pts),
        "fallback_end_price": fallback_used,
    }
