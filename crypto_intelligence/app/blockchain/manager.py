"""Chain manager: instantiates adapters for every configured chain.

Adding Polygon/Arbitrum/Base/... later = add a ChainConfig entry and a small
factory module like ethereum.py; nothing in the scanner core changes.
"""
from __future__ import annotations

import logging

from app.blockchain.base import BaseChainAdapter
from app.blockchain import ethereum, bsc
from config import settings

log = logging.getLogger("manager")

# registry: chain key -> factory(mock?)->adapter. Future chains plug in here.
FACTORIES = {
    "ethereum": ethereum.make_adapter,
    "bsc": bsc.make_adapter,
}


class ChainManager:
    def __init__(self, mock: bool | None = None):
        self.adapters: dict[str, BaseChainAdapter] = {}
        for key, factory in FACTORIES.items():
            if key in settings.chains:
                self.adapters[key] = factory(mock)

    async def connect_all(self) -> dict[str, bool]:
        results = {}
        for key, ad in self.adapters.items():
            results[key] = await ad.connect()
            if not results[key]:
                log.warning("chain %s could not connect yet - will retry in loop", key)
        return results

    async def close_all(self) -> None:
        for ad in self.adapters.values():
            try:
                await ad.close()
            except Exception:
                pass

    def status(self) -> dict[str, bool]:
        return {k: ad.connected for k, ad in self.adapters.items()}
