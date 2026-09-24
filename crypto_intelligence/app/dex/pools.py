"""Phase 3 — pool registry & discovery.

Two discovery sources (event/activity driven, no per-contract scanning):
  1. factory PairCreated events (Phase 1 DexCollector) -> register_pair()
  2. observation-derived pools: a contract that emits V2 Swap/Mint/Burn
     logs and holds two ERC-20 balances is registered as a pool.

Reserves/liquidity are refreshed with cheap eth_calls (getReserves/token0/
token1), rate-limited per pool, and priced via the existing PriceService
(stable / wrapped-native quote legs). All DB writes are batched upserts.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from app.blockchain.base import BaseChainAdapter
from app.blockchain.evm import SEL_GET_RESERVES, SEL_TOKEN0, SEL_TOKEN1
from app.database.models import Chain, DexPool, DexProtocol, TokenPair
from config import ChainConfig, dex_settings

log = logging.getLogger("dex.pools")


def _dec(res: str | None, offset_words: int = 0) -> int | None:
    if not res or res == "0x":
        return None
    body = res[2:]
    try:
        return int(body[offset_words * 64:(offset_words + 1) * 64], 16)
    except ValueError:
        return None


class PoolRegistry:
    """Tracks configured protocols + discovered pools for one chain."""

    RESERVE_REFRESH_SECONDS = 180          # per-pool RPC throttle

    def __init__(self, adapter: BaseChainAdapter, chain_key: str,
                 session_factory, cfg: ChainConfig, prices=None):
        self.adapter = adapter
        self.chain_key = chain_key
        self.session_factory = session_factory
        self.cfg = cfg
        self.prices = prices               # Phase 1 PriceService (optional)
        self._next_refresh: dict[str, float] = {}
        self._pool_tokens: set[str] = set()    # RAM-bounded token cache
        self._refreshed_once: set[str] = set()
        self.chain_pk: int | None = None

    async def _ensure_chain_pk(self) -> int:
        if self.chain_pk is None:
            def _q():
                with self.session_factory() as s:
                    return s.query(Chain.id).filter_by(key=self.chain_key).scalar()
            self.chain_pk = await asyncio.to_thread(_q)
        return self.chain_pk

    # ---------------- protocol seeding (config-driven, idempotent) --------
    def seed_protocols(self, chain_pk: int) -> None:
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        with self.session_factory() as s:
            dialect = s.bind.dialect.name if s.bind else "sqlite"
            ins = pg_insert if dialect == "postgresql" else sqlite_insert
            for d in self.cfg.dexes:
                stmt = ins(DexProtocol).values(
                    chain_pk=chain_pk, name=d.name, kind=d.kind,
                    factory_address=(d.factory_address or "").lower() or None,
                    routers=",".join(r.lower() for r in d.routers) or None,
                ).on_conflict_do_nothing(index_elements=["chain_pk", "name"])
                s.execute(stmt)
            s.commit()

    def router_addresses(self) -> set[str]:
        out: set[str] = set()
        for d in self.cfg.dexes:
            out.update(r.lower() for r in d.routers)
        return out

    # ---------------- pair registration -----------------------------------
    def register_pair(self, chain_pk: int, pool_address: str, dex_name: str,
                      token0: str, token1: str, source: str = "pair_created",
                      ts: datetime | None = None) -> None:
        """Shared by factory-PairCreated discovery AND observation-derived
        pools. Writes both Phase 1 TokenPair and Phase 3 DexPool rows."""
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        ts = ts or datetime.now(timezone.utc).replace(tzinfo=None)
        pool_address, token0, token1 = (a.lower() for a in (pool_address, token0, token1))
        with self.session_factory() as s:
            dialect = s.bind.dialect.name if s.bind else "sqlite"
            ins = pg_insert if dialect == "postgresql" else sqlite_insert
            s.execute(ins(TokenPair).values(
                chain_pk=chain_pk, pair_address=pool_address, dex_name=dex_name,
                token0=token0, token1=token1, first_seen=ts, last_seen=ts,
            ).on_conflict_do_nothing(index_elements=["chain_pk", "pair_address"]))
            s.execute(ins(DexPool).values(
                chain_pk=chain_pk, pool_address=pool_address, dex_name=dex_name,
                token0=token0, token1=token1, first_seen=ts, last_seen=ts,
                source=source,
            ).on_conflict_do_update(
                index_elements=["chain_pk", "pool_address"],
                set_={"last_seen": ts}))
            s.commit()
        self._pool_tokens.add(token0)
        self._pool_tokens.add(token1)

    def known_pool_tokens(self) -> set[str]:
        return set(self._pool_tokens)

    # ---------------- reserve refresh (async, throttled) ------------------
    async def maybe_refresh(self, pool_address: str) -> None:
        pool_address = pool_address.lower()
        now = time.time()
        if now < self._next_refresh.get(pool_address, 0):
            return
        self._next_refresh[pool_address] = now + self.RESERVE_REFRESH_SECONDS
        if len(self._next_refresh) > 5000:          # RAM bound
            self._next_refresh.clear()
        try:
            r0_res = await self.adapter.eth_call(pool_address, SEL_GET_RESERVES)
            if not r0_res or len(r0_res) < 130:
                return
            reserve0 = _dec(r0_res, 0)
            reserve1 = _dec(r0_res, 1)
            t0 = await self.adapter.eth_call(pool_address, SEL_TOKEN0)
            t1 = await self.adapter.eth_call(pool_address, SEL_TOKEN1)
            addr0 = ("0x" + t0[-40:].lower()) if t0 and len(t0) >= 64 else None
            addr1 = ("0x" + t1[-40:].lower()) if t1 and len(t1) >= 64 else None
            liq = await self._liquidity_usd(addr0, addr1, reserve0, reserve1)
            def _write():
                chain_pk = self.chain_pk
                from sqlalchemy.dialects.postgresql import insert as pg_insert
                from sqlalchemy.dialects.sqlite import insert as sqlite_insert
                with self.session_factory() as s:
                    dialect = s.bind.dialect.name if s.bind else "sqlite"
                    ins = pg_insert if dialect == "postgresql" else sqlite_insert
                    vals = dict(chain_pk=chain_pk, pool_address=pool_address,
                                dex_name="observed",
                                token0=addr0 or "", token1=addr1 or "",
                                reserve0=reserve0, reserve1=reserve1,
                                liquidity_usd=liq,
                                last_seen=datetime.now(timezone.utc).replace(tzinfo=None))
                    upd = {k: v for k, v in vals.items()
                           if k not in ("chain_pk", "pool_address", "first_seen")}
                    s.execute(ins(DexPool).values(**vals).on_conflict_do_update(
                        index_elements=["chain_pk", "pool_address"], set_=upd))
                    if liq is not None:
                        from app.database.models import LiquiditySnapshot
                        s.add(LiquiditySnapshot(
                            chain_pk=chain_pk, pool_address=pool_address,
                            token0=addr0 or "", token1=addr1 or "",
                            liquidity_usd=liq, reserve0=reserve0,
                            reserve1=reserve1))
                    s.commit()
            await asyncio.to_thread(_write)
            if addr0:
                self._pool_tokens.update((addr0, addr1))
        except Exception as e:
            log.debug("pool refresh failed %s: %s", pool_address[:10], str(e)[:120])

    async def _liquidity_usd(self, t0: str | None, t1: str | None,
                             r0: int | None, r1: int | None) -> float | None:
        """Value both sides using the price service; conservative: if only
        one side is priced, double it (constant-product pools hold ~equal
        value on both sides)."""
        if not (t0 and t1 and r0 is not None and r1 is not None):
            return None
        v0 = await self._side_value(t0, r0)
        v1 = await self._side_value(t1, r1)
        if v0 is None and v1 is None:
            return None
        if v0 is None:
            return round(v1 * 2, 2)
        if v1 is None:
            return round(v0 * 2, 2)
        return round(v0 + v1, 2)

    async def _side_value(self, token_addr: str, raw_reserve: int) -> float | None:
        if self.prices is None:
            return None
        try:
            price, _src = await self.prices.get_price(token_addr)
            if price is None:
                return None
            dec = None
            def _d():
                with self.session_factory() as s:
                    from app.database.models import Token
                    t = s.query(Token).filter_by(chain_pk=self.chain_pk,
                                                 address=token_addr).first()
                    return t.decimals if t else None
            dec = await asyncio.to_thread(_d)
            dec = dec if dec is not None else 18
            return float(raw_reserve) / (10 ** dec) * float(price)
        except Exception:
            return None

    # ---------------- helpers for the engine ------------------------------
    def get_pools_for_tokens(self, token_addrs: list[str]) -> list[DexPool]:
        """Synchronous lookup of tracked pools containing any of the tokens."""
        addrs = {a.lower() for a in token_addrs}
        if not addrs:
            return []
        with self.session_factory() as s:
            rows = (s.query(DexPool)
                    .filter(DexPool.chain_pk == self.chain_pk,
                            (DexPool.token0.in_(addrs)) | (DexPool.token1.in_(addrs)))
                    .limit(200).all())
        return rows

    def best_pool_liquidity(self, token_addr: str) -> tuple[str | None, float | None]:
        """Highest-liquidity tracked pool for a token (for impact math)."""
        token_addr = token_addr.lower()
        best_liq, best_pool = None, None
        for p in self.get_pools_for_tokens([token_addr]):
            liq = float(p.liquidity_usd) if p.liquidity_usd is not None else None
            if liq is not None and (best_liq is None or liq > best_liq):
                best_liq, best_pool = liq, p.pool_address
        return best_pool, best_liq
