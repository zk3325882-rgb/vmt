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


# ===========================================================================
# PHASE 3 — DEX trading, capital flow, liquidity & market-impact tables
# (Phase 1/2 tables untouched; all new writes are deduplicated by unique keys)
# ===========================================================================

class DexProtocol(Base):
    """Configurable DEX protocol registry (one row per chain+protocol)."""
    __tablename__ = "dex_protocols"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chain_pk: Mapped[int] = mapped_column(ForeignKey("chains.id"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16), default="v2")   # v2 | v3 | ...
    factory_address: Mapped[str | None] = mapped_column(String(64))
    routers: Mapped[str | None] = mapped_column(String(512))      # CSV
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (
        UniqueConstraint("chain_pk", "name", name="uq_dex_protocol"),
    )


class DexPool(Base):
    """Tracked AMM pool. `source` distinguishes factory PairCreated discovery
    from observation-derived pools (a contract that both holds two tokens and
    emits Swap events)."""
    __tablename__ = "dex_pools"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chain_pk: Mapped[int] = mapped_column(ForeignKey("chains.id"), index=True)
    pool_address: Mapped[str] = mapped_column(String(64), index=True)
    dex_name: Mapped[str] = mapped_column(String(64), index=True)
    token0: Mapped[str] = mapped_column(String(64), index=True)
    token1: Mapped[str] = mapped_column(String(64), index=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    liquidity_usd: Mapped[float | None] = mapped_column(Numeric(30, 2))
    reserve0: Mapped[int | None] = mapped_column(BigIntString)
    reserve1: Mapped[int | None] = mapped_column(BigIntString)
    volume_24h_usd: Mapped[float | None] = mapped_column(Numeric(30, 2))
    source: Mapped[str] = mapped_column(String(24), default="pair_created")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (
        UniqueConstraint("chain_pk", "pool_address", name="uq_pool_chain_address"),
        Index("ix_pool_tokens", "chain_pk", "token0", "token1"),
        Index("ix_pool_token_any", "token0"),
    )


class DexSwap(Base):
    """Normalized user-level DEX swap (router internal hops grouped away).
    amounts stored raw (BigInteger-safe) + normalized Decimal; never float."""
    __tablename__ = "dex_swaps"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chain_pk: Mapped[int] = mapped_column(ForeignKey("chains.id"), index=True)
    tx_hash: Mapped[str] = mapped_column(String(80), index=True)
    log_index: Mapped[int] = mapped_column(Integer, default=0)
    block_number: Mapped[int] = mapped_column(BigInteger, index=True)
    wallet_address: Mapped[str] = mapped_column(String(64), index=True)
    dex_name: Mapped[str] = mapped_column(String(64), index=True)
    pool_address: Mapped[str] = mapped_column(String(64), index=True)
    token_in: Mapped[str | None] = mapped_column(String(64), index=True)
    token_out: Mapped[str | None] = mapped_column(String(64), index=True)
    amount_in_raw: Mapped[int | None] = mapped_column(BigIntString)
    amount_out_raw: Mapped[int | None] = mapped_column(BigIntString)
    amount_in: Mapped[float | None] = mapped_column(Numeric(40, 18))
    amount_out: Mapped[float | None] = mapped_column(Numeric(40, 18))
    usd_value: Mapped[float | None] = mapped_column(Numeric(30, 2), index=True)
    classification: Mapped[str] = mapped_column(String(16), default="UNKNOWN_SWAP",
                                                index=True)
    price_impact_pct: Mapped[float | None] = mapped_column(Numeric(12, 4))
    liquidity_impact: Mapped[float | None] = mapped_column(Numeric(12, 6))
    is_user_trade: Mapped[bool] = mapped_column(Boolean, default=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    __table_args__ = (
        UniqueConstraint("chain_pk", "tx_hash", "log_index", name="uq_swap_log"),
        Index("ix_swap_wallet_ts", "wallet_address", "timestamp"),
        Index("ix_swap_token_out_ts", "token_out", "timestamp"),
        Index("ix_swap_token_in_ts", "token_in", "timestamp"),
        Index("ix_swap_class_usd", "classification", "usd_value"),
    )


class LiquidityEvent(Base):
    """LIQUIDITY_ADD / LIQUIDITY_REMOVE observed via pool Mint/Burn events
    (+ LP-token transfers). A large removal is a high-priority event — it is
    NOT automatically called a rug pull."""
    __tablename__ = "liquidity_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chain_pk: Mapped[int] = mapped_column(ForeignKey("chains.id"), index=True)
    tx_hash: Mapped[str] = mapped_column(String(80), index=True)
    log_index: Mapped[int] = mapped_column(Integer, default=0)
    pool_address: Mapped[str] = mapped_column(String(64), index=True)
    dex_name: Mapped[str] = mapped_column(String(64))
    wallet_address: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(24), index=True)
    amount0_raw: Mapped[int | None] = mapped_column(BigIntString)
    amount1_raw: Mapped[int | None] = mapped_column(BigIntString)
    usd_value: Mapped[float | None] = mapped_column(Numeric(30, 2))
    liquidity_before_usd: Mapped[float | None] = mapped_column(Numeric(30, 2))
    liquidity_after_usd: Mapped[float | None] = mapped_column(Numeric(30, 2))
    liquidity_change_usd: Mapped[float | None] = mapped_column(Numeric(30, 2))
    liquidity_change_pct: Mapped[float | None] = mapped_column(Numeric(12, 4))
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    __table_args__ = (
        UniqueConstraint("chain_pk", "tx_hash", "log_index", name="uq_liq_event_log"),
        Index("ix_liq_pool_ts", "pool_address", "timestamp"),
    )


class LiquiditySnapshot(Base):
    __tablename__ = "liquidity_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chain_pk: Mapped[int] = mapped_column(ForeignKey("chains.id"), index=True)
    pool_address: Mapped[str] = mapped_column(String(64), index=True)
    token0: Mapped[str] = mapped_column(String(64))
    token1: Mapped[str] = mapped_column(String(64))
    liquidity_usd: Mapped[float | None] = mapped_column(Numeric(30, 2))
    reserve0: Mapped[int | None] = mapped_column(BigIntString)
    reserve1: Mapped[int | None] = mapped_column(BigIntString)
    volume_24h_usd: Mapped[float | None] = mapped_column(Numeric(30, 2))
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    __table_args__ = (Index("ix_liqsnap_pool_ts", "pool_address", "timestamp"),)


class TokenFlowSnapshot(Base):
    """Incremental aggregated buy/sell flow per token per time bucket.
    bucket_seconds in {60,300,900,3600,14400,86400}; rows are updated with
    DB-side deltas (never recomputed from raw history)."""
    __tablename__ = "token_flow_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chain_pk: Mapped[int] = mapped_column(ForeignKey("chains.id"), index=True)
    token_address: Mapped[str] = mapped_column(String(64), index=True)
    bucket_seconds: Mapped[int] = mapped_column(Integer, index=True)
    bucket_start: Mapped[datetime] = mapped_column(DateTime, index=True)
    buy_volume_usd: Mapped[float] = mapped_column(Numeric(30, 2), default=0)
    sell_volume_usd: Mapped[float] = mapped_column(Numeric(30, 2), default=0)
    net_flow_usd: Mapped[float] = mapped_column(Numeric(30, 2), default=0)
    buy_count: Mapped[int] = mapped_column(Integer, default=0)
    sell_count: Mapped[int] = mapped_column(Integer, default=0)
    large_buy_volume: Mapped[float] = mapped_column(Numeric(30, 2), default=0)
    large_sell_volume: Mapped[float] = mapped_column(Numeric(30, 2), default=0)
    whale_buy_volume: Mapped[float] = mapped_column(Numeric(30, 2), default=0)
    whale_sell_volume: Mapped[float] = mapped_column(Numeric(30, 2), default=0)
    other_swap_volume: Mapped[float] = mapped_column(Numeric(30, 2), default=0)
    liquidity_change_usd: Mapped[float] = mapped_column(Numeric(30, 2), default=0)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (
        UniqueConstraint("chain_pk", "token_address", "bucket_seconds",
                         "bucket_start", name="uq_flow_bucket"),
        Index("ix_flow_token_bucket_time", "token_address", "bucket_seconds",
              "bucket_start"),
    )


class WalletTradeSnapshot(Base):
    """Per-wallet DEX trading aggregates (updated incrementally)."""
    __tablename__ = "wallet_trade_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chain_pk: Mapped[int] = mapped_column(ForeignKey("chains.id"), index=True)
    wallet_address: Mapped[str] = mapped_column(String(64), index=True)
    buy_count: Mapped[int] = mapped_column(Integer, default=0)
    sell_count: Mapped[int] = mapped_column(Integer, default=0)
    other_count: Mapped[int] = mapped_column(Integer, default=0)
    buy_volume_usd: Mapped[float] = mapped_column(Numeric(30, 2), default=0)
    sell_volume_usd: Mapped[float] = mapped_column(Numeric(30, 2), default=0)
    net_trading_flow_usd: Mapped[float] = mapped_column(Numeric(30, 2), default=0)
    largest_buy_usd: Mapped[float | None] = mapped_column(Numeric(30, 2))
    largest_sell_usd: Mapped[float | None] = mapped_column(Numeric(30, 2))
    favorite_dex: Mapped[str | None] = mapped_column(String(64))
    favorite_token: Mapped[str | None] = mapped_column(String(64))
    last_trade_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow,
                                                 onupdate=utcnow)
    __table_args__ = (
        UniqueConstraint("chain_pk", "wallet_address", name="uq_wtradetx_chain_addr"),
    )


class MarketImpactEvent(Base):
    """Ranked market-impact events: LARGE_BUY / LARGE_SELL / WHALE_BUY /
    WHALE_SELL / LIQUIDITY_ADD / LIQUIDITY_REMOVE / HIGH_IMPACT_SWAP /
    CROSS_DEX_ACTIVITY / FLOW_ANOMALY / PRICE_DISCREPANCY."""
    __tablename__ = "market_impact_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chain_pk: Mapped[int] = mapped_column(ForeignKey("chains.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    token_address: Mapped[str | None] = mapped_column(String(64), index=True)
    token_symbol: Mapped[str | None] = mapped_column(String(64))
    wallet_address: Mapped[str | None] = mapped_column(String(64), index=True)
    pool_address: Mapped[str | None] = mapped_column(String(64))
    dex_name: Mapped[str | None] = mapped_column(String(64))
    tx_hash: Mapped[str | None] = mapped_column(String(80), index=True)
    usd_value: Mapped[float | None] = mapped_column(Numeric(30, 2), index=True)
    liquidity_usd: Mapped[float | None] = mapped_column(Numeric(30, 2))
    liquidity_impact: Mapped[float | None] = mapped_column(Numeric(12, 6))
    price_impact_pct: Mapped[float | None] = mapped_column(Numeric(12, 4))
    whale_score: Mapped[float | None] = mapped_column(Numeric(6, 2), index=True)
    impact_score: Mapped[float] = mapped_column(Numeric(6, 2), default=0, index=True)
    explanation: Mapped[str | None] = mapped_column(String(512))
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    __table_args__ = (
        UniqueConstraint("tx_hash", "event_type", "wallet_address",
                         name="uq_impact_event"),
        Index("ix_impact_type_ts", "event_type", "timestamp"),
        Index("ix_impact_token_ts", "token_address", "timestamp"),
    )


class FlowAnomaly(Base):
    """flow_anomaly_score 0-100 per token per hourly window (deduped)."""
    __tablename__ = "flow_anomalies"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chain_pk: Mapped[int] = mapped_column(ForeignKey("chains.id"), index=True)
    token_address: Mapped[str] = mapped_column(String(64), index=True)
    window_start: Mapped[datetime] = mapped_column(DateTime, index=True)
    current_volume_usd: Mapped[float] = mapped_column(Numeric(30, 2), default=0)
    baseline_volume_usd: Mapped[float | None] = mapped_column(Numeric(30, 2))
    relative_volume: Mapped[float | None] = mapped_column(Numeric(20, 4))
    buy_sell_ratio: Mapped[float | None] = mapped_column(Numeric(20, 4))
    imbalance: Mapped[float | None] = mapped_column(Numeric(12, 6))
    whale_participation: Mapped[float | None] = mapped_column(Numeric(12, 6))
    flow_acceleration_pct: Mapped[float | None] = mapped_column(Numeric(20, 4))
    anomaly_score: Mapped[float] = mapped_column(Numeric(6, 2), default=0, index=True)
    reasons: Mapped[str | None] = mapped_column(String(512))
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    __table_args__ = (
        UniqueConstraint("chain_pk", "token_address", "window_start",
                         name="uq_flow_anomaly_window"),
        Index("ix_anomaly_token_ts", "token_address", "timestamp"),
    )


# ===========================================================================
# PHASE 4 — feature engine / signal engine tables (earlier phases untouched)
# ===========================================================================

SIGNAL_TYPES = ("ACCUMULATION_SIGNAL", "DISTRIBUTION_SIGNAL",
                "BULLISH_FLOW_SIGNAL", "BEARISH_FLOW_SIGNAL",
                "WHALE_ACTIVITY_SIGNAL", "LIQUIDITY_RISK_SIGNAL",
                "BREAKOUT_LIKE_FLOW_SIGNAL", "CAPITAL_INFLOW_SIGNAL",
                "CAPITAL_OUTFLOW_SIGNAL", "ANOMALY_SIGNAL", "NEUTRAL_SIGNAL",
                "FLOW_REVERSAL", "WHALE_BEHAVIOR_REVERSAL",
                "ONCHAIN_MOMENTUM_SIGNAL", "NEW_TOKEN_ACTIVITY",
                "NEW_TOKEN_LIQUIDITY", "NEW_TOKEN_WHALE_ACTIVITY",
                "NEW_TOKEN_FLOW_ANOMALY")

SIGNAL_STATUSES = ("NEW", "ACTIVE", "STRENGTHENING", "WEAKENING",
                   "INVALIDATED", "EXPIRED")

# positive-direction signal types (used for MFE/MAE interpretation in P5)
POSITIVE_SIGNAL_TYPES = ("ACCUMULATION_SIGNAL", "BULLISH_FLOW_SIGNAL",
                         "CAPITAL_INFLOW_SIGNAL", "WHALE_ACTIVITY_SIGNAL",
                         "BREAKOUT_LIKE_FLOW_SIGNAL", "ONCHAIN_MOMENTUM_SIGNAL")
NEGATIVE_SIGNAL_TYPES = ("DISTRIBUTION_SIGNAL", "BEARISH_FLOW_SIGNAL",
                         "CAPITAL_OUTFLOW_SIGNAL", "LIQUIDITY_RISK_SIGNAL")


class TokenFeatureSnapshot(Base):
    """Normalized feature vector per token per time bucket. Built ONLY from
    data with timestamp <= bucket_end (no look-ahead). These rows are the
    exact inputs used by the signal engine and stored again on every signal
    via signal_features, which makes historical reconstruction reproducible."""
    __tablename__ = "token_feature_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chain_pk: Mapped[int] = mapped_column(ForeignKey("chains.id"), index=True)
    token_address: Mapped[str] = mapped_column(String(64), index=True)
    bucket_seconds: Mapped[int] = mapped_column(Integer, default=3600, index=True)
    bucket_start: Mapped[datetime] = mapped_column(DateTime, index=True)
    features: Mapped[str] = mapped_column(String(8192))   # JSON, normalized floats
    completeness: Mapped[float | None] = mapped_column(Numeric(5, 2))  # % non-null
    feature_version: Mapped[str] = mapped_column(String(16), default="1.0.0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (
        UniqueConstraint("chain_pk", "token_address", "bucket_seconds",
                         "bucket_start", name="uq_feature_bucket"),
        Index("ix_feat_token_time", "token_address", "bucket_start"),
    )


class Signal(Base):
    """Explainable 0-100 on-chain signal. Score = evidence intensity at
    creation time only; it is NOT a prediction of future price movement."""
    __tablename__ = "signals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chain_pk: Mapped[int] = mapped_column(ForeignKey("chains.id"), index=True)
    token_address: Mapped[str] = mapped_column(String(64), index=True)
    token_symbol: Mapped[str | None] = mapped_column(String(64))
    signal_type: Mapped[str] = mapped_column(String(40), index=True)
    score: Mapped[float] = mapped_column(Numeric(6, 2), index=True)
    confidence: Mapped[float] = mapped_column(Numeric(6, 2), default=0)
    quality: Mapped[float] = mapped_column(Numeric(6, 2), default=0)
    risk_score: Mapped[float] = mapped_column(Numeric(6, 2), default=0, index=True)
    positive_score: Mapped[float] = mapped_column(Numeric(6, 2), default=0)
    negative_score: Mapped[float] = mapped_column(Numeric(6, 2), default=0)
    accumulation_score: Mapped[float | None] = mapped_column(Numeric(6, 2))
    distribution_score: Mapped[float | None] = mapped_column(Numeric(6, 2))
    capital_inflow_score: Mapped[float | None] = mapped_column(Numeric(6, 2))
    capital_outflow_score: Mapped[float | None] = mapped_column(Numeric(6, 2))
    onchain_momentum_score: Mapped[float | None] = mapped_column(Numeric(6, 2))
    confirmation_count: Mapped[int] = mapped_column(Integer, default=0)
    confirmed_features: Mapped[str | None] = mapped_column(String(1024))
    contradicting_features: Mapped[str | None] = mapped_column(String(1024))
    band: Mapped[str | None] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(16), default="NEW", index=True)
    explanation: Mapped[str | None] = mapped_column(String(4096))
    reasons_json: Mapped[str | None] = mapped_column(String(4096))  # machine-readable
    alert_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    last_alert_at: Mapped[datetime | None] = mapped_column(DateTime)
    # ---- no-look-ahead integrity fields (mandatory) ----
    signal_timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)
    feature_timestamp: Mapped[datetime | None] = mapped_column(DateTime)
    data_timestamp: Mapped[datetime | None] = mapped_column(DateTime)
    signal_engine_version: Mapped[str] = mapped_column(String(16), default="4.0.0")
    feature_version: Mapped[str] = mapped_column(String(16), default="1.0.0")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow,
                                                 onupdate=utcnow)
    __table_args__ = (
        Index("ix_signal_token_type_ts", "token_address", "signal_type",
              "signal_timestamp"),
        Index("ix_signal_status_ts", "status", "signal_timestamp"),
        Index("ix_signal_score_ts", "score", "signal_timestamp"),
    )


class SignalFeature(Base):
    """Every numeric input that produced a signal, with its contribution.
    Critical for debugging, backtesting, ML and explainability."""
    __tablename__ = "signal_features"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id"), index=True)
    feature_name: Mapped[str] = mapped_column(String(64), index=True)
    feature_value: Mapped[float | None] = mapped_column(Numeric(30, 8))
    feature_normalized: Mapped[float | None] = mapped_column(Numeric(12, 6))
    feature_contribution: Mapped[float | None] = mapped_column(Numeric(10, 4))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (
        UniqueConstraint("signal_id", "feature_name", name="uq_signal_feature"),
        Index("ix_sigfeat_name", "feature_name", "signal_id"),
    )


class AlertLog(Base):
    """Deduplicated alert history (cooldown enforced by the alert engine)."""
    __tablename__ = "alert_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), index=True)
    channel: Mapped[str] = mapped_column(String(32), default="log")
    alert_type: Mapped[str] = mapped_column(String(40))
    message: Mapped[str] = mapped_column(String(4096))
    dedup_key: Mapped[str] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


# ===========================================================================
# PHASE 5 — backtesting / historical outcome tables
# ===========================================================================

class Backtest(Base):
    """Reproducible backtest run record: full configuration + versions."""
    __tablename__ = "backtests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str | None] = mapped_column(String(128))
    configuration: Mapped[str] = mapped_column(String(8192))  # JSON filters
    signal_engine_version: Mapped[str | None] = mapped_column(String(16))
    feature_version: Mapped[str | None] = mapped_column(String(16))
    backtest_version: Mapped[str | None] = mapped_column(String(16))
    start_date: Mapped[datetime | None] = mapped_column(DateTime)
    end_date: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    progress: Mapped[float] = mapped_column(Numeric(5, 2), default=0)
    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)


class HistoricalOutcome(Base):
    """Outcome of one signal over one horizon. Computed strictly AFTER the
    horizon elapsed using only post-signal market data; the original signal
    row is never modified. INCOMPLETE rows carry NULL metrics — missing data
    is never silently treated as zero."""
    __tablename__ = "historical_outcomes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id"), index=True)
    token_id: Mapped[int | None] = mapped_column(ForeignKey("tokens.id"), index=True)
    chain_pk: Mapped[int] = mapped_column(ForeignKey("chains.id"), index=True)
    token_address: Mapped[str] = mapped_column(String(64), index=True)
    signal_timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)
    signal_type: Mapped[str] = mapped_column(String(40), index=True)
    signal_score: Mapped[float | None] = mapped_column(Numeric(6, 2))
    direction: Mapped[str] = mapped_column(String(8), default="FLAT")  # UP/DOWN/FLAT
    horizon: Mapped[str] = mapped_column(String(8), index=True)
    horizon_seconds: Mapped[int] = mapped_column(Integer)
    price_at_signal: Mapped[float | None] = mapped_column(Numeric(30, 18))
    price_after: Mapped[float | None] = mapped_column(Numeric(30, 18))
    return_percent: Mapped[float | None] = mapped_column(Numeric(20, 6))
    mfe_percent: Mapped[float | None] = mapped_column(Numeric(20, 6))
    mae_percent: Mapped[float | None] = mapped_column(Numeric(20, 6))
    mfe_directional: Mapped[float | None] = mapped_column(Numeric(20, 6))
    mae_directional: Mapped[float | None] = mapped_column(Numeric(20, 6))
    max_drawdown_percent: Mapped[float | None] = mapped_column(Numeric(20, 6))
    drawdown_before_target: Mapped[float | None] = mapped_column(Numeric(20, 6))
    time_to_5pct: Mapped[int | None] = mapped_column(Integer)
    time_to_10pct: Mapped[int | None] = mapped_column(Integer)
    time_to_20pct: Mapped[int | None] = mapped_column(Integer)
    time_to_minus_5pct: Mapped[int | None] = mapped_column(Integer)
    time_to_minus_10pct: Mapped[int | None] = mapped_column(Integer)
    outcome_class: Mapped[str | None] = mapped_column(String(24), index=True)
    hit_5pct: Mapped[bool | None] = mapped_column(Boolean)
    hit_10pct: Mapped[bool | None] = mapped_column(Boolean)
    hit_20pct: Mapped[bool | None] = mapped_column(Boolean)
    volume_change: Mapped[float | None] = mapped_column(Numeric(30, 2))
    liquidity_change: Mapped[float | None] = mapped_column(Numeric(30, 2))
    category: Mapped[str | None] = mapped_column(String(32))
    liquidity_usd: Mapped[float | None] = mapped_column(Numeric(30, 2))
    whale_participation: Mapped[float | None] = mapped_column(Numeric(12, 6))
    flow_anomaly_score: Mapped[float | None] = mapped_column(Numeric(6, 2))
    price_data_completeness: Mapped[float] = mapped_column(Numeric(5, 2), default=0)
    signal_data_completeness: Mapped[float] = mapped_column(Numeric(5, 2), default=0)
    liquidity_data_completeness: Mapped[float] = mapped_column(Numeric(5, 2), default=0)
    data_status: Mapped[str] = mapped_column(String(16), default="OK", index=True)
    # chronological split label (time-series safe, assigned at compute time)
    split: Mapped[str | None] = mapped_column(String(8))
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (
        UniqueConstraint("signal_id", "horizon", name="uq_outcome_horizon"),
        Index("ix_outcome_ts_horizon", "signal_timestamp", "horizon"),
        Index("ix_outcome_type_horizon", "signal_type", "horizon"),
        Index("ix_outcome_chain_horizon", "chain_pk", "horizon"),
    )


class BacktestResult(Base):
    """Aggregated statistics for one (backtest, horizon, group) cell."""
    __tablename__ = "backtest_results"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    backtest_id: Mapped[int] = mapped_column(ForeignKey("backtests.id"), index=True)
    horizon: Mapped[str] = mapped_column(String(8), index=True)
    group_kind: Mapped[str] = mapped_column(String(24), default="ALL")
    group_value: Mapped[str] = mapped_column(String(64), default="ALL")
    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    mean_return: Mapped[float | None] = mapped_column(Numeric(20, 6))
    median_return: Mapped[float | None] = mapped_column(Numeric(20, 6))
    std_return: Mapped[float | None] = mapped_column(Numeric(20, 6))
    min_return: Mapped[float | None] = mapped_column(Numeric(20, 6))
    max_return: Mapped[float | None] = mapped_column(Numeric(20, 6))
    p25: Mapped[float | None] = mapped_column(Numeric(20, 6))
    p75: Mapped[float | None] = mapped_column(Numeric(20, 6))
    p90: Mapped[float | None] = mapped_column(Numeric(20, 6))
    mean_mfe: Mapped[float | None] = mapped_column(Numeric(20, 6))
    mean_mae: Mapped[float | None] = mapped_column(Numeric(20, 6))
    mean_drawdown: Mapped[float | None] = mapped_column(Numeric(20, 6))
    hit_rates: Mapped[str | None] = mapped_column(String(512))  # JSON {target: rate}
    positive_rate: Mapped[float | None] = mapped_column(Numeric(8, 4))
    negative_rate: Mapped[float | None] = mapped_column(Numeric(8, 4))
    neutral_rate: Mapped[float | None] = mapped_column(Numeric(8, 4))
    sharpe_like: Mapped[float | None] = mapped_column(Numeric(12, 4))
    bootstrap: Mapped[str | None] = mapped_column(String(512))  # JSON CIs
    histogram: Mapped[str | None] = mapped_column(String(2048))  # JSON bins
    avg_time_to_5pct: Mapped[float | None] = mapped_column(Numeric(20, 2))
    avg_time_to_10pct: Mapped[float | None] = mapped_column(Numeric(20, 2))
    incomplete_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (
        UniqueConstraint("backtest_id", "horizon", "group_kind", "group_value",
                         name="uq_bt_cell"),
    )


# ===========================================================================
# PHASE 6 — ML, probability calibration & walk-forward learning tables
# ===========================================================================

class MLModel(Base):
    """Registered model version. Every prediction references its model_id so
    historical results remain reproducible. `is_production` follows the
    explicitly configured promotion rule (visible, not hidden)."""
    __tablename__ = "ml_models"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(96), unique=True)   # e.g. LR_HIT10_24H_V001
    model_type: Mapped[str] = mapped_column(String(32))          # logistic|random_forest|...
    target: Mapped[str] = mapped_column(String(24), index=True)  # hit_10pct | outcome_class
    horizon: Mapped[str] = mapped_column(String(8), index=True)
    feature_version: Mapped[str] = mapped_column(String(16))
    signal_engine_version: Mapped[str | None] = mapped_column(String(16))
    ml_version: Mapped[str | None] = mapped_column(String(16))
    training_start: Mapped[datetime | None] = mapped_column(DateTime)
    training_end: Mapped[datetime | None] = mapped_column(DateTime)
    validation_start: Mapped[datetime | None] = mapped_column(DateTime)
    validation_end: Mapped[datetime | None] = mapped_column(DateTime)
    test_start: Mapped[datetime | None] = mapped_column(DateTime)
    test_end: Mapped[datetime | None] = mapped_column(DateTime)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    class_distribution: Mapped[str | None] = mapped_column(String(128))  # JSON
    metrics: Mapped[str | None] = mapped_column(String(2048))            # JSON test metrics
    calibration_method: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(24), default="READY", index=True)
    # READY | INSUFFICIENT_DATA | FAILED | ARCHIVED
    is_production: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    artifact_path: Mapped[str | None] = mapped_column(String(256))
    config_json: Mapped[str | None] = mapped_column(String(4096))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class MLPrediction(Base):
    """Immutable prediction record. Never overwritten; keeps raw vs
    calibrated probability separate plus all versions for reproducibility."""
    __tablename__ = "ml_predictions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), index=True)
    token_id: Mapped[int | None] = mapped_column(ForeignKey("tokens.id"), index=True)
    chain_pk: Mapped[int] = mapped_column(ForeignKey("chains.id"), index=True)
    token_address: Mapped[str] = mapped_column(String(64), index=True)
    model_id: Mapped[int] = mapped_column(ForeignKey("ml_models.id"), index=True)
    model_name: Mapped[str | None] = mapped_column(String(96))
    target: Mapped[str] = mapped_column(String(24), index=True)
    horizon: Mapped[str] = mapped_column(String(8), index=True)
    raw_probability: Mapped[float | None] = mapped_column(Numeric(8, 6))
    calibrated_probability: Mapped[float | None] = mapped_column(Numeric(8, 6))
    data_quality: Mapped[float | None] = mapped_column(Numeric(6, 2))
    prediction_status: Mapped[str] = mapped_column(String(24), default="OK")
    # OK | LIMITED_DATA | NEW_TOKEN | INSUFFICIENT_HISTORY | MODEL_UNAVAILABLE
    feature_vector: Mapped[str | None] = mapped_column(String(8192))  # exact inputs used
    feature_timestamp: Mapped[datetime | None] = mapped_column(DateTime)
    feature_version: Mapped[str | None] = mapped_column(String(16))
    model_version: Mapped[str | None] = mapped_column(String(96))
    prediction_timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)
    actual_outcome_id: Mapped[int | None] = mapped_column(
        ForeignKey("historical_outcomes.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (
        UniqueConstraint("signal_id", "model_id", name="uq_pred_signal_model"),
        Index("ix_pred_token_ts", "token_address", "prediction_timestamp"),
    )


class MLTrainingRun(Base):
    """Audit log of every training attempt (including failures/insufficient)."""
    __tablename__ = "ml_training_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_id: Mapped[int | None] = mapped_column(ForeignKey("ml_models.id"), index=True)
    target: Mapped[str] = mapped_column(String(24), index=True)
    horizon: Mapped[str] = mapped_column(String(8))
    model_type: Mapped[str | None] = mapped_column(String(32))
    configuration: Mapped[str] = mapped_column(String(4096))     # full JSON config
    dataset_period: Mapped[str | None] = mapped_column(String(64))
    sample_counts: Mapped[str | None] = mapped_column(String(256))  # train/val/test sizes
    status: Mapped[str] = mapped_column(String(24), default="RUNNING", index=True)
    # RUNNING | COMPLETED | INSUFFICIENT_DATA | FAILED
    error: Mapped[str | None] = mapped_column(String(512))
    duration_seconds: Mapped[float | None] = mapped_column(Numeric(12, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)


class MLMetric(Base):
    """Per-model, per-split evaluation metrics (historical statistics)."""
    __tablename__ = "ml_metrics"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_id: Mapped[int] = mapped_column(ForeignKey("ml_models.id"), index=True)
    split: Mapped[str] = mapped_column(String(8))     # TRAIN|VALIDATION|TEST|WALK_FORWARD
    metric_name: Mapped[str] = mapped_column(String(32), index=True)
    metric_value: Mapped[float | None] = mapped_column(Numeric(14, 6))
    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (
        UniqueConstraint("model_id", "split", "metric_name", name="uq_metric"),
    )


class MLCalibrationBin(Base):
    """Reliability bins from VALIDATION data only: predicted prob band vs
    observed historical frequency. Descriptive, never a future guarantee."""
    __tablename__ = "ml_calibration_bins"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_id: Mapped[int] = mapped_column(ForeignKey("ml_models.id"), index=True)
    bin_index: Mapped[int] = mapped_column(Integer)
    bin_lo: Mapped[float] = mapped_column(Numeric(5, 4))
    bin_hi: Mapped[float] = mapped_column(Numeric(5, 4))
    predicted_probability: Mapped[float | None] = mapped_column(Numeric(8, 6))
    actual_outcome_rate: Mapped[float | None] = mapped_column(Numeric(8, 6))
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("model_id", "bin_index", name="uq_calib_bin"),)


class MLFeatureImportance(Base):
    __tablename__ = "ml_feature_importances"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_id: Mapped[int] = mapped_column(ForeignKey("ml_models.id"), index=True)
    feature: Mapped[str] = mapped_column(String(64), index=True)
    importance: Mapped[float | None] = mapped_column(Numeric(12, 8))
    direction: Mapped[str | None] = mapped_column(String(8))   # positive|negative|n/a
    feature_version: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("model_id", "feature", name="uq_feat_imp"),)


class MLDriftRecord(Base):
    __tablename__ = "ml_drift"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_id: Mapped[int] = mapped_column(ForeignKey("ml_models.id"), index=True)
    drift_kind: Mapped[str] = mapped_column(String(16), index=True)
    # FEATURE | PREDICTION | PERFORMANCE
    feature: Mapped[str | None] = mapped_column(String(64))
    psi: Mapped[float | None] = mapped_column(Numeric(10, 4))
    reference_window: Mapped[str | None] = mapped_column(String(64))
    recent_window: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), index=True)
    # NORMAL | WATCH | DRIFT_DETECTED | PERFORMANCE_DEGRADED
    detail: Mapped[str | None] = mapped_column(String(1024))
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class MLWalkForwardFold(Base):
    """One fold of a walk-forward run: train window -> unseen eval window."""
    __tablename__ = "ml_walkforward_folds"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("ml_training_runs.id"), index=True)
    fold_index: Mapped[int] = mapped_column(Integer)
    train_start: Mapped[datetime] = mapped_column(DateTime)
    train_end: Mapped[datetime] = mapped_column(DateTime)
    test_start: Mapped[datetime] = mapped_column(DateTime)
    test_end: Mapped[datetime] = mapped_column(DateTime)
    train_samples: Mapped[int] = mapped_column(Integer, default=0)
    test_samples: Mapped[int] = mapped_column(Integer, default=0)
    metrics: Mapped[str | None] = mapped_column(String(1024))   # JSON
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("run_id", "fold_index", name="uq_fold"),)
