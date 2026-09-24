"""Phase 3 — DEX Intelligence Engine: the orchestrator.

Pipeline per processed block range (single shared scanner, no second loop):

    LOGS -> GROUP BY TX -> DECODE SWAP/MINT/BURN EVENTS
         -> ATTRIBUTE USER TRADES (router-internal hops excluded)
         -> PRICE (USD) -> CLASSIFY BUY_LIKE / SELL_LIKE / SWAP_OTHER
         -> MARKET IMPACT (price impact %, liquidity impact ratio)
         -> FLOW AGGREGATION (incremental buckets)
         -> WALLET TRADING PROFILE (incremental)
         -> RANKED MARKET IMPACT EVENTS -> DB

Every stage is defensive: one bad log/tx never stops the scanner, and every
numeric field stays NULL when it cannot be computed for real.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.blockchain.base import BaseChainAdapter, LogEntry
from app.database.models import (
    DexInteraction, DexSwap, LiquidityEvent, MarketImpactEvent, Token,
    WalletTradeSnapshot,
)
from app.dex.assets import classify_swap_sides, is_wrap_unwrap
from app.dex.decoder import (
    decode_v2_liquidity, decode_v2_swap, decode_v3_swap, topic_addr, u256,
)
from app.dex.pools import PoolRegistry
from app.flow.engine import FlowEngine
from app.flow.impact import (
    estimate_price_impact_pct, liquidity_event_valuation, liquidity_impact,
)
from app.wallets.intelligence import compute_whale_score
from config import (
    TRANSFER_TOPIC, UNI_V3_SWAP, V2_BURN_TOPIC, V2_MINT_TOPIC, V2_SWAP_TOPIC,
    ChainConfig, dex_settings as DS, wallet_settings as WS,
)

log = logging.getLogger("dex.engine")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class DexIntelligenceEngine:
    def __init__(self, adapter: BaseChainAdapter, chain_key: str,
                 session_factory, cfg: ChainConfig, prices,
                 pools: PoolRegistry, discovery):
        self.adapter = adapter
        self.chain_key = chain_key
        self.session_factory = session_factory
        self.cfg = cfg
        self.prices = prices                    # Phase 1 PriceService
        self.pools = pools
        self.discovery = discovery
        self.flow = FlowEngine()
        self.swap_count = 0                     # observability (API status)
        self._swap_topics = {V2_SWAP_TOPIC, UNI_V3_SWAP}
        self._liq_topics = {V2_MINT_TOPIC, V2_BURN_TOPIC}
        self._watched_pools: set[str] = set()   # RAM-bounded
        # pool_address -> (dex_name, kind, (token0, token1)) resolved lazily
        self._pool_meta: dict[str, tuple[str, str, tuple[str, str]]] = {}

    # ------------------------------------------------------------------
    async def _chain_pk(self) -> int:
        return await self.pools._ensure_chain_pk()

    async def process_logs(self, logs: list[LogEntry],
                           block_ts: dict[int, datetime]) -> None:
        """Main entry: called by BlockCollector with ALL logs of the range."""
        relevant = [e for e in logs
                    if e.topics and e.topics[0].lower() in
                    (self._swap_topics | self._liq_topics)]
        if not relevant:
            return
        chain_pk = await self._chain_pk()
        transfers_by_tx: dict[str, list[LogEntry]] = {}
        for e in logs:
            if e.topics and e.topics[0].lower() == TRANSFER_TOPIC:
                transfers_by_tx.setdefault(e.transaction_hash, []).append(e)

        # group events per tx so internal hops are visible to attribution
        by_tx: dict[str, list[LogEntry]] = {}
        for e in relevant:
            by_tx.setdefault(e.transaction_hash, []).append(e)

        rows_swaps: list[dict] = []
        rows_liq: list[dict] = []
        impacts: list[dict] = []
        profiles: dict[str, dict] = {}          # wallet -> trade delta
        flow_ops: list[tuple] = []              # deferred flow deltas
        liq_flow_ops: list[tuple] = []

        for tx_hash, events in by_tx.items():
            events.sort(key=lambda x: x.log_index)
            try:
                await self._process_tx(chain_pk, tx_hash, events,
                                       transfers_by_tx.get(tx_hash, []),
                                       block_ts, rows_swaps, rows_liq,
                                       impacts, profiles, flow_ops,
                                       liq_flow_ops)
            except Exception as e:
                log.warning("dex tx %s skipped: %s", tx_hash[:12], str(e)[:160])
                continue

        if rows_swaps or rows_liq or impacts or profiles:
            await asyncio.to_thread(self._write_all, chain_pk, rows_swaps,
                                    rows_liq, impacts, profiles, flow_ops,
                                    liq_flow_ops)
        self.swap_count += len(rows_swaps)

    # ------------------------------------------------------------------
    async def _pool_tokens(self, pool_addr: str, dex_hint: str,
                           kind: str) -> tuple[str, str] | None:
        """Resolve pool token pair from DB registry or eth_call (cached)."""
        meta = self._pool_meta.get(pool_addr)
        if meta:
            return meta[2]
        found = None
        with self.session_factory() as s:
            from app.database.models import DexPool
            row = (s.query(DexPool)
                   .filter(DexPool.pool_address == pool_addr).first())
            if row and row.token0 and row.token1:
                found = (row.token0, row.token1)
        if found is None:
            t0 = await self.adapter.eth_call(pool_addr, "0x0dfe1681")
            t1 = await self.adapter.eth_call(pool_addr, "0xd21220a7")
            if t0 and t1 and len(t0) >= 64 and len(t1) >= 64:
                found = ("0x" + t0[-40:].lower(), "0x" + t1[-40:].lower())
        if found:
            if len(self._pool_meta) > 5000:
                self._pool_meta.clear()
            self._pool_meta[pool_addr] = (dex_hint, kind, found)
        return found

    def _dex_for_pool(self, pool_addr: str, factory_dex: str | None) -> str:
        if factory_dex:
            return factory_dex
        meta = self._pool_meta.get(pool_addr)
        if meta:
            return meta[0]
        with self.session_factory() as s:
            from app.database.models import DexPool
            row = (s.query(DexPool).filter(DexPool.pool_address == pool_addr)
                   .first())
            return row.dex_name if row else "unknown_dex"

    async def _process_tx(self, chain_pk, tx_hash, events, transfer_logs,
                          block_ts, rows_swaps, rows_liq, impacts, profiles,
                          flow_ops, liq_flow_ops) -> None:
        routers = self.pools.router_addresses()
        # best-effort router detection via receipt input selector (never required)
        receipt = await self.adapter.get_tx_receipt(tx_hash)
        input_is_router = False
        tx_from = None
        if receipt:
            sel = (receipt.get("input") or "")[:10].lower()
            input_is_router = sel in DS.exact_in_selectors
            tx_from = (receipt.get("from") or "").lower() or None

        swap_events = [e for e in events
                       if e.topics[0].lower() in self._swap_topics]
        liq_events = [e for e in events
                      if e.topics[0].lower() in self._liq_topics]

        # ---------- swaps ----------
        for e in swap_events:
            topic = e.topics[0].lower()
            pool = e.address.lower()
            kind = "v3" if topic == UNI_V3_SWAP else "v2"
            tokens = await self._pool_tokens(pool, None, kind)
            dex_name = self._dex_for_pool(pool, None)
            if kind == "v2":
                sw = decode_v2_swap(e, tokens or ("", ""), dex_name)
            else:
                sw = decode_v3_swap(e, tokens or ("", ""), dex_name)
            if sw is None:
                continue
            ts = block_ts.get(e.block_number) or utcnow()

            # ---- user attribution: ONE trade per user per tx per pool path.
            # If several Swap events in the same tx share the same attributed
            # wallet (multi-hop routing), keep only the outer legs summed —
            # here: first event keeps full attribution, later duplicates are
            # stored with is_user_trade=False (hop, not a separate trade).
            dup_wallet = any(r["tx_hash"] == tx_hash and
                             r["wallet_address"] == sw.wallet_address
                             and r["is_user_trade"] for r in rows_swaps)
            sender = topic_addr(e.topics[1]) if len(e.topics) > 1 else ""
            via_router = (sender in routers) or \
                         (sw.wallet_address in routers) or \
                         (input_is_router and sw.wallet_address == tx_from)
            is_user = (not dup_wallet) and sw.is_user_trade

            # wrap/unwrap pairs are NOT market trades
            classification = "UNKNOWN_SWAP"
            if sw.token_in and sw.token_out and not is_wrap_unwrap(
                    sw.token_in, sw.token_out, self.chain_key):
                sym_in = self._symbol(sw.token_in)
                sym_out = self._symbol(sw.token_out)
                classification = classify_swap_sides(self.chain_key,
                                                     sw.token_in, sym_in,
                                                     sw.token_out, sym_out)
            elif sw.token_in or sw.token_out:
                # single-leg known side against quote => still classifiable
                tok = sw.token_out or sw.token_in
                if tok and self._is_quote(tok):
                    classification = "UNKNOWN_SWAP"

            usd, norm_in, norm_out = await self._value_swap(sw)

            # ---- market impact metrics (NULL when unknown) ----
            pool_liq = self._pool_liquidity(pool)
            liq_ratio = liquidity_impact(usd, pool_liq)
            price_impact = None
            reserves = await self._pool_reserves(pool)
            if reserves and sw.amount_in_raw and sw.token_in:
                dec_in = self._decimals(sw.token_in)
                r_in, r_out = self._orient_reserves(reserves, sw, pool)
                k_in = (sw.amount_in_raw or 0) / (10 ** dec_in[0])
                price_impact = estimate_price_impact_pct(r_in, r_out, k_in)

            whale_flag = self._wallet_is_whale(sw.wallet_address)
            ws = compute_whale_score(usd, self.chain_key, None, pool_liq, 0, 0) \
                if usd else None

            rows_swaps.append(dict(
                chain_pk=chain_pk, tx_hash=tx_hash,
                log_index=e.log_index, block_number=e.block_number,
                wallet_address=sw.wallet_address, dex_name=dex_name,
                pool_address=pool, token_in=sw.token_in,
                token_out=sw.token_out, amount_in_raw=sw.amount_in_raw,
                amount_out_raw=sw.amount_out_raw,
                amount_in=norm_in, amount_out=norm_out, usd_value=usd,
                classification=classification,
                price_impact_pct=price_impact, liquidity_impact=liq_ratio,
                is_user_trade=is_user, timestamp=ts))

            if not is_user:
                continue                      # hops never feed flows/events

            traded_token = self._traded_token(sw, classification)
            if traded_token and usd:
                flow_ops.append((chain_pk,
                                 traded_token, classification, usd, ts,
                                 bool(whale_flag or (ws and ws.is_whale_event))))
                prof = profiles.setdefault(sw.wallet_address, {
                    "buy": 0, "sell": 0, "other": 0, "buy_usd": 0.0,
                    "sell_usd": 0.0, "largest_buy": None, "largest_sell": None,
                    "dex_counts": {}, "token_counts": {}, "last_ts": ts})
                if classification == "BUY_LIKE":
                    prof["buy"] += 1
                    prof["buy_usd"] += usd
                    prof["largest_buy"] = max(prof["largest_buy"] or 0, usd)
                elif classification == "SELL_LIKE":
                    prof["sell"] += 1
                    prof["sell_usd"] += usd
                    prof["largest_sell"] = max(prof["largest_sell"] or 0, usd)
                else:
                    prof["other"] += 1
                prof["dex_counts"][dex_name] = prof["dex_counts"].get(dex_name, 0) + 1
                if traded_token:
                    prof["token_counts"][traded_token] = \
                        prof["token_counts"].get(traded_token, 0) + 1

            # ---- ranked impact events ----
            ev_type = None
            if usd is not None and classification in ("BUY_LIKE", "SELL_LIKE"):
                big = usd >= DS.large_trade_min_usd
                whale = usd >= DS.whale_trade_min_usd
                high_impact = liq_ratio is not None and liq_ratio >= DS.high_impact_liquidity_ratio
                if whale:
                    ev_type = "WHALE_BUY" if classification == "BUY_LIKE" else "WHALE_SELL"
                elif big:
                    ev_type = "LARGE_BUY" if classification == "BUY_LIKE" else "LARGE_SELL"
                elif high_impact:
                    ev_type = "HIGH_IMPACT_SWAP"
                if ev_type:
                    score = min(100.0, (ws.whale_score if ws else 0.0) * 0.6
                                + min(40.0, (liq_ratio or 0) * 800))
                    impacts.append(dict(
                        chain_pk=chain_pk, event_type=ev_type,
                        token_address=traded_token,
                        token_symbol=self._symbol(traded_token) if traded_token else None,
                        wallet_address=sw.wallet_address, pool_address=pool,
                        dex_name=dex_name, tx_hash=tx_hash, usd_value=usd,
                        liquidity_usd=pool_liq, liquidity_impact=liq_ratio,
                        price_impact_pct=price_impact,
                        whale_score=ws.whale_score if ws else None,
                        impact_score=round(score, 2),
                        explanation=(f"Observed {'buy' if classification == 'BUY_LIKE' else 'sell'}"
                                     f"-like swap ${usd:,.0f} on {dex_name} pool "
                                     f"{pool[:10]}{'…' if len(pool) > 10 else ''}"
                                     + (f"; {liq_ratio * 100:.1f}% of pool liquidity"
                                        if liq_ratio else "")
                                     + ("; routed via known aggregator"
                                        if via_router else "")
                                     )[:500],
                        timestamp=ts))

        # ---------- liquidity Mint/Burn ----------
        for e in liq_events:
            le = decode_v2_liquidity(e, self._dex_for_pool(e.address.lower(), None))
            if le is None:
                continue
            pool = e.address.lower()
            ts = block_ts.get(e.block_number) or utcnow()
            tokens = await self._pool_tokens(pool, None, "v2")
            usd = pct = None
            before = self._pool_liquidity(pool)
            if tokens:
                d0, d1 = self._decimals(tokens[0]), self._decimals(tokens[1])
                p0, _ = await self.prices.get_price(tokens[0])
                p1, _ = await self.prices.get_price(tokens[1])
                n0 = (le.amount0_raw or 0) / (10 ** d0[0]) if le.amount0_raw else None
                n1 = (le.amount1_raw or 0) / (10 ** d1[0]) if le.amount1_raw else None
                usd, pct = liquidity_event_valuation(n0, p0, n1, p1, before)
            signed = usd if le.event_type == "LIQUIDITY_ADD" else -(usd or 0)
            after = (before + signed) if (before is not None and signed) else None
            rows_liq.append(dict(
                chain_pk=chain_pk, tx_hash=tx_hash,
                log_index=e.log_index, pool_address=pool,
                dex_name=le.dex_name, wallet_address=le.wallet_address,
                event_type=le.event_type, amount0_raw=le.amount0_raw,
                amount1_raw=le.amount1_raw, usd_value=usd,
                liquidity_before_usd=before, liquidity_after_usd=after,
                liquidity_change_usd=signed, liquidity_change_pct=pct,
                timestamp=ts))
            if tokens and usd:
                for taddr in tokens:
                    liq_flow_ops.append((chain_pk, taddr, signed, ts))
                shift_alert = (pct is not None and
                               pct >= DS.liquidity_shift_pct_alert)
                big_enough = usd >= DS.liquidity_event_min_usd
                if big_enough or shift_alert:
                    score = min(100.0, (pct or 0) * 2 +
                                min(50.0, (usd or 0) / max(before or 1, 1) * 100))
                    impacts.append(dict(
                        chain_pk=chain_pk, event_type=le.event_type,
                        token_address=tokens[1] if tokens else None,
                        token_symbol=self._symbol(tokens[1]) if tokens else None,
                        wallet_address=le.wallet_address, pool_address=pool,
                        dex_name=le.dex_name, tx_hash=tx_hash, usd_value=usd,
                        liquidity_usd=before, liquidity_impact=None,
                        price_impact_pct=None, whale_score=None,
                        impact_score=round(score, 2),
                        explanation=(f"Observed ${usd:,.0f} liquidity "
                                     f"{'addition' if le.event_type == 'LIQUIDITY_ADD' else 'removal'}"
                                     f" ({pct:.1f}% of pool) — removal is a risk signal,"
                                     " not an automatic rug claim."
                                     if pct is not None else
                                     f"Observed liquidity change ${usd:,.0f}")[:500],
                        timestamp=ts))

    # ---------------- small sync helpers (DB-backed caches) ---------------
    _sym_cache: dict[str, str | None] = {}
    _dec_cache: dict[str, int] = {}
    _liq_cache: dict[str, tuple[float | None, float]] = {}
    _whale_cache: dict[str, tuple[bool, float]] = {}

    def _symbol(self, addr: str | None) -> str | None:
        if not addr:
            return None
        return self._sym_cache.get(addr.lower())

    def _decimals(self, addr: str) -> tuple[int]:
        return (self._dec_cache.get(addr.lower(), 18),)

    def _pool_liquidity(self, pool: str) -> float | None:
        hit = self._liq_cache.get(pool)
        if hit and utcnow().timestamp() - hit[1] < 120:
            return hit[0]
        val = None
        with self.session_factory() as s:
            from app.database.models import DexPool
            row = s.query(DexPool).filter_by(pool_address=pool).first()
            if row and row.liquidity_usd is not None:
                val = float(row.liquidity_usd)
        self._liq_cache[pool] = (val, utcnow().timestamp())
        if len(self._liq_cache) > 3000:
            self._liq_cache.clear()
        return val

    _reserve_cache: dict[str, tuple[tuple[int, int] | None, float]] = {}

    async def _pool_reserves(self, pool: str) -> tuple[int, int] | None:
        hit = DexIntelligenceEngine._reserve_cache.get(pool)
        if hit and utcnow().timestamp() - hit[1] < 90:
            return hit[0]
        out = None
        try:
            res = await self.adapter.eth_call(pool, "0x0902f1ac")
            if res and len(res) >= 194:
                r0 = u256(res, 0)
                r1 = u256(res, 1)
                if r0 and r1:
                    out = (r0, r1)
        except Exception:
            out = None
        if len(DexIntelligenceEngine._reserve_cache) > 3000:
            DexIntelligenceEngine._reserve_cache.clear()
        DexIntelligenceEngine._reserve_cache[pool] = (out, utcnow().timestamp())
        return out

    def _wallet_is_whale(self, addr: str) -> bool:
        hit = self._whale_cache.get(addr)
        if hit and utcnow().timestamp() - hit[1] < 300:
            return hit[0]
        out = False
        with self.session_factory() as s:
            from app.database.models import Wallet
            w = s.query(Wallet).filter_by(address=addr).first()
            out = bool(w and float(w.whale_score or 0) >= WS.whale_min_score)
        self._whale_cache[addr] = (out, utcnow().timestamp())
        return out

    def _traded_token(self, sw, classification: str) -> str | None:
        """The non-quote leg — the asset actually bought/sold."""
        if classification == "BUY_LIKE":
            return sw.token_out
        if classification == "SELL_LIKE":
            return sw.token_in
        return sw.token_out or sw.token_in

    def _orient_reserves(self, reserves: tuple[int, int], sw, pool: str
                         ) -> tuple[float | None, float | None]:
        meta = self._pool_meta.get(pool.lower())
        if not meta:
            return None, None
        t0, t1 = meta[2]
        r0n = reserves[0] / (10 ** self._dec_cache.get(t0, 18))
        r1n = reserves[1] / (10 ** self._dec_cache.get(t1, 18))
        if sw.token_in == t0:
            return r0n, r1n
        if sw.token_in == t1:
            return r1n, r0n
        return None, None

    def _is_quote(self, addr: str) -> bool:
        from app.dex.assets import is_quote_asset
        return is_quote_asset(self.chain_key, addr, self._symbol(addr))

    async def _value_swap(self, sw):
        """USD value of the trade + normalized amounts. Uses the priced leg;
        falls back to quoting the other side. Returns (None, ...) when
        nothing can be priced — never a fabricated value."""
        norm_in = norm_out = None
        if sw.token_in and sw.amount_in_raw is not None:
            d = self._dec_cache.get(sw.token_in.lower(), 18)
            norm_in = Decimal(sw.amount_in_raw) / (Decimal(10) ** d)
        if sw.token_out and sw.amount_out_raw is not None:
            d = self._dec_cache.get(sw.token_out.lower(), 18)
            norm_out = Decimal(sw.amount_out_raw) / (Decimal(10) ** d)
        usd = None
        for tok, norm in ((sw.token_in, norm_in), (sw.token_out, norm_out)):
            if tok and norm is not None:
                price, _src = await self.prices.get_price(tok.lower())
                if price:
                    usd = float(norm * Decimal(str(price)))
                    break
        return usd, norm_in, norm_out

    async def refresh_metadata_caches(self) -> None:
        """Reload symbol/decimals maps from DB (bounded)."""
        def _load():
            syms, decs = {}, {}
            with self.session_factory() as s:
                for t in s.query(Token.address, Token.symbol,
                                 Token.decimals).limit(20000).all():
                    if t.symbol:
                        syms[t.address] = t.symbol
                    decs[t.address] = t.decimals if t.decimals is not None else 18
            return syms, decs
        try:
            syms, decs = await asyncio.to_thread(_load)
            DexIntelligenceEngine._sym_cache = syms
            DexIntelligenceEngine._dec_cache = decs
        except Exception as e:
            log.debug("metadata cache refresh failed: %s", e)

    # ---------------- persistence (batched, deduped) ----------------------
    def _write_all(self, chain_pk, rows_swaps, rows_liq, impacts, profiles,
                   flow_ops, liq_flow_ops) -> None:
        with self.session_factory() as s:
            dialect = s.bind.dialect.name if s.bind else "sqlite"
            ins = pg_insert if dialect == "postgresql" else sqlite_insert
            for chunk_start in range(0, len(rows_swaps), 200):
                chunk = rows_swaps[chunk_start:chunk_start + 200]
                stmt = ins(DexSwap).values(chunk).on_conflict_do_nothing(
                    index_elements=["chain_pk", "tx_hash", "log_index"])
                s.execute(stmt)
            for chunk_start in range(0, len(rows_liq), 200):
                chunk = rows_liq[chunk_start:chunk_start + 200]
                stmt = ins(LiquidityEvent).values(chunk).on_conflict_do_nothing(
                    index_elements=["chain_pk", "tx_hash", "log_index"])
                s.execute(stmt)
            # flow aggregation (incremental deltas only)
            for _cp, tok, cls, usd, ts, is_whale in flow_ops:
                self.flow.record_swap(s, _cp, tok, cls, usd, ts, is_whale)
            for _cp, tok, signed, ts in liq_flow_ops:
                self.flow.record_liquidity_change(s, _cp, tok, signed, ts)
            # wallet trading profiles
            for wallet, prof in profiles.items():
                self._update_wallet_profile(s, ins, chain_pk, wallet, prof)
            # impact events (dedupe on tx+type+wallet)
            for ev in impacts[:500]:
                stmt = ins(MarketImpactEvent).values(**ev).on_conflict_do_nothing(
                    index_elements=["tx_hash", "event_type", "wallet_address"])
                s.execute(stmt)
            s.commit()

    def _update_wallet_profile(self, s: Session, ins, chain_pk: int,
                               wallet: str, p: dict) -> None:
        vals = dict(chain_pk=chain_pk, wallet_address=wallet,
                    buy_count=p["buy"], sell_count=p["sell"],
                    other_count=p["other"],
                    buy_volume_usd=p["buy_usd"], sell_volume_usd=p["sell_usd"],
                    net_trading_flow_usd=p["buy_usd"] - p["sell_usd"],
                    largest_buy_usd=p["largest_buy"],
                    largest_sell_usd=p["largest_sell"],
                    favorite_dex=max(p["dex_counts"], key=p["dex_counts"].get)
                    if p["dex_counts"] else None,
                    favorite_token=max(p["token_counts"], key=p["token_counts"].get)
                    if p["token_counts"] else None,
                    last_trade_at=p["last_ts"], updated_at=utcnow())
        upd = {
            "buy_count": WalletTradeSnapshot.buy_count + p["buy"],
            "sell_count": WalletTradeSnapshot.sell_count + p["sell"],
            "other_count": WalletTradeSnapshot.other_count + p["other"],
            "buy_volume_usd": WalletTradeSnapshot.buy_volume_usd + p["buy_usd"],
            "sell_volume_usd": WalletTradeSnapshot.sell_volume_usd + p["sell_usd"],
            "net_trading_flow_usd": (WalletTradeSnapshot.net_trading_flow_usd
                                     + (p["buy_usd"] - p["sell_usd"])),
            "last_trade_at": p["last_ts"], "updated_at": utcnow()}
        stmt = ins(WalletTradeSnapshot).values(**vals).on_conflict_do_update(
            index_elements=["chain_pk", "wallet_address"], set_=upd)
        s.execute(stmt)
        # update largest_* with max semantics
        from sqlalchemy import case
        s.query(WalletTradeSnapshot).filter_by(
            chain_pk=chain_pk, wallet_address=wallet).update(
            {WalletTradeSnapshot.largest_buy_usd:
             case((WalletTradeSnapshot.largest_buy_usd < p["largest_buy"],
                   p["largest_buy"]), else_=WalletTradeSnapshot.largest_buy_usd)
             if p["largest_buy"] is not None else WalletTradeSnapshot.largest_buy_usd,
             WalletTradeSnapshot.largest_sell_usd:
             case((WalletTradeSnapshot.largest_sell_usd < p["largest_sell"],
                   p["largest_sell"]), else_=WalletTradeSnapshot.largest_sell_usd)
             if p["largest_sell"] is not None else WalletTradeSnapshot.largest_sell_usd},
            synchronize_session=False)
        # record interaction (Phase 2 table stays coherent)
        for dex_name in p["dex_counts"]:
            s.execute(ins(DexInteraction).values(
                chain_pk=chain_pk, wallet_address=wallet,
                dex_name=dex_name, pool_address=None, token_address=None,
                interaction_type="SWAP",
                tx_hash=f"profile:{wallet}:{int(p['last_ts'].timestamp())}",
                usd_value=p["buy_usd"] + p["sell_usd"],
                timestamp=p["last_ts"]).on_conflict_do_nothing(
                index_elements=["chain_pk", "tx_hash", "wallet_address",
                                "dex_name"]))
