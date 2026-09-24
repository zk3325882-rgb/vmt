"""ERC-20 Transfer event decoding + persistence pipeline.

Decode log -> discover/register token -> fetch metadata if missing ->
price lookup -> USD value -> anomaly scoring -> batched DB writes.
One bad log never stops the scanner (per-item try/except).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from app.blockchain.base import BaseChainAdapter, LogEntry
from app.blockchain.evm import is_valid_address
from app.database.models import (
    Chain, LargeTransaction, MarketSnapshot, Token, TokenTransfer,
)
from app.market.prices import PriceService
from app.tokens.discovery import TokenDiscoveryService
from app.tokens.metadata import fetch_metadata
from app.transactions.large_transactions import (
    RollingStats, classify_transfer, compute_anomaly,
)
from config import TRANSFER_TOPIC, settings

log = logging.getLogger("transfers")


def decode_transfer(entry: LogEntry) -> tuple[str, str, int] | None:
    """Return (from, to, raw_amount) from a standard ERC-20 Transfer log."""
    try:
        if len(entry.topics) != 3 or not entry.data or entry.data == "0x":
            return None
        frm = "0x" + entry.topics[1][-40:].lower()
        to = "0x" + entry.topics[2][-40:].lower()
        amount = int(entry.data, 16)
        if not (is_valid_address(frm) and is_valid_address(to)):
            return None
        return frm, to, amount
    except (ValueError, IndexError):
        return None


class TransferCollector:
    def __init__(self, adapter: BaseChainAdapter, chain_key: str,
                 session_factory, discovery: TokenDiscoveryService,
                 price_service: PriceService):
        self.adapter = adapter
        self.chain_key = chain_key
        self.session_factory = session_factory
        self.discovery = discovery
        self.prices = price_service
        self.stats = RollingStats()
        self.meta_cache: dict[str, dict] = {}   # bounded below
        self._pending_meta: set[str] = set()
        self.chain_pk: int | None = None
        # rolling per-token volume estimate (sum of recent valued transfers)
        self.volume_window: dict[str, list] = {}
        self.processed_count = 0

    async def _ensure_chain_pk(self) -> int:
        if self.chain_pk is None:
            def _q():
                with self.session_factory() as s:
                    return s.query(Chain.id).filter_by(key=self.chain_key).scalar()
            self.chain_pk = await asyncio.to_thread(_q)
        return self.chain_pk

    async def process_logs(self, logs: list[LogEntry],
                           block_ts: dict[int, datetime]) -> None:
        chain_pk = await self._ensure_chain_pk()
        pairs = await asyncio.to_thread(self.discovery.known_pairs, chain_pk)
        rows_transfers = []
        rows_large = []
        for entry in logs:
            try:
                if not entry.topics or entry.topics[0].lower() != TRANSFER_TOPIC:
                    continue
                decoded = decode_transfer(entry)
                if not decoded:
                    continue
                frm, to, raw_amount = decoded
                token_addr = entry.address.lower()
                ts = block_ts.get(entry.block_number) or datetime.now(timezone.utc).replace(tzinfo=None)

                # --- token discovery (never discard unknown tokens) ---
                await asyncio.to_thread(
                    self.discovery.register_token, chain_pk, token_addr,
                    "transfer_event", None, entry.block_number, ts)

                # --- metadata (lazy, one background attempt per token) ---
                meta = self.meta_cache.get(token_addr)
                if meta is None and token_addr not in self._pending_meta:
                    self._pending_meta.add(token_addr)
                    asyncio.create_task(self._fetch_meta(chain_pk, token_addr))
                    meta = {"symbol": None, "name": None, "decimals": None}
                decimals = (meta or {}).get("decimals")
                symbol = (meta or {}).get("symbol")

                # normalized amount with Decimal precision
                if decimals is None:
                    norm = Decimal(raw_amount)   # unknown decimals: keep raw scale
                else:
                    norm = Decimal(raw_amount) / (Decimal(10) ** decimals)

                # --- pricing / USD valuation ---
                price, source = await self.prices.get_price(token_addr)
                if price is None and decimals is None and raw_amount > 0:
                    # likely undecoded metadata yet; retry soon instead of caching negative
                    self.prices._neg_cache.pop(token_addr, None)
                usd = None
                if price is not None and decimals is not None:
                    try:
                        usd = float(norm * Decimal(str(price)))
                    except (InvalidOperation, OverflowError):
                        usd = None

                txtype, flow = classify_transfer(self.chain_key, frm, to, pairs)

                rows_transfers.append(dict(
                    chain_pk=chain_pk, tx_hash=entry.transaction_hash,
                    log_index=entry.log_index, block_number=entry.block_number,
                    token_address=token_addr, token_symbol=symbol,
                    token_decimals=decimals, from_address=frm, to_address=to,
                    raw_amount=raw_amount, normalized_amount=norm,
                    usd_value=usd, flow=flow, transaction_type=txtype,
                    timestamp=ts))

                # --- anomaly scoring on valued transfers ---
                vol24 = None
                vw = self.volume_window.get(token_addr)
                if vw:
                    vol24 = sum(v for _, v in vw)
                liq = (meta or {}).get("liquidity_usd")
                res = compute_anomaly(token_addr, usd, self.stats, vol24, liq)
                if usd is not None:
                    self.stats.add(token_addr, usd)
                    self._add_volume(token_addr, usd, ts)
                if (usd is not None and usd >= settings.min_usd_alert
                        and res.anomaly_score >= settings.min_anomaly_score):
                    rows_large.append(dict(
                        chain_pk=chain_pk, tx_hash=entry.transaction_hash,
                        log_index=entry.log_index, token_address=token_addr,
                        token_symbol=symbol, from_address=frm, to_address=to,
                        amount=norm, usd_value=usd,
                        relative_size=res.relative_size,
                        volume_ratio=res.volume_ratio,
                        liquidity_ratio=res.liquidity_ratio,
                        percentile=res.percentile,
                        anomaly_score=res.anomaly_score,
                        flow=flow, transaction_type=txtype, timestamp=ts))
                    log.info("LARGE %s %s %s $%s rel=%s score=%s",
                             symbol or token_addr[:10], flow, f"{norm:,.2f}",
                             f"{usd:,.0f}", res.relative_size, res.anomaly_score)
            except Exception as e:
                log.warning("skipping bad log %s/%s: %s",
                            entry.transaction_hash[:12], entry.log_index, str(e)[:140])
                continue
        if rows_transfers or rows_large:
            await asyncio.to_thread(self._write, rows_transfers, rows_large, chain_pk)
        self.processed_count += len(rows_transfers)

    def _add_volume(self, token_addr: str, usd: float, ts: datetime) -> None:
        window = self.volume_window.setdefault(token_addr, [])
        cutoff = ts.timestamp() - 86400
        window.append((ts.timestamp(), usd))
        if len(window) > 2000:   # RAM bound
            del window[:len(window) - 2000]
        while window and window[0][0] < cutoff:
            window.pop(0)

    def _write(self, rows_t: list[dict], rows_l: list[dict], chain_pk: int) -> None:
        """Batched inserts with duplicate protection (ON CONFLICT DO NOTHING)."""
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        with self.session_factory() as s:
            dialect = s.bind.dialect.name if s.bind else "sqlite"
            ins_cls = pg_insert if dialect == "postgresql" else sqlite_insert
            for chunk_start in range(0, len(rows_t), settings.db_batch_size):
                chunk = rows_t[chunk_start:chunk_start + settings.db_batch_size]
                stmt = ins_cls(TokenTransfer).values(chunk).on_conflict_do_nothing(
                    index_elements=["chain_pk", "tx_hash", "log_index"])
                s.execute(stmt)
            for chunk_start in range(0, len(rows_l), settings.db_batch_size):
                chunk = rows_l[chunk_start:chunk_start + settings.db_batch_size]
                stmt = ins_cls(LargeTransaction).values(chunk).on_conflict_do_nothing(
                    index_elements=["chain_pk", "tx_hash", "log_index"])
                s.execute(stmt)
            # update token counters & market snapshot for priced tokens
            seen_tokens: dict[str, int] = {}
            for r in rows_t:
                seen_tokens[r["token_address"]] = seen_tokens.get(r["token_address"], 0) + 1
            for addr, n in seen_tokens.items():
                t = s.query(Token).filter_by(chain_pk=chain_pk, address=addr).first()
                if t:
                    t.transfer_count = (t.transfer_count or 0) + n
                    latest = max(r["timestamp"] for r in rows_t
                                 if r["token_address"] == addr)
                    t.last_seen = max(t.last_seen or latest, latest)
            for r in rows_l:
                s.add(MarketSnapshot(chain_pk=chain_pk, token_address=r["token_address"],
                                     price_usd=None, source="transfer",
                                     timestamp=r["timestamp"]))
            s.commit()

    async def _fetch_meta(self, chain_pk: int, token_addr: str) -> None:
        """Background metadata fetch; failure just leaves token 'unknown'."""
        try:
            meta = await fetch_metadata(self.adapter, token_addr)
            self.meta_cache[token_addr] = meta
            if len(self.meta_cache) > 5000:   # RAM bound
                self.meta_cache.pop(next(iter(self.meta_cache)))
            await asyncio.to_thread(self.discovery.register_token, chain_pk,
                                    token_addr, "transfer_event", meta)
        except Exception as e:
            log.debug("metadata fetch failed %s: %s", token_addr[:10], e)
            self.meta_cache[token_addr] = {"symbol": None, "name": None, "decimals": None}
        finally:
            self._pending_meta.discard(token_addr)
