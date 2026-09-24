"""Phase 3 — liquidity engine, market impact & flow anomaly math.

Pure functions (deterministic, unit-testable) + DB-side scoring helpers:

* estimate_price_impact_pct  — V2 constant-product exact execution-price
  impact; returns None when inputs are insufficient (never a fake number).
* liquidity_impact           — trade USD / pool liquidity USD.
* liquidity event valuation  — USD add/remove + before/after + pct change.
* flow acceleration          — current vs previous window with noise floor.
* flow anomaly score         — explainable 0-100 composite (volume spike,
  imbalance, whale participation, liquidity shift). OBSERVED metric only.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.database.models import FlowAnomaly, TokenFlowSnapshot
from app.dex.assets import buy_sell_imbalance, buy_sell_ratio
from config import dex_settings as DS

log = logging.getLogger("flow.impact")


# ---------------------------------------------------------------------------
# pure math
# ---------------------------------------------------------------------------

def estimate_price_impact_pct(reserve_in: float | None,
                              reserve_out: float | None,
                              amount_in: float | None,
                              fee_bps: int = 30) -> float | None:
    """Exact execution price impact of a V2 swap vs mid price, in %.

    mid = r_out/r_in ; exec = (r_out*k/(r_in+k)) / k ... simplified to the
    standard closed form: impact = k*(1+fee)/((r_in+k*(1-fee))) - 1 applied
    on the output side. Returns None when any input is missing/zero — we do
    NOT fabricate an impact for unknown reserves."""
    try:
        if not reserve_in or not reserve_out or amount_in is None or amount_in <= 0:
            return None
        rf = float(amount_in) / float(reserve_in)
        if rf > 10:                      # absurd: more than 10x the reserve in
            return None
        fee = fee_bps / 10000.0
        # executed out / ideal out  =>  (1-fee)*r_out/(r_in+k(1-fee)) * r_in/r_out... 
        ratio = ((reserve_out * (amount_in * (1 - fee)))
                 / (reserve_in + amount_in * (1 - fee))) / (reserve_out / reserve_in * amount_in) \
            if amount_in > 0 else 1.0
        impact = (ratio - 1.0) * 100.0   # negative = worse price than mid
        return round(abs(impact), 4)
    except (ZeroDivisionError, ValueError, OverflowError):
        return None


def liquidity_impact(usd_value: float | None,
                     liquidity_usd: float | None) -> float | None:
    """Trade size relative to pool depth (0.05 = 5% of TVL). None if unknown."""
    if not usd_value or not liquidity_usd or liquidity_usd <= 0:
        return None
    return round(min(usd_value / liquidity_usd, 99.0), 6)


def liquidity_event_valuation(amount0_norm: float | None, price0: float | None,
                              amount1_norm: float | None, price1: float | None,
                              liquidity_before_usd: float | None
                              ) -> tuple[float | None, float | None]:
    """Return (usd_value, pct_change_of_pool). When both legs are priced use
    their sum; if only one leg is priced double it (constant-product pools
    hold ~equal value); never invent numbers when nothing is priced."""
    v0 = (amount0_norm or 0) * price0 if price0 is not None and amount0_norm is not None else None
    v1 = (amount1_norm or 0) * price1 if price1 is not None and amount1_norm is not None else None
    if v0 is not None and v1 is not None:
        usd = v0 + v1
    elif v0 is not None:
        usd = v0 * 2
    elif v1 is not None:
        usd = v1 * 2
    else:
        return None, None
    pct = None
    if liquidity_before_usd and liquidity_before_usd > 0:
        pct = round(usd / liquidity_before_usd * 100.0, 4)
    return round(usd, 2), pct


def safe_acceleration(current: float, previous: float,
                      floor_usd: float = 1000.0) -> float | None:
    """% change of window volume; None when baseline below noise floor."""
    c, p = current or 0.0, previous or 0.0
    if abs(p) < floor_usd:
        return None
    return round((c - p) / abs(p) * 100.0, 2)


def compute_flow_anomaly(current_volume: float, baseline_volume: float | None,
                         buy_usd: float, sell_usd: float,
                         whale_usd: float, liquidity_change_usd: float,
                         liquidity_usd: float | None) -> tuple[float, list[str], dict]:
    """Explainable 0-100 anomaly score for one token window.

    Components (weights fixed but simple, all observable):
      40 pts volume spike vs rolling baseline (>= min volume to matter)
      20 pts buy/sell imbalance magnitude
      20 pts whale participation share
      20 pts liquidity shift % (add OR remove both count as anomalous)
    Returns (score, reasons, feature_dict for Phase 4)."""
    reasons: list[str] = []
    features: dict = {}
    score = 0.0
    total = (buy_usd or 0) + (sell_usd or 0)
    if total < DS.flow_anomaly_min_volume_usd:
        return 0.0, ["volume below anomaly floor"], {"volume": total}

    # 1. volume spike
    rel = None
    if baseline_volume and baseline_volume >= DS.flow_anomaly_min_volume_usd:
        rel = current_volume / baseline_volume
        features["relative_volume"] = round(rel, 4)
        if rel > 1.5:
            comp = min(40.0, (rel - 1.0) * 20.0)
            score += comp
            reasons.append(f"volume {rel:.1f}x baseline (${baseline_volume:,.0f})")
    # 2. imbalance
    imb = buy_sell_imbalance(buy_usd, sell_usd)
    features["imbalance"] = imb
    if imb is not None and abs(imb) > 0.5:
        comp = min(20.0, (abs(imb) - 0.5) * 40.0)
        score += comp
        reasons.append(f"{'buy' if imb > 0 else 'sell'}-side imbalance "
                       f"{abs(imb) * 100:.0f}%")
    # 3. whale participation
    wh_share = (whale_usd or 0) / total if total else None
    features["whale_participation"] = round(wh_share, 4) if wh_share is not None else None
    if wh_share is not None and wh_share > 0.3:
        comp = min(20.0, wh_share * 40.0)
        score += comp
        reasons.append(f"whale flow {wh_share * 100:.0f}% of window volume")
    # 4. liquidity shift
    if liquidity_usd and liquidity_usd > 0 and liquidity_change_usd:
        lshift = abs(liquidity_change_usd) / liquidity_usd
        features["liquidity_shift"] = round(lshift, 4)
        if lshift > DS.liquidity_shift_pct_alert / 100.0:
            comp = min(20.0, lshift * 50.0)
            score += comp
            reasons.append(f"liquidity moved {lshift * 100:.1f}%")
    features["buy_sell_ratio"] = buy_sell_ratio(buy_usd, sell_usd)
    return round(min(score, 100.0), 2), reasons[:6], features


# ---------------------------------------------------------------------------
# DB-side hourly anomaly evaluation (incremental — reads snapshots only)
# ---------------------------------------------------------------------------

def evaluate_token_anomalies(s: Session, chain_pk: int,
                             now: datetime | None = None) -> int:
    """For tokens active in the last hour: compare current-hour flow against
    the rolling baseline of previous hourly buckets. Writes flow_anomalies
    rows (deduped per token+window). Bounded work: top 200 active tokens."""
    now = now or datetime.utcnow()
    hour = 3600
    cur_start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    active = (s.query(TokenFlowSnapshot.token_address,
                      func.sum(TokenFlowSnapshot.buy_volume_usd),
                      func.sum(TokenFlowSnapshot.sell_volume_usd))
              .filter(TokenFlowSnapshot.chain_pk == chain_pk,
                      TokenFlowSnapshot.bucket_seconds == hour,
                      TokenFlowSnapshot.bucket_start >= cur_start)
              .group_by(TokenFlowSnapshot.token_address)
              .order_by(func.sum(TokenFlowSnapshot.buy_volume_usd)
                        + func.sum(TokenFlowSnapshot.sell_volume_usd).desc())
              .limit(200).all())
    written = 0
    for tok, buy, sell in active:
        buy, sell = float(buy or 0), float(sell or 0)
        total = buy + sell
        if total < DS.flow_anomaly_min_volume_usd:
            continue
        base_rows = (s.query(TokenFlowSnapshot.buy_volume_usd,
                             TokenFlowSnapshot.sell_volume_usd)
                     .filter(TokenFlowSnapshot.chain_pk == chain_pk,
                             TokenFlowSnapshot.token_address == tok,
                             TokenFlowSnapshot.bucket_seconds == hour,
                             TokenFlowSnapshot.bucket_start < cur_start)
                     .order_by(TokenFlowSnapshot.bucket_start.desc())
                     .limit(DS.baseline_windows).all())
        baseline = sum(float(b or 0) + float(sl or 0) for b, sl in base_rows) / max(len(base_rows), 1) \
            if base_rows else None
        whale_row = (s.query(TokenFlowSnapshot.whale_buy_volume,
                             TokenFlowSnapshot.whale_sell_volume,
                             TokenFlowSnapshot.liquidity_change_usd)
                     .filter(TokenFlowSnapshot.chain_pk == chain_pk,
                             TokenFlowSnapshot.token_address == tok,
                             TokenFlowSnapshot.bucket_seconds == hour,
                             TokenFlowSnapshot.bucket_start >= cur_start).first())
        whale = liq_chg = 0.0
        if whale_row:
            whale = float(whale_row[0] or 0) + float(whale_row[1] or 0)
            liq_chg = float(whale_row[2] or 0)
        from app.database.models import DexPool
        best_liq = (s.query(func.max(DexPool.liquidity_usd))
                    .filter(DexPool.chain_pk == chain_pk,
                            (DexPool.token0 == tok) | (DexPool.token1 == tok))
                    .scalar())
        score, reasons, feats = compute_flow_anomaly(
            total, baseline, buy, sell, whale, liq_chg,
            float(best_liq) if best_liq else None)
        accel = safe_acceleration(total, baseline or 0.0)
        dialect = s.bind.dialect.name if s.bind else "sqlite"
        ins = pg_insert if dialect == "postgresql" else sqlite_insert
        stmt = ins(FlowAnomaly).values(
            chain_pk=chain_pk, token_address=tok, window_start=cur_start,
            current_volume_usd=total, baseline_volume_usd=baseline,
            relative_volume=feats.get("relative_volume"),
            buy_sell_ratio=feats.get("buy_sell_ratio"),
            imbalance=feats.get("imbalance"),
            whale_participation=feats.get("whale_participation"),
            flow_acceleration_pct=accel, anomaly_score=score,
            reasons="; ".join(reasons)[:500], timestamp=now)
        s.execute(stmt.on_conflict_do_update(
            index_elements=["chain_pk", "token_address", "window_start"],
            set_={"anomaly_score": score, "current_volume_usd": total,
                  "reasons": "; ".join(reasons)[:500]}))
        if score >= DS.flow_anomaly_min_volume_usd / 1e6:   # always store; rank later
            written += 1
    return written
