"""Generic async EVM JSON-RPC adapter.

Handles: multiple RPC endpoints with failover, timeouts, retries with
exponential backoff, address validation, log fetching. Chain-specific
subclasses/factories (ethereum.py / bsc.py) only carry configuration.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import random
import re
import time

import httpx

from app.blockchain.base import BaseChainAdapter, BlockHeader, LogEntry, TxInfo
from config import TRANSFER_TOPIC, ChainConfig, settings

log = logging.getLogger("evm")

ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
ZERO_ADDR = "0x" + "0" * 40


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

    async def _rpc(self, method: str, params: list | None = None):
        """Single JSON-RPC call with retry/backoff and endpoint failover."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=settings.rpc_timeout)
        payload = {"jsonrpc": "2.0", "id": next(self._id), "method": method,
                   "params": params or []}
        delay = 0.5
        last_err: Exception | None = None
        for attempt in range(settings.max_retries):
            try:
                resp = await self._client.post(self._url, json=payload)
                if resp.status_code == 429:
                    raise RuntimeError("rate limited (429)")
                resp.raise_for_status()
                data = resp.json()
                if "error" in data:
                    raise RuntimeError(f"RPC error: {data['error']}")
                self.connected = True
                return data["result"]
            except Exception as e:
                last_err = e
                log.debug("[%s] rpc %s attempt %d failed: %s",
                          self.chain_key, method, attempt + 1, str(e)[:160])
                if attempt >= 1 and attempt % 2 == 0:
                    await self._switch_url()
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

    async def get_logs(self, from_block: int, to_block: int,
                       topics=None, addresses=None) -> list[LogEntry]:
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

    def __init__(self, cfg: ChainConfig):
        super().__init__(cfg)
        self._head = cfg.start_block + 1_000_000
        self._t0 = time.time()
        self.connected = True

    async def connect(self) -> bool:
        self.connected = True
        return True

    async def close(self) -> None:
        self.connected = False

    async def block_number(self) -> int:
        step = 3 if self.chain_key == "bsc" else 12
        return self._head + int(time.time() - self._t0) // step

    async def get_block_header(self, number: int) -> BlockHeader | None:
        return BlockHeader(number=number, hash="0x" + f"{number:064x}",
                           timestamp=int(1700000000 + (number - self._head) * 12
                                         + time.time()) )

    @staticmethod
    def _topic(addr: str) -> str:
        return "0x" + "0" * 24 + addr[2:]

    async def get_logs(self, from_block, to_block, topics=None, addresses=None):
        import hashlib
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
        return logs

    async def eth_call(self, to: str, data: str) -> str | None:
        to = to.lower()
        for addr, sym, name, dec in self.MOCK_TOKENS:
            if addr.lower() != to:
                continue
            sel = data[:10]
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
