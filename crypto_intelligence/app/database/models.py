"""SQLAlchemy models - Phase 1 minimal table set.

Financial precision rule: token amounts stored as BigInteger (raw) and
Numeric/Decimal (normalized, USD) - never float. Timestamps are UTC.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, Numeric,
    String, TypeDecorator, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class BigIntString(TypeDecorator):
    """BigInteger that is safe on SQLite (which overflows past 2^63).

    On PostgreSQL it behaves as a normal BIGINT; on SQLite the value is
    stored as a decimal string so huge ERC-20 raw amounts never crash.
    Financial precision rule: never float for token amounts.
    """
    impl = BigInteger
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "sqlite":
            return dialect.type_descriptor(String(78))
        return dialect.type_descriptor(BigInteger())

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "sqlite":
            return str(int(value))
        return int(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return int(value)


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
    raw_amount: Mapped[int] = mapped_column(BigIntString)
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


# ===========================================================================
# PHASE 2 — wallet / whale intelligence tables (Phase 1 tables untouched)
# ===========================================================================

WALLET_TYPES = ("UNKNOWN", "NORMAL_WALLET", "WHALE", "DEX", "EXCHANGE",
                "TOKEN_CONTRACT", "LIQUIDITY_POOL", "BRIDGE", "BURN_ADDRESS",
                "MINTER", "DEPLOYER", "TREASURY", "BOT_LIKE", "CONTRACT")


class Wallet(Base):
    """Observed on-chain address. Labels describe behavior only — never a
    claim about real-world identity. No private data is ever stored."""
    __tablename__ = "wallets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chain_pk: Mapped[int] = mapped_column(ForeignKey("chains.id"), index=True)
    address: Mapped[str] = mapped_column(String(64), index=True)  # lowercase
    wallet_type: Mapped[str] = mapped_column(String(24), default="UNKNOWN", index=True)
    label: Mapped[str | None] = mapped_column(String(128))
    classification_confidence: Mapped[float] = mapped_column(Numeric(4, 3), default=0)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    transaction_count: Mapped[int] = mapped_column(BigInteger, default=0)
    in_count: Mapped[int] = mapped_column(BigInteger, default=0)
    out_count: Mapped[int] = mapped_column(BigInteger, default=0)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    estimated_portfolio_value: Mapped[float | None] = mapped_column(Numeric(30, 2))
    estimated_activity_volume: Mapped[float | None] = mapped_column(Numeric(30, 2))
    exchange_inflow_usd: Mapped[float] = mapped_column(Numeric(30, 2), default=0)
    exchange_outflow_usd: Mapped[float] = mapped_column(Numeric(30, 2), default=0)
    dex_volume_usd: Mapped[float] = mapped_column(Numeric(30, 2), default=0)
    whale_score: Mapped[float] = mapped_column(Numeric(6, 2), default=0, index=True)
    accumulation_score: Mapped[float] = mapped_column(Numeric(6, 2), default=0)
    distribution_score: Mapped[float] = mapped_column(Numeric(6, 2), default=0)
    activity_score: Mapped[float] = mapped_column(Numeric(6, 2), default=0)
    risk_score: Mapped[float] = mapped_column(Numeric(6, 2), default=0)
    behavior_flags: Mapped[str | None] = mapped_column(String(256))  # CSV of flags
    behavior_reasons: Mapped[str | None] = mapped_column(String(512))
    tier: Mapped[int] = mapped_column(Integer, default=3, index=True)  # 1..3
    is_contract: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    __table_args__ = (
        UniqueConstraint("chain_pk", "address", name="uq_wallet_chain_address"),
        Index("ix_wallet_chain_addr", "chain_pk", "address"),
        Index("ix_wallet_whale", "whale_score", "last_seen"),
    )


class AddressLabel(Base):
    """Configurable public address labels (importable; not hardcoded in .py)."""
    __tablename__ = "address_labels"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chain_pk: Mapped[int] = mapped_column(ForeignKey("chains.id"), index=True)
    address: Mapped[str] = mapped_column(String(64), index=True)
    label: Mapped[str] = mapped_column(String(128))
    entity_type: Mapped[str] = mapped_column(String(24), default="unknown")
    source: Mapped[str] = mapped_column(String(32), default="manual")
    confidence: Mapped[float] = mapped_column(Numeric(4, 3), default=0.9)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    __table_args__ = (
        UniqueConstraint("chain_pk", "address", "source", name="uq_label_chain_addr_src"),
    )


class WalletSnapshot(Base):
    """Periodic incremental history snapshots for tracked wallets."""
    __tablename__ = "wallet_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"), index=True)
    date: Mapped[datetime] = mapped_column(DateTime, index=True)  # UTC day start
    tx_count: Mapped[int] = mapped_column(Integer, default=0)
    in_count: Mapped[int] = mapped_column(Integer, default=0)
    out_count: Mapped[int] = mapped_column(Integer, default=0)
    inflow_usd: Mapped[float] = mapped_column(Numeric(30, 2), default=0)
    outflow_usd: Mapped[float] = mapped_column(Numeric(30, 2), default=0)
    net_flow_usd: Mapped[float] = mapped_column(Numeric(30, 2), default=0)
    dex_volume_usd: Mapped[float] = mapped_column(Numeric(30, 2), default=0)
    exchange_inflow_usd: Mapped[float] = mapped_column(Numeric(30, 2), default=0)
    exchange_outflow_usd: Mapped[float] = mapped_column(Numeric(30, 2), default=0)
    liquidity_activity_usd: Mapped[float] = mapped_column(Numeric(30, 2), default=0)
    portfolio_usd: Mapped[float | None] = mapped_column(Numeric(30, 2))
    __table_args__ = (
        UniqueConstraint("wallet_id", "date", name="uq_snapshot_wallet_date"),
    )


class WalletHolding(Base):
    """Approximate token holdings derived from observed transfers.
    Marked incomplete where scanner coverage does not span full history."""
    __tablename__ = "wallet_holdings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"), index=True)
    chain_pk: Mapped[int] = mapped_column(ForeignKey("chains.id"), index=True)
    wallet_address: Mapped[str] = mapped_column(String(64), index=True)
    token_address: Mapped[str] = mapped_column(String(64), index=True)
    amount: Mapped[float] = mapped_column(Numeric(40, 18), default=0)
    usd_value: Mapped[float | None] = mapped_column(Numeric(30, 2))
    last_updated: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (
        UniqueConstraint("wallet_id", "token_address", name="uq_holding_wallet_token"),
        Index("ix_holding_token", "token_address", "amount"),
    )


class DexInteraction(Base):
    """Wallet <-> DEX contract/pool interactions (observable events only)."""
    __tablename__ = "dex_interactions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chain_pk: Mapped[int] = mapped_column(ForeignKey("chains.id"), index=True)
    wallet_address: Mapped[str] = mapped_column(String(64), index=True)
    dex_name: Mapped[str] = mapped_column(String(64), index=True)
    pool_address: Mapped[str | None] = mapped_column(String(64))
    token_address: Mapped[str | None] = mapped_column(String(64), index=True)
    interaction_type: Mapped[str] = mapped_column(String(24), default="UNKNOWN")
    tx_hash: Mapped[str] = mapped_column(String(80), index=True)
    usd_value: Mapped[float | None] = mapped_column(Numeric(30, 2))
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    __table_args__ = (
        UniqueConstraint("chain_pk", "tx_hash", "wallet_address", "dex_name",
                         name="uq_dex_interaction"),
    )


class WhaleEvent(Base):
    """Unified whale/event record with an explainable narrative."""
    __tablename__ = "whale_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chain_pk: Mapped[int] = mapped_column(ForeignKey("chains.id"), index=True)
    wallet_id: Mapped[int | None] = mapped_column(ForeignKey("wallets.id"), index=True)
    wallet_address: Mapped[str] = mapped_column(String(64), index=True)
    token_address: Mapped[str | None] = mapped_column(String(64), index=True)
    token_symbol: Mapped[str | None] = mapped_column(String(64))
    tx_hash: Mapped[str | None] = mapped_column(String(80), index=True)
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    usd_value: Mapped[float | None] = mapped_column(Numeric(30, 2))
    whale_score: Mapped[float] = mapped_column(Numeric(6, 2), default=0, index=True)
    accumulation_score: Mapped[float] = mapped_column(Numeric(6, 2), default=0)
    distribution_score: Mapped[float] = mapped_column(Numeric(6, 2), default=0)
    behavior_flags: Mapped[str | None] = mapped_column(String(256))
    explanation: Mapped[str | None] = mapped_column(String(512))
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    __table_args__ = (
        UniqueConstraint("tx_hash", "wallet_address", "event_type",
                         name="uq_whale_event"),
        Index("ix_whale_event_ts", "timestamp", "event_type"),
    )


class WalletCluster(Base):
    """Possible behavioral relationships between wallets (never identity)."""
    __tablename__ = "wallet_clusters"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cluster_id: Mapped[str] = mapped_column(String(40), index=True)
    chain_pk: Mapped[int] = mapped_column(ForeignKey("chains.id"), index=True)
    wallet_address: Mapped[str] = mapped_column(String(64), index=True)
    related_address: Mapped[str | None] = mapped_column(String(64))
    relationship_score: Mapped[float] = mapped_column(Numeric(6, 2), default=0)
    relationship_type: Mapped[str] = mapped_column(String(32), index=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (
        UniqueConstraint("cluster_id", "wallet_address", "related_address",
                         "relationship_type", name="uq_cluster_member"),
        Index("ix_cluster_chain_type", "chain_pk", "relationship_type"),
    )
