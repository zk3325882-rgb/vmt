"""Central configuration for the Crypto Intelligence Scanner (Phase 1).

All secrets come from environment variables / .env — never hardcoded.
Chain-specific data (RPCs, DEX factories, native coin ids) lives here so the
scanner core stays chain-agnostic (generic EVM adapter + per-chain config).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _csv(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


@dataclass(frozen=True)
class DexConfig:
    """A configurable DEX protocol on one chain (extendable to any DEX)."""
    name: str
    factory_address: str | None = None
    pair_created_topic: str = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"  # PairCreated(...)
    swap_topic: str | None = None  # e.g. Uniswap V3 Swap topic
    watch_swaps: bool = False


@dataclass(frozen=True)
class ChainConfig:
    key: str                     # short id, e.g. "ethereum"
    name: str                    # display name
    chain_id: int                # numeric EIP-155 chain id
    rpc_urls: list[str]
    symbol: str                  # native asset symbol
    coingecko_id: str            # for native price lookup
    dexes: tuple[DexConfig, ...] = ()
    start_block: int = 0         # earliest block to consider on first run


# Known event signature topics
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a5df525a44c"
V2_SWAP_TOPIC = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d13084064b2401"
UNI_V3_SWAP = "0xc42079f94a6350b7e6235f3545a6eb4a61a28c9d0b44346d733d84012db6a600"

CHAINS: dict[str, ChainConfig] = {
    "ethereum": ChainConfig(
        key="ethereum",
        name="Ethereum",
        chain_id=1,
        rpc_urls=_csv(os.getenv("ETH_RPC_URL")) or [
            "https://eth.llamarpc.com",
            "https://ethereum-rpc.publicnode.com",
        ],
        symbol="ETH",
        coingecko_id="ethereum",
        dexes=(
            DexConfig("uniswap_v2", "0x5C697E2FAD1A2C385761b00E755858aB166B25D4"),
            DexConfig("sushiswap", "0xC0AEe478e3658e26D6cc5592ADF967EC22FA27F0"),
            DexConfig("uniswap_v3", "0x1F98431c8aD98523631AE4a59f267346ea31F984",
                      swap_topic=UNI_V3_SWAP, watch_swaps=True),
        ),
        start_block=18_000_000,
    ),
    "bsc": ChainConfig(
        key="bsc",
        name="BNB Smart Chain",
        chain_id=56,
        rpc_urls=_csv(os.getenv("BSC_RPC_URL")) or [
            "https://bsc-dataseed.binance.org",
            "https://bsc-rpc.publicnode.com",
        ],
        symbol="BNB",
        coingecko_id="binancecoin",
        dexes=(
            DexConfig("pancakeswap_v2", "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73"),
            DexConfig("pancakeswap_v3", "0x5c0a42ec595A0EBf64CCA4ff167540C91a50e188",
                      swap_topic=UNI_V3_SWAP, watch_swaps=True),
        ),
        start_block=35_000_000,
    ),
}


# ---------------------------------------------------------------------------
# Phase 2 — wallet / whale intelligence configuration (all env-overridable)
# ---------------------------------------------------------------------------

# Seed address labels are stored in data/address_labels.json (loaded into the
# address_labels table at init_db time) — never hardcoded in Python source.
LABELS_FILE = BASE_DIR / "data" / "address_labels.json"


@dataclass
class WalletSettings:
    """Thresholds for classification, whale scoring, behavior & clustering."""
    whale_min_score: float = float(os.getenv("WHALE_MIN_SCORE", "60"))
    wallet_history_days: int = int(os.getenv("WALLET_HISTORY_DAYS", "30"))
    wallet_snapshot_interval: int = int(os.getenv("WALLET_SNAPSHOT_INTERVAL", "900"))
    cluster_min_score: float = float(os.getenv("CLUSTER_MIN_SCORE", "55"))
    max_wallet_analysis_per_block: int = int(
        os.getenv("MAX_WALLET_ANALYSIS_PER_BLOCK", "100"))
    # tiering thresholds (portfolio USD)
    tier1_min_usd: float = float(os.getenv("TIER1_MIN_USD", "500000"))
    tier2_min_usd: float = float(os.getenv("TIER2_MIN_USD", "20000"))
    tier2_min_txs: int = int(os.getenv("TIER2_MIN_TXS", "10"))
    # whale score component weights (absolute / relative / liquidity / activity)
    w_absolute: float = float(os.getenv("WHALE_W_ABSOLUTE", "0.30"))
    w_relative: float = float(os.getenv("WHALE_W_RELATIVE", "0.30"))
    w_liquidity: float = float(os.getenv("WHALE_W_LIQUIDITY", "0.20"))
    w_activity: float = float(os.getenv("WHALE_W_ACTIVITY", "0.20"))
    # dynamic large-transfer floor per chain (USD); scaled further by token
    # liquidity so $100k on a deep pool stays quiet but screams on a thin one
    whale_floor_usd: dict = field(default_factory=lambda: {
        "ethereum": float(os.getenv("WHALE_FLOOR_ETH_USD", "50000")),
        "bsc": float(os.getenv("WHALE_FLOOR_BSC_USD", "10000")),
    })
    exchange_label_source: str = os.getenv("EXCHANGE_LABEL_SOURCE", "local")
    dex_label_source: str = os.getenv("DEX_LABEL_SOURCE", "local")
    # behavior windows
    behavior_window_days: int = int(os.getenv("BEHAVIOR_WINDOW_DAYS", "7"))
    rapid_movement_minutes: int = int(os.getenv("RAPID_MOVEMENT_MINUTES", "30"))
    common_funder_min_wallets: int = int(os.getenv("COMMON_FUNDER_MIN_WALLETS", "3"))


wallet_settings = WalletSettings()


@dataclass
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "") or f"sqlite:///{BASE_DIR / 'data' / 'scanner.db'}"
    mock_mode: bool = os.getenv("MOCK_MODE", "false").lower() in ("1", "true", "yes")
    start_lookback: int = int(os.getenv("START_BLOCK_LOOKBACK", "30"))
    scan_interval: float = float(os.getenv("SCAN_INTERVAL_SECONDS", "3"))
    db_batch_size: int = int(os.getenv("DB_BATCH_SIZE", "200"))
    poll_log_chunk: int = int(os.getenv("POLL_LOG_CHUNK", "2000"))
    min_usd_alert: float = float(os.getenv("MIN_USD_ALERT", "10000"))
    min_anomaly_score: float = float(os.getenv("MIN_ANOMALY_SCORE", "40"))
    price_api_key: str = os.getenv("PRICE_API_KEY", "")
    coingecko_url: str = os.getenv("COINGECKO_API_URL", "https://api.coingecko.com/api/v3")
    api_host: str = os.getenv("API_HOST", "127.0.0.1")
    api_port: int = int(os.getenv("API_PORT", "8000"))
    rpc_timeout: float = float(os.getenv("RPC_TIMEOUT", "15"))
    max_retries: int = int(os.getenv("RPC_MAX_RETRIES", "6"))
    chains: dict[str, ChainConfig] = field(default_factory=lambda: CHAINS)


settings = Settings()
