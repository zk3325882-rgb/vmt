"""Generic async EVM JSON-RPC adapter.

Handles: multiple RPC endpoints with failover, timeouts, retries with
exponential backoff, address validation, log fetching. Chain-specific
subclasses/factories (ethereum.py / bsc.py) only carry configuration.
"""
from __future__ import annotations

import asyncio
import hashlib
import itertools
import logging
import random
import re
import time

import httpx

from app.blockchain.base import BaseChainAdapter, BlockHeader, LogEntry, TxInfo
from config import (TRANSFER_TOPIC, V2_BURN_TOPIC, V2_MINT_TOPIC,
                    V2_SWAP_TOPIC, ChainConfig, settings)

log = logging.getLogger("evm")

ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
ZERO_ADDR = "0x" + "0" * 40

# Event topic constants are defined once in config.py (verified keccak256
# signatures for ERC-4626 Deposit/Withdraw and Uniswap V2 Swap/Mint/Burn)
# and imported above to avoid drift between modules.
SEL_TOKEN0 = "0x0dfe1681"
SEL_TOKEN1 = "0xd21220a7"
SEL_GET_RESERVES = "0x0902f1ac"


def is_valid_address(a: str | None) -> bool:
    return bool(a) and bool(ADDR_RE.match(a))


class EVMAdapter(BaseChainAdapter):
    """Async JSON-RPC client for any EVM-compatible chain."""

    def __init__(self, cfg: ChainConfig):
        super().__init__(cfg.key)
        self.cfg = cfg
        urls = [u for u in cfg.rpc_urls if u.startswith("http")] or list(cfg.rpc_urls)
        self._urls = itertools.cycle(urls)          # rotate on failures
        self._url = urls[0] if urls else ""
        self._client: httpx.AsyncClient | None = None
        self._id = itertools.count(1)

    # ---------------- connection management ----------------
    async def connect(self) -> bool:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=settings.rpc_timeout)
        try:
            await self.block_number()
            self.connected = True
            log.info("[%s] connected via %s", self.chain_key, self._url)
            return True
        except Exception as e:
            log.warning("[%s] connect failed: %s", self.chain_key, str(e)[:150])
            self.connected = False
            return False

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
        self.connected = False

    async def _switch_url(self) -> None:
        self._url = next(self._urls)
        log.info("[%s] switching RPC endpoint to %s", self.chain_key, self._url)

    @staticmethod
    def _is_permanent_rpc_error(err: Exception) -> bool:
        """Errors that switching endpoints / retrying cannot fix.

        Public free RPCs reject unfiltered eth_getLogs outright; retrying the
        same request on another node just burns time and rate-limit budget.
        """
        m = str(err).lower()
        return ("please specify an address" in m
                or "-32701" in m                      # alchemy-style restriction
                or "block range too large" in m
                or "range too large" in m
                or "exceed the maximum block range" in m
                or "query returned more than" in m)   # max-result-count limits

    async def _rpc(self, method: str, params: list | None = None):
        """JSON-RPC call with retry/backoff, endpoint failover and a circuit
        breaker for permanent errors (retried requests are pinned to the
        endpoint that served them — only genuine transient failures rotate)."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=settings.rpc_timeout)
        delay = 0.5
        last_err: Exception | None = None
        pinned_url = self._url          # don't re-rotate endpoints on retries
        for attempt in range(settings.max_retries):
            payload = {"jsonrpc": "2.0", "id": next(self._id), "method": method,
                       "params": params or []}
            try:
                resp = await self._client.post(pinned_url, json=payload)
                if resp.status_code == 429:
                    raise RuntimeError("rate limited (429)")
                resp.raise_for_status()
                data = resp.json()
                if "error" in data:
                    err = data["error"]
                    raise RuntimeError(f"RPC error: {err}")
                self.connected = True
                return data["result"]
            except Exception as e:
                last_err = e
                if self._is_permanent_rpc_error(e):
                    # fail fast: caller can adapt (narrow range / add filter)
                    raise ConnectionError(
                        f"[{self.chain_key}] RPC {method} permanent error: {e}") from e
                log.debug("[%s] rpc %s attempt %d failed: %s",
                          self.chain_key, method, attempt + 1, str(e)[:160])
                if attempt >= 1 and attempt % 2 == 0:
                    await self._switch_url()
                    pinned_url = self._url
                await asyncio.sleep(delay + random.uniform(0, delay / 2))
                delay = min(delay * 2, 30.0)
        raise ConnectionError(f"[{self.chain_key}] RPC {method} failed after retries: {last_err}")

    # ---------------- chain interface ----------------
    async def block_number(self) -> int:
        return int(await self._rpc("eth_blockNumber"), 16)

    async def get_block_header(self, number: int) -> BlockHeader | None:
        raw = await self._rpc("eth_getBlockByNumber", [hex(number), False])
        if not raw:
            return None
        return BlockHeader(number=int(raw["number"], 16),
                           hash=raw.get("hash") or "",
                           timestamp=int(raw["timestamp"], 16))

    @staticmethod
    def _is_unfiltered_rejection(err: Exception) -> bool:
        """Node refuses requests that don't pin an `address` filter.

        Bisecting the block range cannot fix this — only adding an address
        filter can — so we must never split on these errors."""
        m = str(err).lower()
        return ("please specify an address" in m
                or "-32701" in m)                   # alchemy-style restriction

    @staticmethod
    def _is_range_rejection(err: Exception) -> bool:
        """Node rejects the *size* of the requested range / result count.
        Splitting the range into smaller pieces is the correct remedy."""
        m = str(err).lower()
        return ("block range too large" in m
                or "range too large" in m
                or "exceed the maximum block range" in m
                or "query returned more than" in m)

    async def get_logs(self, from_block: int, to_block: int,
                       topics=None, addresses=None) -> list[LogEntry]:
        """Fetch logs, auto-bisecting the range when the node rejects it
        because the range/result set is too large.

        Unfiltered-request rejections (HTTP-level "specify an address") are
        NOT bisected — splitting cannot help; they propagate immediately so
        callers can add an address filter or skip the query."""
        try:
            return await self._get_logs_raw(from_block, to_block, topics, addresses)
        except ConnectionError as e:
            if self._is_unfiltered_rejection(e):
                raise                                # bisecting is pointless
            msg = str(e).lower()
            retriable = (self._is_range_rejection(e)
                         or "permanent error" in msg
                         or "525" in msg or "524" in msg or "timed out" in msg
                         or "timeout" in msg)
            if not retriable:
                raise
            mid = (from_block + to_block) // 2
            if mid < to_block:      # can still split
                log.info("[%s] getLogs %d-%d rejected, bisecting (%s)",
                         self.chain_key, from_block, to_block,
                         str(e)[len(f"[{self.chain_key}]"):][:120])
                left = await self.get_logs(from_block, mid, topics, addresses)
                right = await self.get_logs(mid + 1, to_block, topics, addresses)
                return left + right
            raise

    async def _get_logs_raw(self, from_block: int, to_block: int,
                            topics, addresses) -> list[LogEntry]:
        params: dict = {"fromBlock": hex(from_block), "toBlock": hex(to_block)}
        if addresses:
            params["address"] = [a.lower() for a in addresses]
        if topics:
            params["topics"] = topics
        raw = await self._rpc("eth_getLogs", [params]) or []
        out = []
        for r in raw:
            try:
                out.append(LogEntry(
                    address=(r.get("address") or "").lower(),
                    topics=r.get("topics") or [],
                    data=r.get("data") or "0x",
                    block_number=int(r["blockNumber"], 16),
                    transaction_hash=r.get("transactionHash") or "",
                    log_index=int(r.get("logIndex", "0x0"), 16),
                ))
            except (KeyError, ValueError) as e:
                log.warning("[%s] malformed log skipped: %s", self.chain_key, e)
        return out

    async def eth_call(self, to: str, data: str) -> str | None:
        if not is_valid_address(to):
            return None
        try:
            return await self._rpc("eth_call", [{"to": to, "data": data}, "latest"])
        except Exception:
            return None

    # ---- MOCK DEX fixtures (dev mode only; deterministic) ----
    MOCK_POOL = "0x" + "cc" * 20          # uniswap_v2 TTK/MUSD pair
    MOCK_TTK = "0x" + "aa" * 20           # traded token (18 dec, $2 via pool)
    MOCK_MUSD = "0x" + "bb" * 20          # mock stable quote ($1)
    V2_SWAP_T = V2_SWAP_TOPIC             # canonical values live in config.py
    V2_MINT_T = V2_MINT_TOPIC
    V2_BURN_T = V2_BURN_TOPIC

    @staticmethod
    def _w(x: int) -> str:
        return f"{x:064x}"

    def _mock_dex_logs(self, number: int) -> list[LogEntry]:
        """Deterministic synthetic swap/liquidity activity for MOCK_MODE."""
        out: list[LogEntry] = []
        base = number * 1000
        txh = "0x" + hashlib.sha256(f"dex:{number}".encode()).hexdigest()
        user = self.MOCK_WALLETS[number % 6]
        w = self._w
        data_hex = (w(2 * 10 ** 18) if number % 2 == 0 else w(0)) \
            + w(0 if number % 2 == 0 else 5 * 10 ** 18) \
            + w(0 if number % 2 == 0 else 1 * 10 ** 18) \
            + w(10 ** 18 if number % 2 == 0 else 0)
        out.append(LogEntry(
            address=self.MOCK_POOL,
            topics=[self.V2_SWAP_T, "0x" + "0" * 24 + user[2:],
                    "0x" + "0" * 24 + user[2:]],
            data="0x" + data_hex, block_number=number,
            transaction_hash=txh, log_index=base))
        out.append(LogEntry(  # internal router hop: pool -> user (token out)
            address=self.MOCK_TTK,
            topics=[TRANSFER_TOPIC, "0x" + "0" * 24 + self.MOCK_POOL[2:],
                    "0x" + "0" * 24 + user[2:]],
            data="0x" + w(10 ** 18), block_number=number,
            transaction_hash=txh, log_index=base + 1))
        if number % 3 == 0:   # periodic liquidity add
            out.append(LogEntry(
                address=self.MOCK_POOL,
                topics=[self.V2_MINT_T, "0x" + "0" * 24 + user[2:]],
                data="0x" + w(10 ** 18) + w(2 * 10 ** 18),
                block_number=number, transaction_hash=txh, log_index=base + 2))
        if number % 7 == 0:   # rarer liquidity removal
            out.append(LogEntry(
                address=self.MOCK_POOL,
                topics=[self.V2_BURN_T, "0x" + "0" * 24 + user[2:],
                        "0x" + "0" * 24 + user[2:]],
                data="0x" + w(5 * 10 ** 17) + w(10 ** 18),
                block_number=number, transaction_hash=txh, log_index=base + 3))
        return out

    async def get_transactions_for_block(self, number: int) -> list[TxInfo]:
        raw = await self._rpc("eth_getBlockByNumber", [hex(number), True])
        if not raw:
            return []
        ts = []
        for t in raw.get("transactions") or []:
            try:
                ts.append(TxInfo(hash=t.get("hash") or "",
                                 from_address=(t.get("from") or "").lower(),
                                 to_address=(t.get("to") or "").lower() or None,
                                 block_number=int(t["blockNumber"], 16),
                                 status=int(t.get("status", "0x1"), 16)))
            except (KeyError, ValueError):
                continue
        return ts

    # ---------------- Phase 3 helpers (best-effort; None on failure) -------
    async def get_tx_receipt(self, tx_hash: str) -> dict | None:
        """Raw receipt dict (input/to/logs). Returns None on any RPC failure —
        swap decoding must never depend exclusively on transaction input."""
        try:
            return await self._rpc("eth_getTransactionReceipt", [tx_hash])
        except Exception:
            return None

    async def fetch_token_balances(self, address: str,
                                   tokens: list[str]) -> dict[str, int]:
        """batch eth_call balanceOf(address); failures are simply omitted."""
        sel = "0x70a08231" + address.lower()[2:].rjust(64, "0")
        out: dict[str, int] = {}
        for tok in tokens:
            res = await self.eth_call(tok, sel)
            if res and res != "0x" and len(res) >= 66:
                try:
                    out[tok.lower()] = int(res[:66], 16) if len(res) == 66 \
                        else int(res[2:66], 16)
                except ValueError:
                    continue
        return out


class MockEVMAdapter(EVMAdapter):
    """Offline synthetic chain source for development/testing (MOCK_MODE=true).

    Generates deterministic-but-realistic blocks with ERC-20 Transfer logs so
    the whole pipeline can be exercised without internet. Clearly separated;
    never used in production mode.
    """

    MOCK_TOKENS = [
        ("0x" + "aa" * 20, "MOCK", "Mock Token", 18),
        ("0x" + "bb" * 20, "MUSD", "Mock Stable", 6),
        ("0x" + "cc" * 20, None, None, None),      # token without metadata
    ]
    MOCK_WALLETS = ["0x" + f"{i:040x}" for i in range(1, 7)]
    # pool address -> (token0, token1, raw reserve0 units, raw reserve1 units)
    MOCK_POOLS_RAW = {
        "0x" + "ee" * 20: ("0x" + "aa" * 20, "0x" + "bb" * 20, 5_000_000, 500_000),
        "0x" + "ef" * 20: ("0x" + "aa" * 20, "0x" + "cc" * 20, 2_000_000, 400_000),
    }

    def __init__(self, cfg: ChainConfig):
        super().__init__(cfg)
        self._head = cfg.start_block + 1_000_000
        self._t0 = time.time()
        self.connected = True

    @property
    def MOCK_POOLS(self):
        return {p.lower(): v for p, v in self.MOCK_POOLS_RAW.items()}

    async def connect(self) -> bool:
        self.connected = True
        return True

    async def close(self) -> None:
        self.connected = False

    async def block_number(self) -> int:
        step = 3 if self.chain_key == "bsc" else 12
        return self._head + int(time.time() - self._t0) // step

    async def get_block_header(self, number: int) -> BlockHeader | None:
        # block hash must fit SQLite INTEGER / be sane length; keep real-looking
        import hashlib as _hl
        bhash = "0x" + _hl.sha256(f"blk:{self.chain_key}:{number}".encode()).hexdigest()
        return BlockHeader(number=number, hash=bhash[:66],
                           timestamp=int(1700000000 + (number % 100000) * 12))

    @staticmethod
    def _topic(addr: str) -> str:
        return "0x" + "0" * 24 + addr[2:]

    async def get_logs(self, from_block, to_block, topics=None, addresses=None):
        import hashlib
        def _match(lg) -> bool:
            if addresses and lg.address.lower() not in {a.lower() for a in addresses}:
                return False
            if topics:
                for i, t in enumerate(topics):
                    if t is None:
                        continue
                    wanted = t if isinstance(t, list) else [t]
                    if i >= len(lg.topics) or lg.topics[i].lower() not in {w.lower() for w in wanted}:
                        return False
            return True
        logs = []
        for blk in range(from_block, to_block + 1):
            h = int(hashlib.md5(f"{self.chain_key}:{blk}".encode()).hexdigest()[:8], 16)
            n_transfers = h % 4                       # 0..3 transfers per block
            txh = "0x" + hashlib.sha256(f"tx:{blk}".encode()).hexdigest()
            for i in range(n_transfers):
                tok = self.MOCK_TOKENS[(h + i) % len(self.MOCK_TOKENS)]
                frm = self.MOCK_WALLETS[(h + i) % 6]
                to = self.MOCK_WALLETS[(h + i + 3) % 6]
                scale = 10**24 if (h % 7 == 0 and i == 0) else 10**18 + (h % 10) * 10**17
                big = hex(scale)[2:].rjust(64, "0")
                logs.append(LogEntry(
                    address=tok[0].lower(),
                    topics=[TRANSFER_TOPIC, self._topic(frm), self._topic(to)],
                    data="0x" + big, block_number=blk, transaction_hash=txh,
                    log_index=i))
            # ---- Phase 3 mock DEX activity on deterministic blocks -------
            if h % 5 == 0:
                pools = sorted(self.MOCK_POOLS.items())
                pool_addr, (t0, t1, _, _) = pools[h % len(pools)]
                user = self.MOCK_WALLETS[h % 6]
                w32 = lambda v: format(v, "064x")
                amt_in = (10_000 + (h % 50) * 500) << 6          # MUSD 6-dec
                amt_out = (amt_in // 2000) << 12                 # MOCK 18-dec
                if h % 10 == 5:      # SELL_LIKE direction
                    logs.append(LogEntry(address=t0, topics=[TRANSFER_TOPIC,
                        self._topic(user), self._topic(pool_addr)],
                        data="0x" + w32(amt_out), block_number=blk,
                        transaction_hash=txh, log_index=899))
                logs.append(LogEntry(
                    address=pool_addr,
                    topics=[V2_SWAP_TOPIC, self._topic(user),
                            self._topic(user if h % 10 != 5 else user)],
                    data="0x" + (w32(0) + w32(amt_in) + w32(amt_out) + w32(0)
                                 if h % 10 != 5 else
                                 w32(amt_out) + w32(0) + w32(0) + w32(amt_in)),
                    block_number=blk, transaction_hash=txh, log_index=900))
                logs.append(LogEntry(
                    address=t1, topics=[TRANSFER_TOPIC, self._topic(pool_addr),
                                        self._topic(user)],
                    data="0x" + w32(amt_out), block_number=blk,
                    transaction_hash=txh, log_index=901))
            if h % 23 == 0:      # liquidity burn (removal) event
                pools = sorted(self.MOCK_POOLS.items())
                pool_addr, _ = pools[h % len(pools)]
                lp = self.MOCK_WALLETS[(h + 2) % 6]
                w32 = lambda v: format(v, "064x")
                logs.append(LogEntry(
                    address=pool_addr,
                    topics=[V2_BURN_TOPIC, self._topic(lp)],
                    data="0x" + w32(50_000 << 18) + w32(25_000 << 6)
                         + "0" * 24 + lp[2:],
                    block_number=blk, transaction_hash=txh, log_index=950))
            if h % 29 == 0:      # liquidity mint (addition) event
                pools = sorted(self.MOCK_POOLS.items())
                pool_addr, _ = pools[h % len(pools)]
                lp = self.MOCK_WALLETS[(h + 4) % 6]
                w32 = lambda v: format(v, "064x")
                logs.append(LogEntry(
                    address=pool_addr,
                    topics=[V2_MINT_TOPIC, self._topic(lp)],
                    data="0x" + w32(30_000 << 18) + w32(15_000 << 6),
                    block_number=blk, transaction_hash=txh, log_index=960))
        return [l for l in logs if _match(l)]

    async def eth_call(self, to: str, data: str) -> str | None:
        to = to.lower()
        sel = data[:10]
        # MOCK DEX pool: token0()/token1()/getReserves() for synthetic pairs
        if to in {p.lower() for p in self.MOCK_POOLS}:
            t0, t1, r0, r1 = self.MOCK_POOLS[to]
            if sel == "0x0dfe1681":   # token0()
                return "0x" + "0" * 24 + t0[2:]
            if sel == "0xd21220a7":   # token1()
                return "0x" + "0" * 24 + t1[2:]
            if sel == "0x0902f1ac":   # getReserves()
                return ("0x" + f"{r0 << 18:064x}" + f"{r1 << (12 if t1.endswith('bb' * 20) else 18):064x}"
                        + "0" * 64)
        for addr, sym, name, dec in self.MOCK_TOKENS:
            if addr.lower() != to:
                continue
            if sel == "0x06fdde03" and name:      # name()
                enc = name.encode().hex()
                return "0x" + "0"*62 + "20" + "0"*62 + f"{len(name):x}".rjust(64, "0") + enc.ljust(((-len(enc)-1)//64+1)*64, "0")
            if sel == "0x95d89b41" and sym:       # symbol()
                enc = sym.encode().hex()
                return "0x" + "0"*62 + "20" + "0"*62 + f"{len(sym):x}".rjust(64, "0") + enc.ljust(((-len(enc)-1)//64+1)*64, "0")
            if sel == "0x313ce567" and dec is not None:   # decimals()
                return "0x" + f"{dec:064x}"
        return None

    async def get_transactions_for_block(self, number: int) -> list[TxInfo]:
        import hashlib
        txh = "0x" + hashlib.sha256(f"tx:{number}".encode()).hexdigest()
        return [TxInfo(hash=txh, from_address=self.MOCK_WALLETS[number % 6],
                       to_address=self.MOCK_WALLETS[(number + 1) % 6],
                       block_number=number)]

    async def get_tx_receipt(self, tx_hash: str) -> dict | None:
        # deterministic pseudo-receipt: every 5th tx went through a router
        num = int(tx_hash[2:10], 16)
        sel = "0x7ff36ab5" if num % 5 == 0 else "0x83bd37f9"
        return {"transactionHash": tx_hash, "input": sel + "00" * 32}
