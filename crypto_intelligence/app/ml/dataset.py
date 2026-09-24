"""Phase 6 — ML dataset builder.

Joins Phase 4 signals + stored signal_features with Phase 5 historical
outcomes for one (target, horizon). OUTCOME COLUMNS ARE LABELS ONLY: they are
extracted into `y` / `meta` and physically cannot reach the feature vector
because vectorize() runs on the signal-feature map before any outcome field
is even read, and its leakage guard raises on forbidden keys.

Rows are ordered chronologically; the caller performs time-series-safe
splits (see training.chronological_split).
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.database.models import (Chain, HistoricalOutcome, Signal,
                                 SignalFeature, Token)
from app.ml.features import FEATURE_VERSION, vectorize
from config import ml_settings as MS

log = logging.getLogger("ml.dataset")

# valid binary targets -> HistoricalOutcome columns
BINARY_TARGETS = {
    "hit_5pct": ("hit_5pct", "positive"),
    "hit_10pct": ("hit_10pct", "positive"),
    "hit_20pct": ("hit_20pct", "positive"),
    "hit_minus_5pct": ("hit_minus_5pct", "negative"),
    "hit_minus_10pct": ("hit_minus_10pct", "negative"),
}
MULTICLASS_TARGET = "outcome_class"


def normalize_target(target: str) -> str:
    t = (target or "").strip().lower().replace("+", "hit_").replace("%", "pct")
    t = t.replace("hit_5pct", "hit_5pct")
    aliases = {"minus5pct": "hit_minus_5pct", "minus10pct": "hit_minus_10pct",
               "hit_-5pct": "hit_minus_5pct", "hit_-10pct": "hit_minus_10pct",
               "5pct": "hit_5pct", "10pct": "hit_10pct", "20pct": "hit_20pct"}
    return aliases.get(t, t if (t in BINARY_TARGETS or t == MULTICLASS_TARGET) else t)


def build_dataset(session: Session, *, target: str | None = None,
                  horizon: str | None = None,
                  start: datetime | None = None,
                  end: datetime | None = None,
                  require_ok_status: bool = True) -> dict:
    """Return {'rows':[...], 'columns':[...], 'status', 'counts'} where each
    row = {'signal_id','token_address','chain_pk','t', 'X'(dict), 'y', 'meta'}.

    status is OK or INSUFFICIENT_DATA — never fabricated rows."""
    target = normalize_target(target or MS.default_target)
    horizon = horizon or MS.default_horizon
    multi = target == MULTICLASS_TARGET
    if not multi and target not in BINARY_TARGETS:
        raise ValueError(f"unknown ML target: {target!r} "
                         f"(valid: {sorted(BINARY_TARGETS)} + {MULTICLASS_TARGET})")
    col, direction = (None, None) if multi else BINARY_TARGETS[target]

    q = (session.query(HistoricalOutcome, Signal, Chain.key, Token.category)
         .join(Signal, Signal.id == HistoricalOutcome.signal_id)
         .join(Chain, Chain.id == HistoricalOutcome.chain_pk)
         .outerjoin(Token, (Token.chain_pk == HistoricalOutcome.chain_pk)
                    & (Token.address == HistoricalOutcome.token_address))
         .filter(HistoricalOutcome.horizon == horizon))
    if require_ok_status:
        q = q.filter(HistoricalOutcome.data_status == "OK")
    if start:
        q = q.filter(HistoricalOutcome.signal_timestamp >= start)
    if end:
        q = q.filter(HistoricalOutcome.signal_timestamp < end)
    q = q.order_by(HistoricalOutcome.signal_timestamp.asc(),
                   HistoricalOutcome.id.asc())

    # preload per-signal feature maps (bounded memory: chunked by query order)
    rows: list[dict] = []
    feat_cache: dict[int, dict] = {}

    def _features(sig_id: int) -> dict:
        if sig_id not in feat_cache:
            fm = {f.feature_name: float(f.feature_value)
                  for f in session.query(SignalFeature)
                  .filter_by(signal_id=sig_id).all()}
            feat_cache.clear()          # keep memory bounded
            feat_cache[sig_id] = fm
        return feat_cache[sig_id]

    skipped_no_label = skipped_leak = 0
    for oc, sig, chain_key, category in q.yield_per(500):
        if multi:
            y = oc.outcome_class
            if y is None:
                skipped_no_label += 1
                continue
        else:
            y = getattr(oc, col)
            if y is None:
                skipped_no_label += 1
                continue
            y = 1 if bool(y) else 0

        signal_row = {
            "score": float(sig.score or 0), "confidence": float(sig.confidence or 0),
            "quality": float(sig.quality or 0), "risk_score": float(sig.risk_score or 0),
            "positive_score": float(sig.positive_score or 0),
            "negative_score": float(sig.negative_score or 0),
            "accumulation_score": _num(sig.accumulation_score),
            "distribution_score": _num(sig.distribution_score),
            "capital_inflow_score": _num(sig.capital_inflow_score),
            "capital_outflow_score": _num(sig.capital_outflow_score),
            "onchain_momentum_score": _num(sig.onchain_momentum_score),
            "confirmation_count": sig.confirmation_count,
            "category": category or "Unknown", "signal_type": sig.signal_type,
        }
        try:
            X, cols = vectorize(signal_row, _features(sig.id), chain_key=chain_key)
        except ValueError as e:      # leakage guard — explicit, never silent
            skipped_leak += 1
            log.error("dataset row rejected (leakage guard): %s", e)
            continue
        rows.append({
            "signal_id": sig.id, "token_address": sig.token_address,
            "chain_pk": sig.chain_pk, "t": sig.signal_timestamp,
            "X": X, "y": y,
            "meta": {"return_percent": _num(oc.return_percent),
                     "mfe_percent": _num(oc.mfe_percent),
                     "mae_percent": _num(oc.mae_percent),
                     "outcome_class": oc.outcome_class,
                     "outcome_id": oc.id},
        })

    counts = {"total": len(rows)}
    status = "OK"
    if len(rows) < (MS.min_train_samples + MS.min_validation_samples
                    + MS.min_test_samples):
        status = "INSUFFICIENT_DATA"
    result = {
        "rows": rows, "columns": cols_of(rows), "target": target,
        "horizon": horizon, "status": status, "counts": counts,
        "skipped": {"no_label": skipped_no_label, "leak_guard": skipped_leak},
        "feature_version": FEATURE_VERSION,
    }
    log.info("dataset built target=%s horizon=%s rows=%d status=%s",
             target, horizon, len(rows), status)
    return result


def cols_of(rows: list[dict]) -> list[str]:
    from app.ml.features import feature_columns
    return feature_columns() if rows else feature_columns()


def _num(v):
    return float(v) if v is not None else None
