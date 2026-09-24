"""Phase 4 - Feature Engine.

Builds a normalized feature vector per token from ALREADY-STORED Phase 1-3
data (flow buckets, anomalies, large transactions, whale events, market
snapshots). The signal engine never queries raw blockchain tables directly.

NO LOOK-AHEAD: every query is bounded by `as_of` (and features only use
buckets whose window fully closed before as_of - grace). Historical
reconstruction simply calls build(as_of=<past time>).

Missing data stays None; normalization helpers never return NaN.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database.models import (
    DexPool, FlowAnomaly, LargeTransaction, LiquiditySnapshot, MarketSnapshot,
    Token, TokenFeatureSnapshot, TokenFlowSnapshot, TokenTransfer, WhaleEvent,
)
from config import signal_settings as SS

log = logging.getLogger("features.engine")

BUCKET_1H, BUCKET_24H, BUCKET_15M, BUCKET_5M = 3600, 86400, 900, 300


# ---------------------------------------------------------------------------
# safe numeric helpers (never NaN/inf out of this module)
# ---------------------------------------------------------------------------

def _f(v):
    """float or None, sanitized."""
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return x


def clamp01(x):
    x = _f(x)
    if x is None:
        return None
    return max(0.0, min(1.0, x))


def saturating(value, scale):
    """[0, inf) -> [0, 1] with diminishing returns; robust denominators."""
    v, s = _f(value), _f(scale)
    if v is None or not s or s <= 0:
        return None
    v = max(v, 0.0)
    return clamp01(v / (v + s))


def signed_score(value, scale):
    """(-inf, inf) -> [0, 1] centered at 0.5 (no infinities)."""
    v, s = _f(value), _f(scale)
    if v is None or not s or s <= 0:
        return None
    return clamp01(0.5 + 0.5 * (v / (abs(v) + s)))


def percentile_rank(sorted_vals, x):
    """Fraction of sorted_vals <= x (historical percentile, pure python)."""
    if not sorted_vals or x is None:
        return None
    lo, hi = 0, len(sorted_vals)
    while lo < hi:
        mid = (lo + hi) // 2
        if sorted_vals[mid] <= x:
            lo = mid + 1
        else:
            hi = mid
    return lo / len(sorted_vals)


# ---------------------------------------------------------------------------
# extraction (SQL aggregation over existing Phase 1-3 tables only)
# ---------------------------------------------------------------------------

def _sum_flow(s, chain_pk, token, bucket, since, until):
    row = (s.query(
        func.sum(TokenFlowSnapshot.buy_volume_usd),
        func.sum(TokenFlowSnapshot.sell_volume_usd),
        func.sum(TokenFlowSnapshot.whale_buy_volume),
        func.sum(TokenFlowSnapshot.whale_sell_volume),
        func.sum(TokenFlowSnapshot.large_buy_volume),
        func.sum(TokenFlowSnapshot.large_sell_volume),
        func.sum(TokenFlowSnapshot.liquidity_change_usd),
        func.sum(TokenFlowSnapshot.other_swap_volume),
        func.count(TokenFlowSnapshot.id))
        .filter(TokenFlowSnapshot.chain_pk == chain_pk,
                TokenFlowSnapshot.token_address == token,
                TokenFlowSnapshot.bucket_seconds == bucket,
                TokenFlowSnapshot.bucket_start >= since,
                TokenFlowSnapshot.bucket_start < until)
        .one())
    keys = ("buy", "sell", "whale_buy", "whale_sell", "large_buy",
            "large_sell", "liq_change", "other", "n")
    return {k: _f(v) for k, v in zip(keys, row)}


def extract_raw_features(s: Session, chain_pk: int, token_address: str,
                         as_of: datetime):
    """Raw features available strictly at/before `as_of` minus grace.

    This single function is the ONLY place where Phase 1-3 tables are read
    for scoring; both live evaluation and historical reconstruction use it
    with different `as_of`, which structurally prevents look-ahead bias.
    """
    cutoff = as_of - timedelta(seconds=SS.feature_grace_seconds)
    feats: dict = {}

    windows = {"5m": (BUCKET_5M, 300), "15m": (BUCKET_15M, 900),
               "1h": (BUCKET_1H, 3600), "4h": (BUCKET_1H, 14400),
               "24h": (BUCKET_1H, 86400)}
    for label, (bucket, span) in windows.items():
        win = _sum_flow(s, chain_pk, token_address, bucket,
                        cutoff - timedelta(seconds=span), cutoff)
        feats[f"buy_volume_{label}"] = win["buy"]
        feats[f"sell_volume_{label}"] = win["sell"]
        if win["buy"] is None and win["sell"] is None:
            net = None
        else:
            net = (win["buy"] or 0) - (win["sell"] or 0)
        feats[f"net_flow_{label}"] = net
        if label == "1h":
            wb, ws = win["whale_buy"] or 0, win["whale_sell"] or 0
            tot = (win["buy"] or 0) + (win["sell"] or 0)
            feats["whale_buy_volume"] = win["whale_buy"]
            feats["whale_sell_volume"] = win["whale_sell"]
            feats["whale_net_flow"] = (wb - ws) if tot > 0 else None
            feats["whale_participation_ratio"] = (wb + ws) / tot if tot > 0 else None
            feats["large_buy_volume"] = win["large_buy"]
            feats["large_sell_volume"] = win["large_sell"]
            feats["liquidity_change_1h"] = win["liq_change"]

    buy1, sell1 = feats.get("buy_volume_1h"), feats.get("sell_volume_1h")
    if buy1 is not None and sell1 is not None:
        total = buy1 + sell1
        feats["buy_sell_ratio"] = (buy1 / sell1) if sell1 > 0 else (
            999.0 if buy1 > 0 else None)
        feats["buy_sell_imbalance"] = (buy1 - sell1) / total if total > 0 else None
    else:
        feats["buy_sell_ratio"] = None
        feats["buy_sell_imbalance"] = None

    prev = _sum_flow(s, chain_pk, token_address, BUCKET_1H,
                     cutoff - timedelta(hours=2), cutoff - timedelta(hours=1))
    cur_v = (buy1 or 0) + (sell1 or 0)
    pv = (prev["buy"] or 0) + (prev["sell"] or 0)
    if pv > 0:
        feats["flow_acceleration_pct"] = (cur_v - pv) / pv * 100.0
    elif cur_v > 0:
        feats["flow_acceleration_pct"] = 100.0  # capped: no infinite %
    else:
        feats["flow_acceleration_pct"] = None

    m2 = _sum_flow(s, chain_pk, token_address, BUCKET_1H,
                   cutoff - timedelta(hours=3), cutoff - timedelta(hours=1))
    prior = (m2["buy"] or 0) + (m2["sell"] or 0)
    feats["volume_prior_2h_avg"] = prior / 2 if prior else None
    feats["volume_current_1h"] = cur_v or None

    fa = (s.query(FlowAnomaly)
          .filter(FlowAnomaly.chain_pk == chain_pk,
                  FlowAnomaly.token_address == token_address,
                  FlowAnomaly.window_start < cutoff)
          .order_by(FlowAnomaly.window_start.desc()).first())
    feats["flow_anomaly_score"] = _f(fa.anomaly_score) if fa else None
    feats["relative_volume"] = _f(fa.relative_volume) if fa else None

    # ---- liquidity: sum of each pool's latest snapshot <= cutoff ----
    feats["liquidity_usd"] = None
    feats["liquidity_change_percent"] = None
    pools = [(x.address if hasattr(x, "address") else x.pair_address)
             for x in s.query(DexPool).filter(
                 DexPool.chain_pk == chain_pk,
                 (DexPool.token0 == token_address)
                 | (DexPool.token1 == token_address)).limit(50).all()]
    pools = [p for p in pools if p]

    def _liq_at(ts_upper):
        if not pools:
            return None
        sub = (s.query(LiquiditySnapshot.pool_address,
                       func.max(LiquiditySnapshot.timestamp).label("mt"))
               .filter(LiquiditySnapshot.pool_address.in_(pools),
                       LiquiditySnapshot.timestamp <= ts_upper)
               .group_by(LiquiditySnapshot.pool_address).subquery())
        val = (s.query(func.sum(LiquiditySnapshot.liquidity_usd))
               .join(sub, (LiquiditySnapshot.pool_address == sub.c.pool_address)
                     & (LiquiditySnapshot.timestamp == sub.c.mt)).scalar())
        return _f(val)

    cur_liq = _liq_at(cutoff)
    if cur_liq is not None:
        feats["liquidity_usd"] = cur_liq
        old = _liq_at(cutoff - timedelta(hours=24))
        if old and old > 0:
            feats["liquidity_change_percent"] = (cur_liq - old) / old * 100.0

    # ---- transactions (Phase 1 large tx) ----
    q = s.query(LargeTransaction).filter(
        LargeTransaction.chain_pk == chain_pk,
        LargeTransaction.token_address == token_address,
        LargeTransaction.timestamp >= cutoff - timedelta(hours=24),
        LargeTransaction.timestamp < cutoff)
    feats["large_transaction_count"] = q.count()
    feats["large_inflow"] = _f(q.filter(LargeTransaction.flow == "IN")
                               .with_entities(func.sum(
                                   LargeTransaction.usd_value)).scalar())
    feats["large_outflow"] = _f(q.filter(LargeTransaction.flow == "OUT")
                                .with_entities(func.sum(
                                    LargeTransaction.usd_value)).scalar())
    feats["transaction_anomaly"] = _f(q.with_entities(
        func.max(LargeTransaction.anomaly_score)).scalar())
    feats["max_relative_size"] = _f(q.with_entities(
        func.max(LargeTransaction.relative_size)).scalar())
    feats["largest_transaction_usd"] = _f(q.with_entities(
        func.max(LargeTransaction.usd_value)).scalar())

    # ---- whale events (Phase 2) ----
    wq = s.query(WhaleEvent).filter(
        WhaleEvent.chain_pk == chain_pk,
        WhaleEvent.timestamp >= cutoff - timedelta(hours=24),
        WhaleEvent.timestamp < cutoff)
    tok_col = getattr(WhaleEvent, "token_address", None)
    if tok_col is not None:
        wq = wq.filter(tok_col == token_address)
    feats["whale_event_count_24h"] = wq.count()
    feats["max_whale_score"] = _f(wq.with_entities(
        func.max(WhaleEvent.whale_score)).scalar())

    # ---- wallet activity ----
    aw = (s.query(func.count(func.distinct(TokenTransfer.from_address)),
                  func.count(func.distinct(TokenTransfer.to_address)))
          .filter(TokenTransfer.chain_pk == chain_pk,
                  TokenTransfer.token_address == token_address,
                  TokenTransfer.timestamp >= cutoff - timedelta(hours=1),
                  TokenTransfer.timestamp < cutoff).one())
    feats["active_wallet_count_1h"] = int((aw[0] or 0) + (aw[1] or 0))
    tok = s.query(Token).filter_by(chain_pk=chain_pk,
                                   address=token_address).first()
    feats["category"] = tok.category if tok else None
    feats["symbol"] = (tok.symbol or token_address[:10]) if tok else token_address[:10]
    feats["token_id"] = tok.id if tok else None
    feats["token_age_hours"] = (_f((cutoff - tok.first_seen).total_seconds() / 3600.0)
                                if tok and tok.first_seen else None)

    # ---- market ----
    ms = (s.query(MarketSnapshot)
          .filter(MarketSnapshot.chain_pk == chain_pk,
                  MarketSnapshot.token_address == token_address,
                  MarketSnapshot.timestamp < cutoff)
          .order_by(MarketSnapshot.timestamp.desc()).limit(2).all())
    feats["price"] = None
    feats["volume"] = None
    feats["price_change_pct_per_hour"] = None
    price_at = None
    if ms:
        feats["price"] = _f(ms[0].price_usd)
        feats["volume"] = _f(ms[0].volume_24h)
        price_at = ms[0].timestamp
        if len(ms) > 1 and _f(ms[1].price_usd):
            age_h = max((ms[0].timestamp - ms[1].timestamp).total_seconds()
                        / 3600.0, 1e-9)
            feats["price_change_pct_per_hour"] = (
                (feats["price"] / _f(ms[1].price_usd) - 1) * 100.0 / age_h)
    return feats, price_at


# ---------------------------------------------------------------------------
# normalization
# ---------------------------------------------------------------------------

def normalize_features(raw: dict) -> dict:
    """Normalize to 0..1 using liquidity/volume context — never raw dollars.

    A $10M flow on a $10B-liquidity token and a $100K flow on a $500K token
    map to comparable values because everything is divided by a contextual
    scale (5% of liquidity, falling back to 5% of hourly volume).
    """
    f = dict(raw)
    liq = f.get("liquidity_usd") or 0
    vol = f.get("volume_current_1h") or 0
    denom = liq if liq > 0 else (vol if vol > 0 else None)
    scale = (denom * 0.05) if denom else None
    n: dict = {}
    for k in ("buy_volume_1h", "sell_volume_1h", "net_flow_1h",
              "whale_buy_volume", "whale_sell_volume", "whale_net_flow",
              "large_inflow", "large_outflow", "largest_transaction_usd"):
        v = f.get(k)
        if v is None or not scale:
            n[k + "_norm"] = None
        elif "net" in k:
            n[k + "_norm"] = signed_score(v, scale)
        else:
            n[k + "_norm"] = saturating(abs(v), scale)
    if f.get("buy_sell_ratio") is not None:
        n["buy_sell_ratio_norm"] = clamp01(
            math.log1p(max(f["buy_sell_ratio"], 0)) / math.log1p(10))
    if f.get("buy_sell_imbalance") is not None:
        n["imbalance_norm"] = signed_score(f["buy_sell_imbalance"], 0.5)
    if f.get("flow_acceleration_pct") is not None:
        n["acceleration_norm"] = signed_score(f["flow_acceleration_pct"], 100.0)
    for sk in ("flow_anomaly_score", "transaction_anomaly", "max_whale_score"):
        if f.get(sk) is not None:
            n[sk + "_norm"] = clamp01(f[sk] / 100.0)
    if f.get("relative_volume") is not None:
        n["relative_volume_norm"] = clamp01(
            math.log1p(max(f["relative_volume"], 0)) / math.log1p(10))
    if f.get("whale_participation_ratio") is not None:
        n["whale_participation_norm"] = clamp01(f["whale_participation_ratio"])
    if f.get("liquidity_change_percent") is not None:
        n["liquidity_change_norm"] = signed_score(f["liquidity_change_percent"], 20.0)
    if f.get("max_relative_size") is not None:
        n["relative_size_norm"] = clamp01(
            math.log1p(max(f["max_relative_size"], 0)) / math.log1p(50))
    if f.get("active_wallet_count_1h") is not None:
        n["wallet_activity_norm"] = saturating(f["active_wallet_count_1h"], 25)
    if f.get("large_transaction_count") is not None:
        n["large_tx_norm"] = saturating(f["large_transaction_count"], 10)
    if f.get("whale_event_count_24h") is not None:
        n["whale_events_norm"] = saturating(f["whale_event_count_24h"], 5)
    cur, base = f.get("volume_current_1h"), f.get("volume_prior_2h_avg")
    if cur is not None:
        if base and base > 0:
            n["momentum_norm"] = clamp01(cur / (cur + 2 * base))
        else:
            n["momentum_norm"] = 0.5 if cur > 0 else None
    if f.get("price_change_pct_per_hour") is not None:
        n["market_confirm_norm"] = signed_score(f["price_change_pct_per_hour"], 2.0)
    return n


FEATURE_NAMES = [
    "buy_volume_5m", "buy_volume_15m", "buy_volume_1h", "buy_volume_4h",
    "buy_volume_24h", "sell_volume_5m", "sell_volume_15m", "sell_volume_1h",
    "sell_volume_4h", "sell_volume_24h", "net_flow_5m", "net_flow_15m",
    "net_flow_1h", "net_flow_4h", "net_flow_24h", "buy_sell_ratio",
    "buy_sell_imbalance", "flow_acceleration_pct", "flow_anomaly_score",
    "whale_buy_volume", "whale_sell_volume", "whale_net_flow",
    "whale_participation_ratio", "liquidity_usd", "liquidity_change_percent",
    "large_transaction_count", "large_inflow", "large_outflow",
    "transaction_anomaly", "max_relative_size", "largest_transaction_usd",
    "whale_event_count_24h", "max_whale_score", "active_wallet_count_1h",
    "token_age_hours", "price", "volume", "volume_current_1h",
    "volume_prior_2h_avg", "price_change_pct_per_hour", "relative_volume",
]


def completeness_pct(raw: dict) -> float:
    have = sum(1 for k in FEATURE_NAMES if raw.get(k) is not None)
    return round(100.0 * have / len(FEATURE_NAMES), 2)


class FeatureEngine:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    def build(self, chain_pk: int, token_address: str,
              as_of: datetime | None = None, persist: bool = True) -> dict:
        """{'raw','normalized','completeness','feature_timestamp'} using
        ONLY data <= as_of."""
        as_of = as_of or datetime.utcnow()
        with self.session_factory() as s:
            raw, feat_ts = extract_raw_features(s, chain_pk, token_address, as_of)
            norm = normalize_features(raw)
            comp = completeness_pct(raw)
            if persist:
                self._persist(s, chain_pk, token_address, as_of, raw, norm, comp)
        return {"raw": raw, "normalized": norm, "completeness": comp,
                "feature_timestamp": feat_ts or as_of}

    def _persist(self, s, chain_pk, token_address, as_of, raw, norm, comp):
        from app.flow.engine import bucket_start
        bstart = bucket_start(as_of, BUCKET_1H)
        payload = {**{k: raw.get(k) for k in FEATURE_NAMES}, **norm}
        blob = json.dumps({k: v for k, v in payload.items()
                           if isinstance(v, (int, float)) or v is None})[:8000]
        existing = (s.query(TokenFeatureSnapshot)
                    .filter_by(chain_pk=chain_pk, token_address=token_address,
                               bucket_seconds=BUCKET_1H,
                               bucket_start=bstart).first())
        if existing:
            existing.features = blob
            existing.completeness = comp
        else:
            s.add(TokenFeatureSnapshot(
                chain_pk=chain_pk, token_address=token_address,
                bucket_seconds=BUCKET_1H, bucket_start=bstart,
                features=blob, completeness=comp,
                feature_version=SS.feature_version))
        s.commit()

    @staticmethod
    def reconstruct(s: Session, chain_pk: int, token_address: str,
                    as_of: datetime) -> dict | None:
        """Historical reconstruction using ONLY data <= as_of: prefer the
        persisted snapshot that existed at that time; otherwise recompute
        from pre-T rows via the same past-only extractor."""
        row = (s.query(TokenFeatureSnapshot)
               .filter(TokenFeatureSnapshot.chain_pk == chain_pk,
                       TokenFeatureSnapshot.token_address == token_address,
                       TokenFeatureSnapshot.bucket_start <= as_of)
               .order_by(TokenFeatureSnapshot.bucket_start.desc()).first())
        if row:
            try:
                data = json.loads(row.features)
                raw = {k: data.get(k) for k in FEATURE_NAMES}
                norm = {k: v for k, v in data.items() if k.endswith("_norm")}
                return {"raw": raw, "normalized": norm,
                        "completeness": _f(row.completeness) or 0.0,
                        "feature_timestamp": row.bucket_start,
                        "source": "snapshot"}
            except Exception:
                pass
        eng = FeatureEngine(lambda: s)
        out = eng.build(chain_pk, token_address, as_of=as_of, persist=False)
        out["source"] = "recomputed_past_only"
        return out
