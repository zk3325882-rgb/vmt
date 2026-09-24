"""SQLAlchemy models - Phase 1 minimal table set.

Financial precision rule: token amounts stored as BigInteger (raw) and
Numeric/Decimal (normalized, USD) - never float. Timestamps are UTC.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, Numeric,
    String, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Chain(Base):
    __tablename__ = "chains"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    chain_id: Mapped[int] = mapped_column(Integer, unique=True)
    native_symbol: Mapped[str] = mapped_column(String(16), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    head_block: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Token(Base):
    __tablename__ = "tokens"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chain_pk: Mapped[int] = mapped_column(ForeignKey("chains.id"), index=True)
    address: Mapped[str] = mapped_column(String(64), index=True)  # lowercase
    symbol: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str | None] = mapped_column(String(128))
    decimals: Mapped[int | None] = mapped_column(Integer)
    category: Mapped[str] = mapped_column(String(32), default="Unknown", index=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    first_transfer_block: Mapped[int | None] = mapped_column(BigInteger)
    first_pool_block: Mapped[int | None] = mapped_column(BigInteger)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    discovery_source: Mapped[str] = mapped_column(String(32), default="transfer_event")
    transfer_count: Mapped[int] = mapped_column(BigInteger, default=0)
    has_metadata: Mapped[bool] = mapped_column(Boolean, default=False)
    price_usd: Mapped[float | None] = mapped_column(Numeric(30, 18))
    price_timestamp: Mapped[datetime | None] = mapped_column(DateTime)
    price_source: Mapped[str | None] = mapped_column(String(32))
    liquidity_usd: Mapped[float | None] = mapped_column(Numeric(30, 2))
    __table_args__ = (
        UniqueConstraint("chain_pk", "address", name="uq_token_chain_address"),
        Index("ix_tokens_last_seen", "last_seen"),
    )


class TokenPair(Base):
    __tablename__ = "token_pairs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chain_pk: Mapped[int] = mapped_column(ForeignKey("chains.id"), index=True)
    pair_address: Mapped[str] = mapped_column(String(64), index=True)
    dex_name: Mapped[str] = mapped_column(String(64))
    token0: Mapped[str] = mapped_column(String(64), index=True)
    token1: Mapped[str] = mapped_column(String(64), index=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    liquidity_usd: Mapped[float | None] = mapped_column(Numeric(30, 2))
    __table_args__ = (
        UniqueConstraint("chain_pk", "pair_address", name="uq_pair_chain_address"),
    )


class Block(Base):
    __tablename__ = "blocks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chain_pk: Mapped[int] = mapped_column(ForeignKey("chains.id"), index=True)
    block_number: Mapped[int] = mapped_column(BigInteger, index=True)
    block_hash: Mapped[str | None] = mapped_column(String(80))
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    __table_args__ = (
        UniqueConstraint("chain_pk", "block_number", name="uq_block_chain_number"),
    )


class Transaction(Base):
    __tablename__ = "transactions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chain_pk: Mapped[int] = mapped_column(ForeignKey("chains.id"), index=True)
    tx_hash: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    block_number: Mapped[int] = mapped_column(BigInteger, index=True)
    from_address: Mapped[str] = mapped_column(String(64))
    to_address: Mapped[str | None] = mapped_column(String(64))
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    status: Mapped[int | None] = mapped_column(Integer)


class TokenTransfer(Base):
    __tablename__ = "token_transfers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chain_pk: Mapped[int] = mapped_column(ForeignKey("chains.id"), index=True)
    tx_hash: Mapped[str] = mapped_column(String(80), index=True)
    log_index: Mapped[int] = mapped_column(Integer)
    block_number: Mapped[int] = mapped_column(BigInteger, index=True)
    token_address: Mapped[str] = mapped_column(String(64), index=True)
    token_symbol: Mapped[str | None] = mapped_column(String(64))
    token_decimals: Mapped[int | None] = mapped_column(Integer)
    from_address: Mapped[str] = mapped_column(String(64))
    to_address: Mapped[str] = mapped_column(String(64))
    raw_amount: Mapped[int] = mapped_column(BigInteger)
    normalized_amount: Mapped[float] = mapped_column(Numeric(40, 18))
    usd_value: Mapped[float | None] = mapped_column(Numeric(30, 2), index=True)
    flow: Mapped[str] = mapped_column(String(8), default="TRANSFER")
    transaction_type: Mapped[str] = mapped_column(String(24), default="TRANSFER")
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    __table_args__ = (
        UniqueConstraint("chain_pk", "tx_hash", "log_index", name="uq_transfer_log"),
    )


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chain_pk: Mapped[int] = mapped_column(ForeignKey("chains.id"), index=True)
    token_address: Mapped[str] = mapped_column(String(64), index=True)
    price_usd: Mapped[float | None] = mapped_column(Numeric(30, 18))
    liquidity_usd: Mapped[float | None] = mapped_column(Numeric(30, 2))
    volume_24h: Mapped[float | None] = mapped_column(Numeric(30, 2))
    source: Mapped[str] = mapped_column(String(32), default="unknown")
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class LargeTransaction(Base):
    __tablename__ = "large_transactions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chain_pk: Mapped[int] = mapped_column(ForeignKey("chains.id"), index=True)
    tx_hash: Mapped[str] = mapped_column(String(80), index=True)
    log_index: Mapped[int] = mapped_column(Integer, default=0)
    token_address: Mapped[str] = mapped_column(String(64), index=True)
    token_symbol: Mapped[str | None] = mapped_column(String(64))
    from_address: Mapped[str] = mapped_column(String(64))
    to_address: Mapped[str] = mapped_column(String(64))
    amount: Mapped[float] = mapped_column(Numeric(40, 18))
    usd_value: Mapped[float | None] = mapped_column(Numeric(30, 2), index=True)
    relative_size: Mapped[float | None] = mapped_column(Numeric(20, 4))
    volume_ratio: Mapped[float | None] = mapped_column(Numeric(20, 6))
    liquidity_ratio: Mapped[float | None] = mapped_column(Numeric(20, 6))
    percentile: Mapped[float | None] = mapped_column(Numeric(8, 2))
    anomaly_score: Mapped[float] = mapped_column(Numeric(6, 2), index=True)
    flow: Mapped[str] = mapped_column(String(8), index=True)
    transaction_type: Mapped[str] = mapped_column(String(24))
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    __table_args__ = (
        UniqueConstraint("chain_pk", "tx_hash", "log_index", name="uq_large_log"),
        Index("ix_large_score_ts", "anomaly_score", "timestamp"),
    )


class ScannerState(Base):
    __tablename__ = "scanner_state"
    chain_pk: Mapped[int] = mapped_column(ForeignKey("chains.id"), primary_key=True)
    last_processed_block: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
