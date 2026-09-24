"""Per-chain live block scanning loop.

Responsibilities:
 - poll new blocks (with confirmation margin)
 - fetch Transfer + DEX PairCreated logs for each range
 - hand logs to collectors, persist blocks/txs/scanner_state
 - resume from last_processed_block after restart
 - survive RPC failures (adapter retries; loop sleeps and continues)
Processing is strictly block-range streamed in chunks -> bounded RAM.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.blockchain.base import BaseChainAdapter
from app.collectors.dex_collector import DexCollector, decode_pair_created
from app.collectors.transfer_collector import TransferCollector
from app.database.models import Block, Chain, ScannerState, Transaction
from config import TRANSFER_TOPIC, settings

log = logging.getLogger("blocks")

CONFIRMATIONS = 2  # small safety margin against reorgs


class BlockCollector:
    def __init__(self, adapter: BaseChainAdapter, chain_key: str, session_factory,
                 transfer_collector: TransferCollector,
                 dex_collector: DexCollector, discovery):
        self.adapter = adapter
        self.chain_key = chain_key
        self.session_factory = session_factory
        self.transfers = transfer_collector
        self.dex = dex_collector
        self.discovery = discovery
        self.running = False
        self.chain_pk: int | None = None
        self.head = 0
        self.last_error: str | None = None

    # ---------- scanner state persistence ----------
    def _load_state(self) -> tuple[int, int]:
        with self.session_factory() as s:
            pk = s.query(Chain.id).filter_by(key=self.chain_key).scalar()
            if pk is None:
                return 0, 0
            st = s.get(ScannerState, pk)
            return pk, (st.last_processed_block if st else 0)

    def _save_state(self, chain_pk: int, block_number: int, head: int) -> None:
        with self.session_factory() as s:
            st = s.get(ScannerState, chain_pk)
            if st:
                st.last_processed_block = block_number
            else:
                s.add(ScannerState(chain_pk=chain_pk, last_processed_block=block_number))
            c = s.get(Chain, chain_pk)
            if c:
                c.head_block = head
            s.commit()

    def _persist_block_and_txs(self, chain_pk: int, header, txs) -> dict:
        ts = datetime.fromtimestamp(header.timestamp, tz=timezone.utc).replace(tzinfo=None)
        with self.session_factory() as s:
            dialect = s.bind.dialect.name
            ins_cls = pg_insert if dialect == "postgresql" else sqlite_insert
            stmt = ins_cls(Block).values(
                chain_pk=chain_pk, block_number=header.number,
                block_hash=header.hash, timestamp=ts
            ).on_conflict_do_nothing(index_elements=["chain_pk", "block_number"])
            s.execute(stmt)
            if txs:
                rows = [dict(chain_pk=chain_pk, tx_hash=t.hash,
                             block_number=t.block_number, from_address=t.from_address,
                             to_address=t.to_address, timestamp=ts, status=t.status)
                        for t in txs if t.hash]
                if rows:
                    stmt = ins_cls(Transaction).values(rows).on_conflict_do_nothing(
                        index_elements=["tx_hash"])
                    try:
                        s.execute(stmt)
                    except Exception:
                        s.rollback()  # tx-level unique clash on multi-chain same hash
            s.commit()
        return {header.number: ts}

    def _register_pairs(self, chain_pk: int, pair_events, ts_map) -> None:
        for dex_cfg, entry in pair_events:
            decoded = decode_pair_created(entry)
            if not decoded:
                continue
            t0, t1, pair = decoded
            self.discovery.register_pair(chain_pk, pair, dex_cfg.name, t0, t1,
                                         block_number=entry.block_number,
                                         ts=ts_map.get(entry.block_number))

    # ---------- main loop ----------
    async def run(self) -> None:
        self.running = True
        self.chain_pk, last = await asyncio.to_thread(self._load_state)
        if self.chain_pk is None:
            log.error("[%s] chain not seeded in DB", self.chain_key)
            return
        cfg = settings.chains[self.chain_key]
        if last <= 0:
            # Retry with capped exponential backoff instead of recursing,
            # so a sustained RPC outage cannot blow the stack.
            delay = 5
            while True:
                try:
                    head = await self.adapter.block_number()
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning("[%s] initial block_number failed (retry in %ds): %s",
                                self.chain_key, delay, e)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 60)
            last = max(cfg.start_block, head - settings.start_lookback) - 1
            log.info("[%s] first run: starting near head at %d", self.chain_key, last + 1)
        else:
            log.info("[%s] resuming from block %d", self.chain_key, last + 1)

        while self.running:
            try:
                head = await self.adapter.block_number()
                self.head = head
                target = head - CONFIRMATIONS
                if target <= last:
                    await asyncio.sleep(settings.scan_interval)
                    continue
                # chunk the catch-up range so eth_getLogs limits are respected
                frm = last + 1
                to = min(target, frm + settings.poll_log_chunk - 1)
                await self._process_range(frm, to)
                last = to
                await asyncio.to_thread(self._save_state, self.chain_pk, last, head)
                self.last_error = None
                if to >= target:
                    await asyncio.sleep(settings.scan_interval)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = str(e)[:200]
                log.warning("[%s] scan loop error (will retry): %s",
                            self.chain_key, self.last_error)
                await asyncio.sleep(min(30, settings.scan_interval * 4))

    async def _process_range(self, frm: int, to: int) -> None:
        # 1. headers for timestamps (fetch only when small range; else approximate now)
        ts_map: dict[int, datetime] = {}
        if to - frm <= 8:
            for n in range(frm, to + 1):
                hdr = await self.adapter.get_block_header(n)
                if hdr:
                    ts_map.update(await asyncio.to_thread(
                        self._persist_block_and_txs, self.chain_pk, hdr, []))
        else:
            hdr_last = await self.adapter.get_block_header(to)
            if hdr_last:
                ts_map[to] = datetime.fromtimestamp(hdr_last.timestamp,
                                                    tz=timezone.utc).replace(tzinfo=None)
        # 2. Transfer logs (event-driven token discovery — no per-contract scans)
        try:
            transfer_logs = await self.adapter.get_logs(frm, to, topics=[TRANSFER_TOPIC])
        except ConnectionError:
            # narrow the chunk once on "range too large"-style errors
            mid = (frm + to) // 2
            if mid > frm:
                await self._process_range(frm, mid)
                await self._process_range(mid + 1, to)
                return
            raise
        # 3. DEX pair-creation events
        pair_events = await self.dex.fetch_events(frm, to)
        if pair_events:
            await asyncio.to_thread(self._register_pairs, self.chain_pk, pair_events, ts_map)
        # 4. transfers pipeline (discovery, pricing, scoring, storage)
        await self.transfers.process_logs(transfer_logs, ts_map)
        log.info("[%s] blocks %d-%d | %d transfer logs | %d pair events",
                 self.chain_key, frm, to, len(transfer_logs), len(pair_events))

    def stop(self) -> None:
        self.running = False
