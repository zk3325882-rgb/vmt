"""Ethereum chain adapter (config-only specialization of the generic EVM adapter)."""
from __future__ import annotations

from app.blockchain.evm import EVMAdapter, MockEVMAdapter
from config import CHAINS, settings


def make_adapter(mock: bool | None = None) -> EVMAdapter:
    use_mock = settings.mock_mode if mock is None else mock
    cls = MockEVMAdapter if use_mock else EVMAdapter
    return cls(CHAINS["ethereum"])
