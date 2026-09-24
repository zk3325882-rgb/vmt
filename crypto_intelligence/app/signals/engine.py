"""Phase 4 - Signal lifecycle engine.

Creates/updates Signals from feature vectors produced by the Feature Engine,
enforces dedup/cooldown/expiry state machine, persists every input feature
(signal_features) so signals are fully explainable and reproducible, and
emits deduplicated alerts.

NO LOOK-AHEAD GUARANTEE (enforced structurally):
- score_signal() receives a feature dict built with as_of=T; it never opens
  a DB session to read market data.
- persist_signal() refuses features whose timestamps are > signal T.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.database.models import (
    AlertLog, Signal, SignalFeature, Token,
)
from app.signals.scoring import (
    build_explanation, compute_scores, detect_signal_type,
)
from config import signal_settings as SS

log = logging.getLogger("signals.engine")


def score_signal(features: dict) -> dict | None:
    """Pure function: features available at T -> signal payload or None.

    `features` = {'raw':..., 'normalized':..., 'completeness':...,
                  'feature_timestamp':...}. Uses ONLY those values — no DB,
    no future data.
    """
    raw = dict(features.get("raw") or {})
    norm = dict(features.get("normalized") or {})
    if not raw:
        return None
    raw.setdefault("_completeness", features.get("completeness", 0.0))
    scores = compute_scores(raw, norm)
    stype = detect_signal_type(raw, norm, scores)
    text, reasons, contra = build_explanation(raw, norm, scores, stype)
    # NEUTRAL_SIGNAL below alert threshold is noise -> no signal
    if stype == "NEUTRAL_SIGNAL" and scores["score"] < SS.min_score_alert:
        return None
    return {
        "signal_type": stype,
        "scores": scores,
        "explanation": text,
        "reasons": reasons,
        "contradicting": contra,
        "features": {k: v for k, v in raw.items() if not k.startswith("_")},
        "normalized": norm,
        "feature_timestamp": features.get("feature_timestamp"),
        "completeness": features.get("completeness", 0.0),
    }


class SignalEngine:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    # ------------------------------------------------------------------
    def evaluate_token(self, chain_pk: int, token_address: str,
                       as_of: datetime | None = None) -> dict | None:
        """Live path: build features (past-only) then score+persist."""
        from app.features.engine import FeatureEngine
        as_of = as_of or datetime.utcnow()
        feats = FeatureEngine(self.session_factory).build(
            chain_pk, token_address, as_of=as_of, persist=True)
        payload = score_signal(feats)
        if payload is None:
            return None
        return self.persist_signal(chain_pk, token_address, payload, as_of)

    # ------------------------------------------------------------------
    def persist_signal(self, chain_pk: int, token_address: str,
                       payload: dict, t: datetime) -> dict:
        """Create / strengthen / update a Signal row with lifecycle rules."""
        s = payload["scores"]
        stype = payload["signal_type"]
        # integrity guard: features must be strictly older than signal time
        fts = payload.get("feature_timestamp")
        if isinstance(fts, datetime) and fts > t:
            raise ValueError("look-ahead rejected: feature timestamp after T")
        expires = t + timedelta(seconds=SS.active_ttl_seconds)

        with self.session_factory() as db:
            prior = (db.query(Signal)
                     .filter(Signal.chain_pk == chain_pk,
                             Signal.token_address == token_address,
                             Signal.signal_type == stype,
                             Signal.status.in_(("NEW", "ACTIVE", "STRENGTHENING",
                                                "WEAKENING")),
                             Signal.expires_at > t)
                     .order_by(Signal.signal_timestamp.desc()).first())
            action = "CREATE"
            sig = prior
            if prior is not None:
                delta = s["score"] - float(prior.score)
                if abs(delta) < SS.update_threshold:
                    prior.confidence = max(float(prior.confidence), s["confidence"])
                    prior.updated_at = datetime.utcnow()
                    prior.confirmation_count += 1
                    action = "KEEP"
                    return self._out(prior, action, payload)
                prior.status = ("STRENGTHENING" if delta > 0 else "WEAKENING")
                prior.score = s["score"]
                prior.confidence = s["confidence"]
                prior.quality = s["quality"]
                prior.risk_score = s["risk_score"]
                prior.positive_score = s["positive_score"]
                prior.negative_score = s["negative_score"]
                prior.accumulation_score = s["accumulation_score"]
                prior.distribution_score = s["distribution_score"]
                prior.capital_inflow_score = s["capital_inflow_score"]
                prior.capital_outflow_score = s["capital_outflow_score"]
                prior.onchain_momentum_score = s["onchain_momentum_score"]
                prior.band = s["band"]
                prior.confirmation_count += 1
                prior.explanation = payload["explanation"]
                prior.reasons_json = json.dumps(payload["reasons"])[:4000]
                prior.confirmed_features = json.dumps(
                    [r["feature"] for r in payload["reasons"]])[:1000]
                prior.contradicting_features = json.dumps(
                    payload["contradicting"])[:1000]
                prior.feature_timestamp = fts
                prior.data_timestamp = fts
                prior.updated_at = datetime.utcnow()
                sig = prior
                action = "UPDATE"
            else:
                tok = db.query(Token).filter_by(chain_pk=chain_pk,
                                                address=token_address).first()
                sig = Signal(
                    chain_pk=chain_pk, token_address=token_address,
                    token_symbol=(tok.symbol if tok else None),
                    signal_type=stype, score=s["score"],
                    confidence=s["confidence"], quality=s["quality"],
                    risk_score=s["risk_score"],
                    positive_score=s["positive_score"],
                    negative_score=s["negative_score"],
                    accumulation_score=s["accumulation_score"],
                    distribution_score=s["distribution_score"],
                    capital_inflow_score=s["capital_inflow_score"],
                    capital_outflow_score=s["capital_outflow_score"],
                    onchain_momentum_score=s["onchain_momentum_score"],
                    band=s["band"], status="NEW",
                    explanation=payload["explanation"],
                    reasons_json=json.dumps(payload["reasons"])[:4000],
                    confirmed_features=json.dumps(
                        [r["feature"] for r in payload["reasons"]])[:1000],
                    contradicting_features=json.dumps(
                        payload["contradicting"])[:1000],
                    confirmation_count=1,
                    signal_timestamp=t, feature_timestamp=fts,
                    data_timestamp=fts, expires_at=expires,
                    signal_engine_version=SS.version,
                    feature_version=SS.feature_version)
                db.add(sig)
                db.flush()
            # store every numeric input (explainability + backtesting + ML)
            all_feats = {**payload["features"],
                         **{k: v for k, v in payload["normalized"].items()}}
            contrib = {r["feature"]: r["contribution"]
                       for r in payload["reasons"]}
            for name, value in all_feats.items():
                if value is None or not isinstance(value, (int, float)):
                    continue
                exists = (db.query(SignalFeature)
                          .filter_by(signal_id=sig.id, feature_name=name).first())
                if exists:
                    continue
                db.add(SignalFeature(
                    signal_id=sig.id, feature_name=name,
                    feature_value=float(value),
                    feature_normalized=(float(value) / 100.0
                                        if name.endswith("_score")
                                        else None),
                    feature_contribution=float(contrib.get(name, 0.0))))
            db.commit()
            db.refresh(sig)
            result = self._out(sig, action, payload)
        if action == "CREATE":
            self.maybe_alert(result)
        return result

    @staticmethod
    def _out(sig: Signal, action: str, payload: dict) -> dict:
        return {"id": sig.id, "action": action,
                "chain_pk": sig.chain_pk,
                "token_address": sig.token_address,
                "symbol": sig.token_symbol,
                "signal_type": sig.signal_type,
                "score": float(sig.score), "band": sig.band,
                "confidence": float(sig.confidence),
                "quality": float(sig.quality),
                "risk_score": float(sig.risk_score),
                "status": sig.status,
                "signal_timestamp": sig.signal_timestamp.isoformat(),
                "explanation": sig.explanation,
                "reasons": payload["reasons"],
                "contradicting": payload["contradicting"],
                "engine_version": sig.signal_engine_version}

    # ------------------------------------------------------------------
    def maybe_alert(self, result: dict) -> bool:
        """Deduplicated alert via unique(token,type,day) key + cooldown."""
        if result["score"] < SS.min_score_alert:
            return False
        day = datetime.utcnow().strftime("%Y%m%d")
        key = f"{result['token_address']}|{result['signal_type']}|{day}"
        with self.session_factory() as db:
            if db.query(AlertLog).filter_by(dedup_key=key).first():
                return False
            msg = (f"[SIGNAL {result['score']:.0f}/100 {result['band']}] "
                   f"{result['signal_type']} {result['symbol'] or ''} "
                   f"@chain{result['chain_pk']} — evidence intensity only, "
                   f"not a price prediction.")
            db.add(AlertLog(signal_id=result["id"], channel="log",
                            alert_type=result["signal_type"],
                            message=msg, dedup_key=key))
            sig = db.get(Signal, result["id"])
            if sig:
                sig.alert_sent = True
                sig.last_alert_at = datetime.utcnow()
            db.commit()
        log.info(msg)
        return True

    # ------------------------------------------------------------------
    def expire_stale(self, now: datetime | None = None) -> int:
        now = now or datetime.utcnow()
        with self.session_factory() as db:
            n = (db.query(Signal)
                 .filter(Signal.status.in_(("NEW", "ACTIVE", "STRENGTHENING",
                                            "WEAKENING")),
                         Signal.expires_at <= now)
                 .update({Signal.status: "EXPIRED"},
                         synchronize_session=False))
            db.commit()
        return n

    # ------------------------------------------------------------------
    def run_cycle(self, tokens: list[tuple[int, str]],
                  as_of: datetime | None = None) -> list[dict]:
        out = []
        for chain_pk, addr in tokens:
            try:
                r = self.evaluate_token(chain_pk, addr, as_of)
                if r:
                    out.append(r)
            except Exception as e:  # one bad token must never kill the loop
                log.warning("signal eval failed %s/%s: %s", chain_pk, addr, e)
        self.expire_stale(as_of)
        return out
