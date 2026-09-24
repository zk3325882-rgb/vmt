"""DEX discovery collector: PairCreated (+ optionally Swap) events.

Uses per-chain configurable factory contracts (config.DexConfig) so more DEX
protocols can be added without code changes. Discovered pairs feed token
discovery and later pool-price valuation.
"""
from __future__ import annotations

import logging

from app.blockchain.base import BaseChainAdapter, LogEntry
from config import DexConfig

log = logging.getLogger("dex")

PAIR_CREATED_TOPIC = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"


def topic_to_addr(topic: str) -> str:
    return "0x" + topic[-40:].lower()


def decode_pair_created(entry: LogEntry) -> tuple[str, str, str] | None:
    """PairCreated(token0, token1, pair, uint256) -> (t0, t1, pair)."""
    try:
        if len(entry.topics) < 4:
            return None
        t0 = topic_to_addr(entry.topics[1])
        t1 = topic_to_addr(entry.topics[2])
        pair = topic_to_addr(entry.topics[3])
        return t0, t1, pair
    except Exception:
        return None


class DexCollector:
    def __init__(self, adapter: BaseChainAdapter, dexes: tuple[DexConfig, ...]):
        self.adapter = adapter
        self.dexes = dexes
        self.factories = [d.factory_address.lower() for d in dexes if d.factory_address]

    async def fetch_events(self, from_block: int, to_block: int) -> list[tuple[DexConfig, LogEntry]]:
        """Fetch PairCreated logs from configured factories in this range."""
        out: list[tuple[DexConfig, LogEntry]] = []
        if not self.factories:
            return out
        try:
            logs = await self.adapter.get_logs(
                from_block, to_block,
                topics=[PAIR_CREATED_TOPIC], addresses=self.factories)
        except Exception as e:
            log.warning("dex log fetch failed %d-%d: %s", from_block, to_block, str(e)[:120])
            return out
        fac_names = {d.factory_address.lower(): d for d in self.dexes if d.factory_address}
        for entry in logs:
            dex = fac_names.get(entry.address)
            if dex:
                out.append((dex, entry))
        return out
