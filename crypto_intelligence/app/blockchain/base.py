"""Abstract blockchain adapter interface.

Phase 2+ (Solana, extra EVM chains) only needs a new implementation of this
interface plus a ChainConfig entry - the scanner core never changes.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass


@dataclass
class LogEntry:
    address: str          # emitting contract (lowercase)
    topics: list[str]
    data: str
    block_number: int
    transaction_hash: str
    log_index: int


@dataclass
class BlockHeader:
    number: int
    hash: str
    timestamp: int        # unix seconds (chain time, UTC)


@dataclass
class TxInfo:
    hash: str
    from_address: str
    to_address: str | None
    block_number: int
    status: int = 1


class BaseChainAdapter(abc.ABC):
    """Minimal async interface every chain adapter must implement."""

    def __init__(self, chain_key: str):
        self.chain_key = chain_key
        self.connected = False

    @abc.abstractmethod
    async def connect(self) -> bool: ...

    @abc.abstractmethod
    async def close(self) -> None: ...

    @abc.abstractmethod
    async def block_number(self) -> int: ...

    @abc.abstractmethod
    async def get_block_header(self, number: int) -> BlockHeader | None: ...

    @abc.abstractmethod
    async def get_logs(self, from_block: int, to_block: int,
                       topics=None, addresses=None) -> list[LogEntry]: ...

    @abc.abstractmethod
    async def eth_call(self, to: str, data: str) -> str | None:
        """Raw `eth_call`; returns hex result string or None."""

    @abc.abstractmethod
    async def get_transactions_for_block(self, number: int) -> list[TxInfo]: ...

    async def healthy(self) -> bool:
        try:
            await self.block_number()
            return True
        except Exception:
            return False
