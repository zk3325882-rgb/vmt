"""Phase 3 — Capital Flow Engine.

Incremental, DB-side aggregation of DEX flow into time buckets
(1m/5m/15m/1h/4h/24h per config). Rows are updated with deltas only — the
raw swap history is NEVER re-scanned per request (8 GB RAM friendly).

Metrics per token per bucket (all OBSERVED, never predictions):
  buy_volume_usd / sell_volume_usd / net_flow_usd = buy - sell
  buy_count / sell_count
  large_buy_volume / large_sell_volume   (> dex_settings.large_trade_min_usd)
  whale_buy_volume / whale_sell_volume   (wallet whale_score >= threshold)
  other_swap_volume / liquidity_change_usd

Multi-pool: pool-level rows live in dex_swaps; these aggregates intentionally
sum across pools WITHOUT double counting, because each user trade is stored
exactly once (unique tx_hash+log_index) after router-hop grouping.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.database.models import TokenFlowSnapshot
from config import dex_settings as DS

log = logging.getLogger("flow.engine")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def bucket_start(ts: datetime, bucket_seconds: int) -> datetime:
    epoch = int(ts.replace(tzinfo=timezone.utc if ts.tzinfo else
                          timezone.utc).timestamp())
    return datetime.fromtimestamp(epoch - epoch % bucket_seconds,
                                  tz=timezone.utc).replace(tzinfo=None)


class FlowEngine:
    """Aggregates classified swaps into token_flow_snapshots."""

    def record_swap(self, s: Session, chain_pk: int, token_address: str,
                    classification: str, usd: float | None,
                    ts: datetime, is_whale: bool,
                    large_floor: float | None = None) -> None:
        """Apply one swap delta to every configured bucket (DB-side upsert)."""
        if usd is None or usd <= 0 or classification == "UNKNOWN_SWAP":
            return
        large_floor = large_floor if large_floor is not None else DS.large_trade_min_usd
        kind = classification          # BUY_LIKE | SELL_LIKE | SWAP_OTHER
        for bs in DS.flow_buckets:
            bstart = bucket_start(ts, bs)
            vals = dict(chain_pk=chain_pk, token_address=token_address.lower(),
                        bucket_seconds=bs, bucket_start=bstart,
                        buy_volume_usd=usd if kind == "BUY_LIKE" else 0,
                        sell_volume_usd=usd if kind == "SELL_LIKE" else 0,
                        net_flow_usd=(usd if kind == "BUY_LIKE" else
                                      -usd if kind == "SELL_LIKE" else 0),
                        buy_count=1 if kind == "BUY_LIKE" else 0,
                        sell_count=1 if kind == "SELL_LIKE" else 0,
                        large_buy_volume=usd if (kind == "BUY_LIKE" and usd >= large_floor) else 0,
                        large_sell_volume=usd if (kind == "SELL_LIKE" and usd >= large_floor) else 0,
                        whale_buy_volume=usd if (kind == "BUY_LIKE" and is_whale) else 0,
                        whale_sell_volume=usd if (kind == "SELL_LIKE" and is_whale) else 0,
                        other_swap_volume=usd if kind == "SWAP_OTHER" else 0,
                        liquidity_change_usd=0, timestamp=ts)
            stmt = self._ins(s)(TokenFlowSnapshot).values(**vals)
            upd = {c: getattr(TokenFlowSnapshot, c) + getattr(stmt.excluded, c)
                   for c in ("buy_volume_usd", "sell_volume_usd", "net_flow_usd",
                             "buy_count", "sell_count", "large_buy_volume",
                             "large_sell_volume", "whale_buy_volume",
                             "whale_sell_volume", "other_swap_volume",
                             "liquidity_change_usd")}
            s.execute(stmt.on_conflict_do_update(
                index_elements=["chain_pk", "token_address", "bucket_seconds",
                                "bucket_start"], set_=upd))

    def record_liquidity_change(self, s: Session, chain_pk: int,
                                token_address: str, change_usd: float,
                                ts: datetime) -> None:
        if not change_usd:
            return
        for bs in DS.flow_buckets:
            bstart = bucket_start(ts, bs)
            stmt = self._ins(s)(TokenFlowSnapshot).values(
                chain_pk=chain_pk, token_address=token_address.lower(),
                bucket_seconds=bs, bucket_start=bstart,
                liquidity_change_usd=change_usd, timestamp=ts)
            s.execute(stmt.on_conflict_do_update(
                index_elements=["chain_pk", "token_address", "bucket_seconds",
                                "bucket_start"],
                set_={"liquidity_change_usd":
                      TokenFlowSnapshot.liquidity_change_usd
                      + stmt.excluded.liquidity_change_usd}))

    @staticmethod
    def _ins(s: Session):
        dialect = s.bind.dialect.name if s.bind else "sqlite"
        return pg_insert if dialect == "postgresql" else sqlite_insert

    # ---------------- read-side helpers (used by APIs) --------------------
    @staticmethod
    def get_flow(s: Session, chain_pk: int, token_address: str,
                 bucket_seconds: int, now: datetime | None = None) -> dict:
        """Sum current + previous partial buckets so e.g. '1h flow' covers a
        rolling hour even though rows are aligned to bucket boundaries."""
        now = now or utcnow()
        start = now - timedelta(seconds=bucket_seconds * 2)
        rows = (s.query(TokenFlowSnapshot)
                .filter(TokenFlowSnapshot.chain_pk == chain_pk,
                        TokenFlowSnapshot.token_address == token_address.lower(),
                        TokenFlowSnapshot.bucket_seconds == bucket_seconds,
                        TokenFlowSnapshot.bucket_start >= start)
                .order_by(TokenFlowSnapshot.bucket_start.desc()).limit(3).all())
        agg = {c: 0.0 for c in ("buy_volume_usd", "sell_volume_usd",
                                "net_flow_usd", "large_buy_volume",
                                "large_sell_volume", "whale_buy_volume",
                                "whale_sell_volume", "other_swap_volume",
                                "liquidity_change_usd")}
        counts = {"buy_count": 0, "sell_count": 0}
        for r in rows:
            for k in agg:
                agg[k] += float(getattr(r, k) or 0)
            for k in counts:
                counts[k] += int(getattr(r, k) or 0)
        agg.update(counts)
        return agg

    @staticmethod
    def flow_profile(s: Session, chain_pk: int, token_address: str,
                     now: datetime | None = None) -> dict:
        """5m/15m/1h/4h/24h profile used by dashboard + Phase 4 features."""
        out = {}
        labels = {60: "1m", 300: "5m", 900: "15m", 3600: "1h",
                  14400: "4h", 86400: "24h"}
        for bs, name in labels.items():
            out[name] = FlowEngine.get_flow(s, chain_pk, token_address, bs, now)
        return out
