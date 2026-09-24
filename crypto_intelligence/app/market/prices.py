"""Extensible price provider architecture.

PriceProvider is the interface; Phase 1 ships:
 - StablecoinProvider   (hard $1 peg for known stables)
 - NativePoolProvider   (DEX pool reserves -> token/native price * native USD)
 - CoingeckoProvider    (optional public market API, cached, rate-limit safe)

The scanner is never dependent on a single source: providers are tried in
order and failures just mean "no price" (USD stays NULL, transfer kept).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from decimal import Decimal

import httpx


from app.blockchain.base import BaseChainAdapter
from app.database.models import Chain, Token, TokenPair
from config import settings

log = logging.getLogger("prices")


def _ssl_verify_enabled() -> bool:
    """Allow opting out of TLS verification on networks with intercepting
    proxies / corporate root CAs (SSL: CERTIFICATE_VERIFY_FAILED)."""
    return os.getenv("SSL_VERIFY", "true").strip().lower() not in ("0", "false", "no", "off")


def _new_client(timeout: float) -> httpx.AsyncClient:
    """httpx client with resilient TLS handling.

    Verification order (each fallback only on cert-store failures):
      1. normal verification (certifi / default context)
      2. OS certificate store via ssl.create_default_context() — certifi
         misses many intermediate setups on Windows
      3. if SSL_VERIFY=false, unverified (intercepting-proxy networks)
    """
    import ssl
    if not _ssl_verify_enabled():
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        log.warning("TLS certificate verification DISABLED (SSL_VERIFY=false)")
        return httpx.AsyncClient(timeout=timeout, verify=ctx)
    try:
        ctx = ssl.create_default_context()   # OS trust store (Windows)
    except Exception:
        ctx = True                           # fall back to httpx default (certifi)
    return httpx.AsyncClient(timeout=timeout, verify=ctx)

STABLES = {
    "ethereum": {"0xdac17f958d2ee523a2206206994597c13d831ec7": 1.0,   # USDT
                 "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": 1.0,   # USDC
                 "0x6b175474e89094c44da98b954eedeac495271d0f": 1.0},  # DAI
    "bsc": {"0x55d398326f99059ff775485246999027b3197955": 1.0,        # USDT-BSC
            "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d": 1.0,        # USDC-BSC
            "0xe9e7cea3dedca5984780bafc599bd69add087d56": 1.0},       # BUSD
}
# wrapped native assets per chain
WRAPPED_NATIVE = {"ethereum": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
                  "bsc": "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"}


class PriceProvider:
    """Base class - implement `get_price` in future providers."""
    name = "base"

    async def get_price(self, chain_key: str, token_address: str) -> float | None:
        return None


class StablecoinProvider(PriceProvider):
    name = "stablecoin"

    async def get_price(self, chain_key: str, token_address: str) -> float | None:
        price = STABLES.get(chain_key, {}).get(token_address.lower())
        if price is None and settings.mock_mode:
            # MOCK dev mode only: treat the synthetic MUSD token as a $1 stable
            if token_address.lower() == "0x" + "bb" * 20:
                return 1.0
        return price


class NativeAssetPrice:
    """Tracks ETH/BNB USD price via CoinGecko (cached, refreshed periodically)."""

    def __init__(self):
        self.prices: dict[str, float] = {}
        self._last_fetch = 0.0
        self._lock = asyncio.Lock()

    async def refresh(self) -> None:
        ids = ",".join(c.coingecko_id for c in settings.chains.values())
        try:
            async with _new_client(10) as client:
                headers = {}
                if settings.price_api_key:
                    headers["x-cg-demo-api-key"] = settings.price_api_key
                r = await client.get(
                    f"{settings.coingecko_url}/simple/price",
                    params={"ids": ids, "vs_currencies": "usd"}, headers=headers)
                r.raise_for_status()
                data = r.json()
                for key, cfg in settings.chains.items():
                    p = data.get(cfg.coingecko_id, {}).get("usd")
                    if p:
                        self.prices[key] = float(p)
            self._last_fetch = time.time()
            log.info("native prices updated: %s", self.prices)
        except Exception as e:
            log.warning("native price refresh failed: %s", str(e)[:120])

    async def get(self, chain_key: str) -> float | None:
        if time.time() - self._last_fetch > 120:
            async with self._lock:
                if time.time() - self._last_fetch > 120:
                    await self.refresh()
        return self.prices.get(chain_key)


class PoolPriceProvider(PriceProvider):
    """DEX pool price: if a token has a pair against a stable or wrapped
    native, derive price from pool reserves (getReserves eth_call)."""
    name = "dex_pool"
    SEL_GET_RESERVES = "0x0902f1ac"

    def __init__(self, adapter: BaseChainAdapter, chain_key: str,
                 native: NativeAssetPrice, session_factory):
        self.adapter = adapter
        self.chain_key = chain_key
        self.native = native
        self.session_factory = session_factory
        self._cache: dict[str, tuple[float, float]] = {}  # addr -> (price, ts)

    def _find_quote_pair(self, token_address: str):
        """Return (pair_address, token_is_0, quote_kind) using DB pairs."""
        def _q():
            with self.session_factory() as s:
                chain_pk = s.query(Chain.id).filter_by(key=self.chain_key).scalar()
                if not chain_pk:
                    return None
                stable_addrs = set(STABLES.get(self.chain_key, {}))
                wnat = WRAPPED_NATIVE.get(self.chain_key)
                rows = s.query(TokenPair).filter(
                    TokenPair.chain_pk == chain_pk,
                    (TokenPair.token0 == token_address) | (TokenPair.token1 == token_address)
                ).limit(10).all()
                for pr in rows:
                    other = pr.token1 if pr.token0 == token_address else pr.token0
                    tok_is_0 = pr.token0 == token_address
                    if other in stable_addrs:
                        return (pr.pair_address, tok_is_0, "stable")
                    if other == wnat:
                        return (pr.pair_address, tok_is_0, "native")
                return None
        return _q()

    async def get_price(self, chain_key: str, token_address: str) -> float | None:
        if chain_key != self.chain_key:
            return None
        token_address = token_address.lower()
        hit = self._cache.get(token_address)
        if hit and time.time() - hit[1] < 60:
            return hit[0]
        try:
            found = await asyncio.to_thread(self._find_quote_pair, token_address)
            if not found:
                return None
            pair_addr, tok_is_0, kind = found
            res = await self.adapter.eth_call(pair_addr, self.SEL_GET_RESERVES)
            if not res or len(res) < 194:
                return None
            body = bytes.fromhex(res[2:])
            r0 = int.from_bytes(body[0:32], "big")
            r1 = int.from_bytes(body[32:64], "big")
            if r0 == 0 or r1 == 0:
                return None
            with self.session_factory() as s:
                chain_pk = s.query(Chain.id).filter_by(key=chain_key).scalar()
                t0 = s.query(Token).filter_by(chain_pk=chain_pk, address=token_address).first()
                d_tok = t0.decimals if t0 and t0.decimals is not None else 18
                # fetch decimals of quote token
                q = s.query(TokenPair).filter_by(chain_pk=chain_pk, pair_address=pair_addr).first()
                quote_addr = q.token1 if tok_is_0 else q.token0
                qt = s.query(Token).filter_by(chain_pk=chain_pk, address=quote_addr).first()
                d_q = qt.decimals if qt and qt.decimals is not None else 18
            rt, rq = (r0, r1) if tok_is_0 else (r1, r0)
            raw = Decimal(rq) / Decimal(rt) * Decimal(10) ** Decimal(d_tok - d_q)
            if kind == "stable":
                price = float(raw) * STABLES[chain_key].get(quote_addr, 1.0)
            else:
                nat = await self.native.get(chain_key)
                if nat is None:
                    return None
                price = float(raw) * nat
            if price > 0:
                self._cache[token_address] = (price, time.time())
                return price
        except Exception as e:
            log.debug("pool price error %s: %s", token_address, e)
        return None


class CoingeckoProvider(PriceProvider):
    """Optional public market-data API (demo endpoint, no key required)."""
    name = "coingecko"
    CONTRACT_PATHS = {"ethereum": "ethereum", "bsc": "binance-smart-chain"}

    def __init__(self):
        self._cache: dict[str, tuple[float, float]] = {}
        self._calls = 0.0

    async def get_price(self, chain_key: str, token_address: str) -> float | None:
        path = self.CONTRACT_PATHS.get(chain_key)
        if not path or settings.mock_mode:
            return None
        token_address = token_address.lower()
        hit = self._cache.get(token_address)
        if hit and time.time() - hit[1] < 300:
            return hit[0]
        # crude rate limit (~10s between external calls)
        if time.time() - self._calls < 10:
            return None
        self._calls = time.time()
        try:
            async with _new_client(10) as client:
                r = await client.get(
                    f"{settings.coingecko_url}/simple/token/price",
                    params={"contract_addresses": f"{path}:{token_address}",
                            "vs_currencies": "usd"})
                r.raise_for_status()
                data = r.json()
                for v in data.values():
                    p = v.get("usd")
                    if p:
                        self._cache[token_address] = (float(p), time.time())
                        return float(p)
        except Exception as e:
            log.debug("coingecko price error %s: %s", token_address, str(e)[:100])
        return None


class PriceService:
    """Tries providers in order; caches results; never raises to caller."""

    def __init__(self, adapter: BaseChainAdapter, chain_key: str, session_factory,
                 native: NativeAssetPrice):
        self.chain_key = chain_key
        self.providers: list[PriceProvider] = [
            StablecoinProvider(),
            PoolPriceProvider(adapter, chain_key, native, session_factory),
            CoingeckoProvider(),
        ]
        self.native = native
        self._neg_cache: dict[str, float] = {}  # addr -> last "no price" time

    async def get_price(self, token_address: str) -> tuple[float | None, str | None]:
        token_address = token_address.lower()
        # negative cache: don't hammer providers for unpriceable tokens
        last = self._neg_cache.get(token_address)
        if last and time.time() - last < 300:
            return None, None
        for p in self.providers:
            try:
                price = await p.get_price(self.chain_key, token_address)
                if price and price > 0:
                    return price, p.name
            except Exception as e:
                log.debug("provider %s failed: %s", p.name, e)
        self._neg_cache[token_address] = time.time()
        return None, None
