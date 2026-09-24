"""Phase 6 — ML feature vectorization with strict no-future-leakage rules.

Inputs are the Phase 4 signal feature snapshots (signal_features rows and/or
the persisted token_feature_snapshots blob). Every value in a snapshot was
computed from data <= signal time T, so reusing them keeps leakage out by
construction. This module additionally GUARANTEES that outcome/label columns
can never enter the feature matrix via an explicit deny-list enforced at
vectorize() time (raise, not silent drop).
"""
from __future__ import annotations

import math

# ---------------------------------------------------------------------------
# Deny list: any key containing these substrings is a FUTURE-OUTCOME field
# (Phase 5 labels) or identity column and must never be a model input.
# ---------------------------------------------------------------------------
FORBIDDEN_FEATURE_SUBSTRINGS = (
    "price_after", "return_percent", "mfe_", "mae_", "max_drawdown",
    "drawdown_before", "time_to_", "hit_5", "hit_10", "hit_20",
    "hit_minus", "outcome_class", "actual_outcome", "horizon_seconds",
    "split", "label", "target",
)
FORBIDDEN_FEATURE_EXACT = {
    "id", "signal_id", "token_id", "chain_pk", "chain_id",
    "signal_timestamp", "feature_timestamp", "created_at", "updated_at",
    "computed_at", "expires_at", "tx_hash", "token_address", "address",
    "model_id", "backtest_id", "data_status", "signal_type", "status",
    "band", "explanation", "reasons_json", "confirmed_features",
    "contradicting_features", "category", "symbol", "source",
}


def is_forbidden_feature(name: str) -> bool:
    n = name.lower()
    if n in FORBIDDEN_FEATURE_EXACT:
        return True
    return any(s in n for s in FORBIDDEN_FEATURE_SUBSTRINGS)


# ---------------------------------------------------------------------------
# Ordered numeric feature schema (stable across versions; changing this
# list requires bumping FEATURE_VERSION).
# ---------------------------------------------------------------------------
NUMERIC_FEATURES = [
    # signal-level scores (available at T)
    "score", "confidence", "quality", "risk_score",
    "positive_score", "negative_score",
    "accumulation_score", "distribution_score",
    "capital_inflow_score", "capital_outflow_score",
    "onchain_momentum_score", "confirmation_count",
    # flow features (raw, available at T)
    "buy_volume_5m", "sell_volume_5m", "net_flow_5m",
    "buy_volume_15m", "sell_volume_15m", "net_flow_15m",
    "buy_volume_1h", "sell_volume_1h", "net_flow_1h",
    "buy_volume_4h", "sell_volume_4h", "net_flow_4h",
    "buy_volume_24h", "sell_volume_24h", "net_flow_24h",
    "buy_sell_ratio", "buy_sell_imbalance", "flow_acceleration_pct",
    "flow_anomaly_score", "relative_volume",
    # whale / dex
    "whale_buy_volume", "whale_sell_volume", "whale_net_flow",
    "whale_participation_ratio", "large_buy_volume", "large_sell_volume",
    # liquidity
    "liquidity_usd", "liquidity_change_1h", "liquidity_change_percent",
    # transactions
    "large_transaction_count", "large_inflow", "large_outflow",
    "transaction_anomaly", "max_relative_size", "largest_transaction_usd",
    # wallet / whale events
    "whale_event_count_24h", "max_whale_score", "active_wallet_count_1h",
    # market (pre-T only)
    "price", "volume", "volume_current_1h", "volume_prior_2h_avg",
    "price_change_pct_per_hour",
    # token context
    "token_age_hours",
]

# normalized variants produced by Phase 4 normalize_features()
NORM_SUFFIX = "_norm"

# categorical one-hot vocabularies (safe encoding, no identity leak)
CATEGORY_VOCAB = ["Layer 1", "Layer 2", "Meme", "DeFi", "Stablecoin",
                  "Gaming", "AI", "RWA", "New Token", "Unknown"]
CHAIN_VOCAB_KEYS = ["ethereum", "bsc"]          # extended when new chains added
SIGNAL_TYPE_VOCAB = [
    "ACCUMULATION_SIGNAL", "DISTRIBUTION_SIGNAL", "BULLISH_FLOW_SIGNAL",
    "BEARISH_FLOW_SIGNAL", "WHALE_ACTIVITY_SIGNAL", "LIQUIDITY_RISK_SIGNAL",
    "BREAKOUT_LIKE_FLOW_SIGNAL", "CAPITAL_INFLOW_SIGNAL",
    "CAPITAL_OUTFLOW_SIGNAL", "ANOMALY_SIGNAL", "NEUTRAL_SIGNAL",
]

FEATURE_VERSION = "6.0.0"


def _clean(v):
    """float or None; NaN/inf sanitized to None (never silently zeroed)."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def feature_columns(chain_keys=("ethereum", "bsc")) -> list[str]:
    cols: list[str] = []
    for f in NUMERIC_FEATURES:
        cols.append(f)
        cols.append(f + NORM_SUFFIX)
    cols += [f"cat_{c.lower().replace(' ', '_')}" for c in CATEGORY_VOCAB]
    cols += [f"chain_{k}" for k in chain_keys]
    cols += [f"type_{t}" for t in SIGNAL_TYPE_VOCAB]
    cols += ["has_price", "has_liquidity", "has_whale_data", "has_dex_flow"]
    return cols


def vectorize(signal_row: dict, feature_map: dict, *,
              chain_key: str | None = None,
              strict: bool = True) -> tuple[dict, list[str]]:
    """Build the ordered feature dict for ONE signal.

    `signal_row`: dict of Signal columns (score, confidence, ..., category,
    signal_type). `feature_map`: stored signal_features (name -> value) or a
    Phase 4 snapshot dict {raw..., *_norm...}.

    Raises ValueError on forbidden keys when strict=True — future-outcome
    fields can NEVER enter the matrix silently.
    """
    src = dict(feature_map or {})
    # merge non-conflicting scalar signal fields
    for k in ("score", "confidence", "quality", "risk_score",
              "positive_score", "negative_score", "accumulation_score",
              "distribution_score", "capital_inflow_score",
              "capital_outflow_score", "onchain_momentum_score",
              "confirmation_count"):
        if k not in src and signal_row.get(k) is not None:
            src[k] = signal_row.get(k)

    if strict:
        bad = [k for k in src if is_forbidden_feature(k)]
        if bad:
            raise ValueError(
                f"leakage guard: outcome/identity fields in feature map: {sorted(bad)}")

    vec: dict = {}
    for f in NUMERIC_FEATURES:
        vec[f] = _clean(src.get(f))
        nv = src.get(f + NORM_SUFFIX)
        if nv is None and f.endswith("_score"):     # *_score already 0..100
            base = _clean(src.get(f))
            nv = base / 100.0 if base is not None else None
        vec[f + NORM_SUFFIX] = _clean(nv)

    cat = (signal_row.get("category") or "").strip()
    for c in CATEGORY_VOCAB:
        vec[f"cat_{c.lower().replace(' ', '_')}"] = 1.0 if cat == c else 0.0
    ck = (chain_key or signal_row.get("chain_key") or "").lower()
    for k in CHAIN_VOCAB_KEYS:
        vec[f"chain_{k}"] = 1.0 if ck == k else 0.0
    st = signal_row.get("signal_type") or ""
    for t in SIGNAL_TYPE_VOCAB:
        vec[f"type_{t}"] = 1.0 if st == t else 0.0

    vec["has_price"] = 1.0 if vec.get("price") is not None else 0.0
    vec["has_liquidity"] = 1.0 if vec.get("liquidity_usd") is not None else 0.0
    vec["has_whale_data"] = 1.0 if (vec.get("whale_event_count_24h") is not None
                                    or vec.get("max_whale_score") is not None) else 0.0
    vec["has_dex_flow"] = 1.0 if vec.get("buy_volume_1h") is not None else 0.0
    return vec, feature_columns()


def to_matrix(rows: list[dict], columns: list[str]):
    """list-of-dicts -> numpy array with median imputation stats returned.

    Missing values are imputed per-column with TRAIN-set medians (fit on
    train only — never on val/test — to avoid statistical leakage), and the
    imputation constants are returned so live prediction reuses identical
    values. Indicator columns (has_*/cat_*/chain_*/type_*) keep NaN->0.
    """
    import numpy as np
    X = np.full((len(rows), len(columns)), np.nan, dtype="float64")
    for i, r in enumerate(rows):
        for j, c in enumerate(columns):
            v = r.get(c)
            if v is not None:
                X[i, j] = v
    indicator = np.array([c.startswith(("has_", "cat_", "chain_", "type_"))
                          for c in columns])
    return X, indicator


def fit_imputation(X_train: np.ndarray, indicator: np.ndarray):
    import numpy as np
    medians = np.zeros(X_train.shape[1])
    for j in range(X_train.shape[1]):
        col = X_train[:, j]
        good = col[~np.isnan(col)]
        medians[j] = float(np.median(good)) if good.size else 0.0
    return medians


def apply_imputation(X: np.ndarray, medians: np.ndarray, indicator: np.ndarray):
    import numpy as np
    out = X.copy()
    nan_mask = np.isnan(out)
    fill = np.where(indicator, 0.0, medians)[None, :]
    out[nan_mask] = np.broadcast_to(fill, out.shape)[nan_mask]
    return out
