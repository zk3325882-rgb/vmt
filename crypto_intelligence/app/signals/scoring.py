"""Phase 4 - Scoring, risk and explanation.

Deterministic, explainable scoring: given a feature dict (raw + normalized)
that was available at time T, produce scores / risk / explanations using
ONLY those features. The same function is used live and for historical
reconstruction, which guarantees reproducibility and prevents look-ahead.
"""
from __future__ import annotations

import json
from datetime import datetime

from config import signal_settings as SS


def _g(raw: dict, norm: dict, key: str):
    """Prefer stored normalized value; recompute fallback via helpers."""
    v = norm.get(key + "_norm")
    if v is not None:
        return v
    from app.features.engine import normalize_features
    n2 = normalize_features(raw)
    return n2.get(key + "_norm")


def score_band(score: float) -> str:
    for upper, name in SS.bands:
        if score < upper:
            return name
    return "EXTREME"


def compute_scores(raw: dict, norm: dict) -> dict:
    """All sub-scores in 0..100 plus the final composite score."""
    def nz(v, default=0.0):
        return default if v is None else v

    # --- flow intensity (bullish/bearish components) ---
    buy_n = _g(raw, norm, "buy_volume_1h")
    sell_n = _g(raw, norm, "sell_volume_1h")
    imb = norm.get("imbalance_norm")
    bsr = norm.get("buy_sell_ratio_norm")
    accel = norm.get("acceleration_norm")
    momentum = norm.get("momentum_norm")

    bullish_flow = (0.35 * nz(buy_n) + 0.25 * nz(imb, 0.5) + 0.2 * nz(bsr)
                    + 0.1 * nz(accel, 0.5) + 0.1 * nz(momentum, 0.5))
    bearish_flow = (0.35 * nz(sell_n) + 0.25 * (1 - nz(imb, 0.5))
                    + 0.1 * nz(accel, 0.5))
    net_pos = nz(raw.get("net_flow_1h"), 0) > 0

    # --- whale activity ---
    wpart = norm.get("whale_participation_norm")
    wnet = _g(raw, norm, "whale_net_flow")
    wevents = norm.get("whale_events_norm")
    maxw = norm.get("max_whale_score_norm")
    whale_bull = (0.4 * nz(wpart) + 0.3 * nz(wnet, 0.5) + 0.2 * nz(wevents)
                  + 0.1 * nz(maxw))
    whale_bear = (0.4 * nz(wpart) + 0.3 * (1 - nz(wnet, 0.5)) + 0.2 * nz(wevents)
                  + 0.1 * nz(maxw))

    # --- anomaly ---
    fa = norm.get("flow_anomaly_score_norm")
    rv = norm.get("relative_volume_norm")
    tx = norm.get("transaction_anomaly_norm")
    anomaly = max(nz(fa), 0.6 * nz(rv) + 0.4 * nz(tx))

    # --- liquidity stability (higher norm == more stable) ---
    lchg = norm.get("liquidity_change_norm")  # signed around 0.5
    liq_stability = nz(lchg, 0.5)
    liq_instability = 1.0 - liq_stability

    # --- wallet breadth ---
    wallets = norm.get("wallet_activity_norm")

    # --- market confirmation ---
    mkt = norm.get("market_confirm_norm", 0.5)

    # composite directional evidence
    positive_evidence = (SS.flow_weight * bullish_flow
                         + SS.whale_weight * whale_bull
                         + SS.anomaly_weight * anomaly
                         + SS.wallet_weight * nz(wallets)
                         + SS.market_weight * nz(mkt, 0.5))
    negative_evidence = (SS.flow_weight * bearish_flow
                         + SS.whale_weight * whale_bear
                         + SS.anomaly_weight * anomaly
                         + SS.wallet_weight * nz(wallets)
                         + SS.market_weight * (1 - nz(mkt, 0.5))
                         + SS.liquidity_weight * liq_instability)
    neutral_core = (SS.liquidity_weight * liq_stability)

    pos100 = round(min(positive_evidence, 1.0) * 100, 2)
    neg100 = round(min(negative_evidence, 1.0) * 100, 2)
    base = max(pos100, neg100) + neutral_core * 10

    # confidence: agreement of independent evidence families
    confirmers = sum(1 for v in (bullish_flow if net_pos else bearish_flow,
                                 anomaly, nz(wallets), nz(mkt, 0.5) if net_pos
                                 else 1 - nz(mkt, 0.5)) if v >= 0.5)
    completeness = raw.get("_completeness", 60.0)
    confidence = round(min(100.0, confirmers * 20
                           + completeness * 0.4), 2)

    # risk penalty
    risk, _risk_reasons = risk_detail(raw, norm)
    score = round(max(0.0, min(100.0, base * (1 - SS.risk_weight * risk / 100.0))), 2)

    quality = round(min(100.0, 0.6 * confidence + 0.4 * completeness), 2)

    accum = round(min(100.0, 100 * (0.5 * nz(wnet, 0.5) + 0.3 * nz(wpart)
                                    + 0.2 * nz(imb, 0.5))), 2)
    distrib = round(min(100.0, 100 * (0.5 * (1 - nz(wnet, 0.5)) + 0.3 * nz(wpart)
                                      + 0.2 * (1 - nz(imb, 0.5)))), 2)
    inflow = round(min(100.0, 100 * (0.5 * nz(buy_n) + 0.3 * nz(imb, 0.5)
                                     + 0.2 * nz(momentum, 0.5))), 2)
    outflow = round(min(100.0, 100 * (0.5 * nz(sell_n)
                                      + 0.3 * (1 - nz(imb, 0.5)))), 2)
    onchain_momentum = round(min(100.0, 100 * (0.5 * nz(momentum, 0.5)
                                               + 0.3 * nz(accel, 0.5)
                                               + 0.2 * nz(imb, 0.5))), 2)

    return {
        "score": score, "band": score_band(score),
        "confidence": confidence, "quality": quality,
        "risk_score": risk,
        "positive_score": pos100, "negative_score": neg100,
        "accumulation_score": accum, "distribution_score": distrib,
        "capital_inflow_score": inflow, "capital_outflow_score": outflow,
        "onchain_momentum_score": onchain_momentum,
        "confirmation_count": confirmers,
        "components": {
            "bullish_flow": round(bullish_flow * 100, 2),
            "bearish_flow": round(bearish_flow * 100, 2),
            "whale": round(100 * (whale_bull if net_pos else whale_bear), 2),
            "anomaly": round(anomaly * 100, 2),
            "liquidity_stability": round(liq_stability * 100, 2),
            "wallet_activity": round(nz(wallets) * 100, 2),
            "market_confirmation": round(nz(mkt, 0.5) * 100, 2),
        },
    }


def risk_detail(raw: dict, norm: dict) -> tuple[float, list]:
    """(risk 0..100, reasons) — data-quality/reliability risk only."""
    liq = raw.get("liquidity_usd")
    r = 0.0
    reasons: list[str] = []
    if liq is None:
        r += 25; reasons.append("liquidity_unknown")
    elif liq < 50_000:
        r += 30; reasons.append("very_low_liquidity")
    elif liq < 250_000:
        r += 15; reasons.append("low_liquidity")
    if raw.get("price") is None:
        r += 25; reasons.append("price_missing")
    comp = raw.get("_completeness", 60.0)
    if comp < 40:
        r += 20; reasons.append("incomplete_data")
    elif comp < 60:
        r += 10; reasons.append("partial_data")
    age_h = raw.get("token_age_hours")
    if age_h is not None and age_h < 24:
        r += 10; reasons.append("new_token")
    lchg = raw.get("liquidity_change_percent")
    if lchg is not None and lchg <= -20:
        r += 15; reasons.append("liquidity_decline")
    return round(min(r, 100.0), 2), reasons


def detect_signal_type(raw: dict, norm: dict, scores: dict) -> str:
    """Classify which pattern fired — rule-based, transparent, deterministic."""
    net1 = raw.get("net_flow_1h") or 0
    net24 = raw.get("net_flow_24h") or 0
    imb = raw.get("buy_sell_imbalance") or 0
    wnet = raw.get("whale_net_flow") or 0
    fa = raw.get("flow_anomaly_score") or 0
    lchg = raw.get("liquidity_change_percent")
    age = raw.get("token_age_hours")
    minf = SS.min_flow_usd

    if age is not None and age < 24 and scores["score"] >= 30:
        if lchg is not None and lchg > 20:
            return "NEW_TOKEN_LIQUIDITY"
        if wnet > minf:
            return "NEW_TOKEN_WHALE_ACTIVITY"
        if fa >= 50:
            return "NEW_TOKEN_FLOW_ANOMALY"
        return "NEW_TOKEN_ACTIVITY"
    if lchg is not None and lchg <= -20:
        return "LIQUIDITY_RISK_SIGNAL"
    if net1 >= minf and net24 <= -minf and imb > 0.2:
        return "FLOW_REVERSAL"
    if net1 <= -minf and net24 >= minf and imb < -0.2:
        return "FLOW_REVERSAL"
    if scores["accumulation_score"] >= 65 and wnet > minf and imb > 0:
        return "ACCUMULATION_SIGNAL"
    if scores["distribution_score"] >= 65 and wnet < -minf and imb < 0:
        return "DISTRIBUTION_SIGNAL"
    if fa >= 60 and abs(net1) < minf:
        return "ANOMALY_SIGNAL"
    if wnet > minf and (raw.get("whale_participation_ratio") or 0) > 0.3:
        return "WHALE_ACTIVITY_SIGNAL"
    if net1 >= minf:
        return ("CAPITAL_INFLOW_SIGNAL"
                if scores["capital_inflow_score"] >= 60 else "BULLISH_FLOW_SIGNAL")
    if net1 <= -minf:
        return ("CAPITAL_OUTFLOW_SIGNAL"
                if scores["capital_outflow_score"] >= 60 else "BEARISH_FLOW_SIGNAL")
    return "NEUTRAL_SIGNAL"


def build_explanation(raw: dict, norm: dict, scores: dict, stype: str) -> tuple[str, list]:
    """Human-readable reasons + machine-readable list. Every reason cites a
    concrete past-time feature value — no future information can appear."""
    reasons = []
    fmt_money = lambda v: ("$%s" % f"{v:,.0f}") if v is not None else "n/a"
    if raw.get("net_flow_1h") is not None:
        reasons.append({
            "feature": "net_flow_1h", "value": raw["net_flow_1h"],
            "contribution": scores["components"]["bullish_flow"] / 10,
            "text": (f"Net flow in last 1h: {fmt_money(raw['net_flow_1h'])} "
                     f"(buys {fmt_money(raw.get('buy_volume_1h'))} vs sells "
                     f"{fmt_money(raw.get('sell_volume_1h'))}).")})
    if raw.get("buy_sell_imbalance") is not None:
        reasons.append({
            "feature": "buy_sell_imbalance", "value": raw["buy_sell_imbalance"],
            "contribution": scores["confidence"] / 20,
            "text": f"Buy/sell imbalance: {raw['buy_sell_imbalance']:+.1%}."})
    if raw.get("whale_net_flow") is not None:
        reasons.append({
            "feature": "whale_net_flow", "value": raw["whale_net_flow"],
            "contribution": scores["components"]["whale"] / 10,
            "text": (f"Whale net flow: {fmt_money(raw['whale_net_flow'])}, "
                     f"participation {(raw.get('whale_participation_ratio') or 0):.0%} "
                     "of 1h volume.")})
    if raw.get("flow_anomaly_score") is not None:
        reasons.append({
            "feature": "flow_anomaly_score", "value": raw["flow_anomaly_score"],
            "contribution": scores["components"]["anomaly"] / 10,
            "text": f"Flow anomaly score {raw['flow_anomaly_score']:.0f}/100 "
                    f"(volume {raw.get('relative_volume') or 0:.1f}x baseline)."})
    if raw.get("liquidity_change_percent") is not None:
        reasons.append({
            "feature": "liquidity_change_percent",
            "value": raw["liquidity_change_percent"],
            "contribution": -scores["risk_score"] / 20,
            "text": f"Liquidity change 24h: {raw['liquidity_change_percent']:+.1f}% "
                    f"(pool depth {fmt_money(raw.get('liquidity_usd'))})."})
    if raw.get("active_wallet_count_1h"):
        reasons.append({
            "feature": "active_wallet_count_1h",
            "value": raw["active_wallet_count_1h"],
            "contribution": scores["components"]["wallet_activity"] / 20,
            "text": f"Distinct active wallets last hour: "
                    f"{raw['active_wallet_count_1h']}."})
    contra = []
    if (raw.get("net_flow_1h") or 0) > 0 and (raw.get("liquidity_change_percent") or 0) < -10:
        contra.append("liquidity declining while flow positive")
    if (raw.get("net_flow_1h") or 0) < 0 and (raw.get("whale_net_flow") or 0) > 0:
        contra.append("whales net buying while overall flow negative")
    text = (f"{stype}: evidence intensity {scores['score']:.0f}/100 "
            f"({scores['band']}) from data up to "
            f"{raw.get('_as_of', 'now')}. Scores describe observed on-chain "
            "activity only and are NOT predictions of future price movement.")
    return text, [{"feature": r["feature"], "value": r["value"],
                   "contribution": round(r["contribution"], 2), "text": r["text"]}
                  for r in reasons], contra
