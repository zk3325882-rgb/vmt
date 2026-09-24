"""Chain manager: instantiates adapters for every configured chain.

Adding Polygon/Arbitrum/Base/... = add a ChainConfig entry in config.py;
the generic EVM factory below picks it up automatically (no per-chain code).
ENABLE_CHAINS env var (CSV) restricts which chains run, e.g.:
    ENABLE_CHAINS=ethereum,bsc,polygon
"""
from __future__ import annotations

import logging

from app.blockchain.base import BaseChainAdapter
from app.blockchain.evm import EVMAdapter, MockEVMAdapter
from config import CHAINS, settings

log = logging.getLogger("manager")


def _make_for(key: str):
    """Factory closure for any chain key registered in config.CHAINS."""
    def factory(mock: bool | None = None) -> BaseChainAdapter:
        use_mock = settings.mock_mode if mock is None else mock
        cls = MockEVMAdapter if use_mock else EVMAdapter
        return cls(CHAINS[key])
    return factory


# registry built from config — every ChainConfig entry gets an adapter
FACTORIES = {key: _make_for(key) for key in CHAINS}


class ChainManager:
    def __init__(self, mock: bool | None = None):
        enabled = [k.strip().lower() for k in
                   (settings.enabled_chains or "").split(",") if k.strip()]
        self.adapters: dict[str, BaseChainAdapter] = {}
        for key, factory in FACTORIES.items():
            if key not in settings.chains:
                continue
            if enabled and key not in enabled:
                log.info("chain %s disabled via ENABLE_CHAINS - skipping", key)
                continue
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
