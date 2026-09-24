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
    """A configurable DEX protocol on one chain (extendable to any DEX).

    kind: "v2"   -> Uniswap/Pancake V2-style AMM pools (Swap event emitted by
                    the pair itself; sender/receiver are indexed topics).
          "v3"   -> concentrated-liquidity pools (Swap event emitted by the
                    pool, sender is a non-indexed data field — needs tx input
                    or transfer analysis to attribute the user).
    routers: known router/aggregator addresses — transfers between these and
             pools are internal hops, never counted as user trades.
    """
    name: str
    factory_address: str | None = None
    kind: str = "v2"
    pair_created_topic: str = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"  # PairCreated(...)
    swap_topic: str | None = None  # e.g. Uniswap V3 Swap topic
    watch_swaps: bool = True
    routers: tuple[str, ...] = ()


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
V2_SWAP_TOPIC = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
UNI_V3_SWAP = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"

CHAINS: dict[str, ChainConfig] = {
    "ethereum": ChainConfig(
        key="ethereum",
        name="Ethereum",
        chain_id=1,
        rpc_urls=_csv(os.getenv("ETH_RPC_URL")) or [
            "https://ethereum-rpc.publicnode.com",
            "https://eth.llamarpc.com",
            "https://rpc.flashbots.net",
            "https://eth.drpc.org",
            "https://1rpc.io/eth",
        ],
        symbol="ETH",
        coingecko_id="ethereum",
        dexes=(
            DexConfig("uniswap_v2", "0x5C697E2FAD1A2C385761b00E755858aB166B25D4",
                      routers=("0x7a250d5630b4cf539739df2c5dacb4c659f2488d",)),
            DexConfig("sushiswap", "0xC0AEe478e3658e26D6cc5592ADF967EC22FA27F0",
                      routers=("0xd9e1ce17f2641f24aE83637464490A67C297Dc3",)),
            DexConfig("uniswap_v3", "0x1F98431c8aD98523631AE4a59f267346ea31F984",
                      kind="v3", swap_topic=UNI_V3_SWAP,
                      routers=("0x68b3465833fb72a70ecdef46cd56a25da5507a6e",)),
        ),
        start_block=18_000_000,
    ),
    "bsc": ChainConfig(
        key="bsc",
        name="BNB Smart Chain",
        chain_id=56,
        rpc_urls=_csv(os.getenv("BSC_RPC_URL")) or [
            "https://bsc-rpc.publicnode.com",
            "https://bsc-dataseed.binance.org",
            "https://bsc.drpc.org",
            "https://1rpc.io/bnb",
        ],
        symbol="BNB",
        coingecko_id="binancecoin",
        dexes=(
            DexConfig("pancakeswap_v2", "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73",
                      routers=("0x10ed43c715d61eb8b5d0e0acc3c10bbdb12e97ee",)),
            DexConfig("pancakeswap_v3", "0x5c0a42ec595A0EBf64CCA4ff167540C91a50e188",
                      kind="v3", swap_topic=UNI_V3_SWAP,
                      routers=("0x1b81dfde770232ffcfe18f130f9bb72ab4cf0002",)),
        ),
        start_block=35_000_000,
    ),
    # ---- Phase 7 expansion: additional EVM chains (all generic adapters) ----
    "polygon": ChainConfig(
        key="polygon", name="Polygon PoS", chain_id=137,
        rpc_urls=_csv(os.getenv("POLYGON_RPC_URL")) or [
            "https://polygon-bor-rpc.publicnode.com",
            "https://polygon-rpc.com",
            "https://polygon.drpc.org",
            "https://1rpc.io/matic",
        ],
        symbol="MATIC", coingecko_id="matic-network",
        dexes=(
            DexConfig("quickswap_v2", "0xa03be7D54B45F7aF5573CAC90780a7009CE4ef7e",
                      routers=("0xf06f6d847f95ebaaf338ebe16c62eb11111f780b",)),
            DexConfig("sushiswap_polygon", "0xc350adb7a5cf5bf39336605c427b0cb1b06f3d65",
                      routers=("0x1dca3cf4f9ea292e01e63fbc1a7e52eccbafedff",)),
        ),
        start_block=9_000_000,
    ),
    "arbitrum": ChainConfig(
        key="arbitrum", name="Arbitrum One", chain_id=42161,
        rpc_urls=_csv(os.getenv("ARBITRUM_RPC_URL")) or [
            "https://arbitrum-one-rpc.publicnode.com",
            "https://arb1.arbitrum.io/rpc",
            "https://arbitrum.drpc.org",
            "https://1rpc.io/arb",
        ],
        symbol="ETH", coingecko_id="ethereum",
        dexes=(
            DexConfig("uniswap_v3_arb", "0x1F98431c8aD98523631AE4a59f267346ea31F984",
                      kind="v3", swap_topic=UNI_V3_SWAP,
                      routers=("0x68b3465833fb72a70ecdef46cd56a25da5507a6e",)),
            DexConfig("camelot_v2", "0x6Ec0939FeBB7FE4Dce48223AfB64d64F15173AF9",
                      pair_created_topic="0x383064bb0600dced5a62e189a69eeaa09b7dad02a10d6b6e4e2b3b12128f0bfc",
                      routers=("0x0278bfd2389c63ccf731afdccb47fbb61a7d917a",)),
        ),
        start_block=170_000_000,
    ),
    "base": ChainConfig(
        key="base", name="Base", chain_id=8453,
        rpc_urls=_csv(os.getenv("BASE_RPC_URL")) or [
            "https://base-rpc.publicnode.com",
            "https://mainnet.base.org",
            "https://base.drpc.org",
            "https://1rpc.io/base",
        ],
        symbol="ETH", coingecko_id="ethereum",
        dexes=(
            DexConfig("uniswap_v3_base", "0x33128a8fC178698547A91E0726fC13F461e16c79",
                      kind="v3", swap_topic=UNI_V3_SWAP),
            DexConfig("aerodrome_v2", "0x420DD381b31aEf66836bfE69a821e2ad7c0d59b0",
                      swap_topic="0x1ce176c79e8c23abc7ec77e37ade2dd1c3d304813ec35bbe2f56ed04b72043db"),
        ),
        start_block=28_000_000,
    ),
    "avalanche": ChainConfig(
        key="avalanche", name="Avalanche C-Chain", chain_id=43114,
        rpc_urls=_csv(os.getenv("AVAX_RPC_URL")) or [
            "https://avalanche-c-chain-rpc.publicnode.com",
            "https://api.avax.network/ext/bc/C/rpc",
            "https://avalanche.drpc.org",
            "https://1rpc.io/avax/c",
        ],
        symbol="AVAX", coingecko_id="avalanche-2",
        dexes=(
            DexConfig("traderjoe_v2", "0x43588D554a82F65764875f5813F1071D2FF20616",
                      pair_created_topic="0x10f27ee44aab5209253cb397e4b180973961ad39e98512dc8104d92582b4858c",
                      routers=("0x6e2a7ae06f473d9c6951703d0610f580f2d37af5",)),
            DexConfig("pangolin_v2", "0xefa94DE7a4656D78766cD740f4964Fc7BcA8a3de"),
        ),
        start_block=1_000_000_000,
    ),
    "optimism": ChainConfig(
        key="optimism", name="OP Mainnet", chain_id=10,
        rpc_urls=_csv(os.getenv("OPTIMISM_RPC_URL")) or [
            "https://optimism-rpc.publicnode.com",
            "https://mainnet.optimism.io",
            "https://optimism.drpc.org",
            "https://1rpc.io/op",
        ],
        symbol="ETH", coingecko_id="ethereum",
        dexes=(
            DexConfig("velodrome_v2", "0xa062aE8A9c5e3a381282904DF6B34AA2E6A9A2E3",
                      swap_topic="0x1ce176c79e8c23abc7ec77e37ade2dd1c3d304813ec35bbe2f56ed04b72043db"),
            DexConfig("uniswap_v3_op", "0x1F98431c8aD98523631AE4a59f267346ea31F984",
                      kind="v3", swap_topic=UNI_V3_SWAP),
        ),
        start_block=125_000_000,
    ),
}

# Native / wrapped-native symbols per chain — used by the classifier so a
# "USDT"-style check never mislabels e.g. WBNB on BSC. Chain-agnostic set.
NATIVE_SYMBOLS: set[str] = {"weth", "eth", "wbtc", "bnb", "wbnb", "wmatic",
                            "matic", "pol", "avax", "wavax", "wtmatic"}

# Known event signature topics (Phase 3 additions)
V2_MINT_TOPIC = "0x4c209b5fc8ad50758f13e2e1088ba56a560dff690a1c6fef26394f4c03821c4f"   # Mint(sender, amount0, amount1)
V2_BURN_TOPIC = "0xdccd412f0b1252819cb1fd330b93224ca42612892bb3f4f789976e6d81936496"   # Burn(sender, amount0, amount1, to)


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
        "polygon": float(os.getenv("WHALE_FLOOR_POLYGON_USD", "5000")),
        "arbitrum": float(os.getenv("WHALE_FLOOR_ARBITRUM_USD", "20000")),
        "base": float(os.getenv("WHALE_FLOOR_BASE_USD", "10000")),
        "avalanche": float(os.getenv("WHALE_FLOOR_AVAX_USD", "10000")),
        "optimism": float(os.getenv("WHALE_FLOOR_OPTIMISM_USD", "10000")),
    })
    exchange_label_source: str = os.getenv("EXCHANGE_LABEL_SOURCE", "local")
    dex_label_source: str = os.getenv("DEX_LABEL_SOURCE", "local")
    # behavior windows
    behavior_window_days: int = int(os.getenv("BEHAVIOR_WINDOW_DAYS", "7"))
    rapid_movement_minutes: int = int(os.getenv("RAPID_MOVEMENT_MINUTES", "30"))
    common_funder_min_wallets: int = int(os.getenv("COMMON_FUNDER_MIN_WALLETS", "3"))


wallet_settings = WalletSettings()


# ---------------------------------------------------------------------------
# Phase 3 — DEX / capital-flow configuration (all env-overridable)
# ---------------------------------------------------------------------------

@dataclass
class DexSettings:
    """Thresholds & buckets for swap classification, flow aggregation,
    market-impact events and data retention. Nothing here is hardcoded in
    the engines themselves."""
    # quote assets recognised for BUY_LIKE / SELL_LIKE classification
    # (verified contract addresses live in app/market/prices.py STABLES;
    #  symbols are only a fallback for mock/dev tokens)
    quote_symbols: tuple = ("USDT", "USDC", "DAI", "BUSD", "USDE", "FDUSD",
                            "USDS", "TUSD", "USDD", "FRAX", "LUSD")
    native_quote_symbols: tuple = ("WETH", "WBNB", "ETH", "BNB", "WMATIC",
                                   "POL", "WTM", "AVAX", "WAVAX")
    # large buy/sell event floors (USD) — combined with relative metrics
    large_trade_min_usd: float = float(os.getenv("DEX_LARGE_TRADE_MIN_USD", "10000"))
    whale_trade_min_usd: float = float(os.getenv("DEX_WHALE_TRADE_MIN_USD", "50000"))
    high_impact_liquidity_ratio: float = float(os.getenv("DEX_HIGH_IMPACT_RATIO", "0.05"))
    # liquidity removal considered "high priority" (% of pool removed)
    liquidity_event_min_usd: float = float(os.getenv("LIQ_EVENT_MIN_USD", "5000"))
    liquidity_shift_pct_alert: float = float(os.getenv("LIQ_SHIFT_PCT_ALERT", "20"))
    # flow snapshot buckets (seconds)
    flow_buckets: tuple = (60, 300, 900, 3600, 14400, 86400)
    flow_aggregate_interval: float = float(os.getenv("FLOW_AGGREGATE_INTERVAL", "30"))
    # flow anomaly scoring
    flow_anomaly_min_volume_usd: float = float(os.getenv("FLOW_ANOMALY_MIN_USD", "5000"))
    baseline_windows: int = int(os.getenv("FLOW_BASELINE_WINDOWS", "12"))
    cross_dex_window_seconds: int = int(os.getenv("CROSS_DEX_WINDOW_SECONDS", "180"))
    # router/aggregator detection: tx input-data selectors that indicate an
    # exact-in swap routed through pools (first 4 bytes of calldata)
    exact_in_selectors: tuple = ("0x7ff36ab5", "0x18cbafe5", "0xa2e74af6",
                                 "0xb85c50bd", "0x04e45aaf", "0x49404b77")
    # retention (days); 0 disables pruning. Core tx/transfer history is never
    # auto-deleted unless explicitly configured > 0.
    raw_swap_retention_days: int = int(os.getenv("RAW_SWAP_RETENTION_DAYS", "0"))
    flow_snapshot_retention_days: int = int(os.getenv("FLOW_SNAPSHOT_RETENTION_DAYS", "30"))
    liquidity_snapshot_retention_days: int = int(os.getenv("LIQUIDITY_SNAPSHOT_RETENTION_DAYS", "30"))
    impact_event_retention_days: int = int(os.getenv("IMPACT_EVENT_RETENTION_DAYS", "90"))
    prune_interval_seconds: float = float(os.getenv("PRUNE_INTERVAL_SECONDS", "21600"))


dex_settings = DexSettings()


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
    # CSV of chain keys to run ("" = all configured chains), e.g.
    # ENABLE_CHAINS=ethereum,bsc,polygon
    enabled_chains: str = os.getenv("ENABLE_CHAINS", "")


settings = Settings()


# ---------------------------------------------------------------------------
# Phase 4 — signal engine configuration (all env-overridable)
# ---------------------------------------------------------------------------

@dataclass
class SignalSettings:
    """Weights & thresholds for the explainable 0-100 signal model.
    Scores describe observed on-chain evidence intensity only — they are
    never probabilities of future price movement."""
    # component weights (kept out of business logic; sum normalised at use)
    flow_weight: float = float(os.getenv("SIGNAL_W_FLOW", "0.30"))
    whale_weight: float = float(os.getenv("SIGNAL_W_WHALE", "0.25"))
    liquidity_weight: float = float(os.getenv("SIGNAL_W_LIQUIDITY", "0.15"))
    anomaly_weight: float = float(os.getenv("SIGNAL_W_ANOMALY", "0.15"))
    wallet_weight: float = float(os.getenv("SIGNAL_W_WALLET", "0.10"))
    market_weight: float = float(os.getenv("SIGNAL_W_MARKET", "0.05"))
    risk_weight: float = float(os.getenv("SIGNAL_W_RISK", "0.25"))  # penalty scale
    # evaluation cadence + freshness guard (no look-ahead: features must be
    # strictly older than the horizon being evaluated)
    eval_interval_seconds: float = float(os.getenv("SIGNAL_EVAL_INTERVAL", "60"))
    feature_grace_seconds: int = int(os.getenv("SIGNAL_FEATURE_GRACE", "180"))
    # lifecycle / dedup
    cooldown_seconds: int = int(os.getenv("SIGNAL_COOLDOWN_SECONDS", "1800"))
    active_ttl_seconds: int = int(os.getenv("SIGNAL_ACTIVE_TTL", "7200"))
    update_threshold: float = float(os.getenv("SIGNAL_UPDATE_THRESHOLD", "5"))
    min_score_alert: float = float(os.getenv("SIGNAL_MIN_ALERT_SCORE", "60"))
    # minimum absolute 1h flow before directional signals can fire (USD)
    min_flow_usd: float = float(os.getenv("SIGNAL_MIN_FLOW_USD", "1000"))
    # reversal detection: prior net-flow magnitude needed to call a flip
    reversal_min_prior_usd: float = float(os.getenv("SIGNAL_REVERSAL_MIN_PRIOR", "500"))
    # score bands (intensity labels only, NOT profit probabilities)
    bands: tuple = ((20, "VERY_LOW_ACTIVITY"), (40, "LOW"), (60, "MODERATE"),
                    (75, "ELEVATED"), (90, "HIGH"), (101, "EXTREME"))
    version: str = os.getenv("SIGNAL_ENGINE_VERSION", "4.0.0")
    feature_version: str = os.getenv("FEATURE_VERSION", "1.0.0")


signal_settings = SignalSettings()


# ---------------------------------------------------------------------------
# Phase 5 — backtesting / historical outcome configuration
# ---------------------------------------------------------------------------

@dataclass
class BacktestSettings:
    """Outcome horizons and classification thresholds. All statistics are
    DESCRIPTIVE HISTORICAL measurements — never guarantees or calibrated
    future probabilities."""
    # horizon name -> seconds (configurable; nothing hardcoded in engines)
    horizons: dict = field(default_factory=lambda: {
        "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400,
        "6h": 21600, "12h": 43200, "24h": 86400, "3d": 259200, "7d": 604800,
    })
    # outcome classification thresholds (percent returns), configurable
    strong_positive_pct: float = float(os.getenv("OUTCOME_STRONG_POS_PCT", "10"))
    positive_pct: float = float(os.getenv("OUTCOME_POS_PCT", "3"))
    negative_pct: float = float(os.getenv("OUTCOME_NEG_PCT", "-3"))
    strong_negative_pct: float = float(os.getenv("OUTCOME_STRONG_NEG_PCT", "-10"))
    # hit-rate targets used by dashboards/reports
    hit_targets: tuple = (5.0, 10.0, 20.0)
    # bootstrap confidence intervals
    bootstrap_enabled: bool = os.getenv("BOOTSTRAP_ENABLED", "true").lower() in ("1", "true", "yes")
    bootstrap_samples: int = int(os.getenv("BOOTSTRAP_SAMPLES", "1000"))
    bootstrap_seed: int = int(os.getenv("BOOTSTRAP_SEED", "42"))
    # chronological split ratios (train/validation/test) — time-series safe
    split_train: float = float(os.getenv("SPLIT_TRAIN", "0.6"))
    split_validation: float = float(os.getenv("SPLIT_VALIDATION", "0.2"))
    # processing
    batch_size: int = int(os.getenv("BACKTEST_BATCH_SIZE", "500"))
    max_rows_per_query: int = int(os.getenv("BACKTEST_MAX_ROWS", "20000"))
    worker_concurrency: int = int(os.getenv("BACKTEST_CONCURRENCY", "2"))
    # liquidity buckets for group analysis (USD upper bounds)
    liquidity_buckets: tuple = (1e5, 5e5, 1e6, 1e7, 1e8)
    version: str = os.getenv("BACKTEST_VERSION", "5.0.0")


backtest_settings = BacktestSettings()


# ---------------------------------------------------------------------------
# Phase 6 — ML, probability calibration & walk-forward learning
# ---------------------------------------------------------------------------
@dataclass
class MLSettings:
    """CPU-friendly ML layer. Calibrated probabilities are HISTORICAL model
    estimates conditioned on similar past observations — they are NOT
    guaranteed future outcomes and must never be presented as such."""
    enabled: bool = os.getenv("ML_ENABLED", "true").lower() in ("1", "true", "yes")
    # minimum-sample policy: never train/fabricate on tiny datasets
    min_train_samples: int = int(os.getenv("MIN_TRAIN_SAMPLES", "120"))
    min_validation_samples: int = int(os.getenv("MIN_VALIDATION_SAMPLES", "40"))
    min_test_samples: int = int(os.getenv("MIN_TEST_SAMPLES", "40"))
    # default prediction target / horizon (configurable per training run)
    default_target: str = os.getenv("ML_DEFAULT_TARGET", "hit_10pct")
    default_horizon: str = os.getenv("ML_DEFAULT_HORIZON", "24h")
    # direction convention for binary targets
    positive_targets: tuple = ("hit_5pct", "hit_10pct", "hit_20pct")
    negative_targets: tuple = ("hit_minus_5pct", "hit_minus_10pct")
    # model zoo (CPU only; optional libs detected at runtime)
    baseline_model: str = "logistic"
    candidate_models: tuple = ("logistic", "random_forest", "hist_gradient_boosting")
    xgboost_enabled: bool = os.getenv("XGBOOST_ENABLED", "false").lower() in ("1", "true", "yes")
    lightgbm_enabled: bool = os.getenv("LIGHTGBM_ENABLED", "false").lower() in ("1", "true", "yes")
    # imbalance handling (no time-leaking oversampling)
    class_weight_balanced: bool = True
    # calibration: sigmoid|isotonic|none — fit ONLY on validation data
    calibration_method: str = os.getenv("CALIBRATION_METHOD", "sigmoid")
    calibration_min_samples: int = int(os.getenv("CALIBRATION_MIN_SAMPLES", "40"))
    reliability_bins: int = 10
    # chronological split ratios (train/val/test) — time-series safe
    split_train: float = float(os.getenv("ML_SPLIT_TRAIN", "0.7"))
    split_validation: float = float(os.getenv("ML_SPLIT_VAL", "0.15"))
    # walk-forward defaults
    wf_train_days: int = int(os.getenv("WF_TRAIN_DAYS", "14"))
    wf_test_days: int = int(os.getenv("WF_TEST_DAYS", "3"))
    wf_step_days: int = int(os.getenv("WF_STEP_DAYS", "3"))
    wf_min_train: int = int(os.getenv("WF_MIN_TRAIN", "80"))
    # live prediction gating
    min_feature_completeness: float = float(os.getenv("ML_MIN_COMPLETENESS", "35"))
    max_feature_age_seconds: int = int(os.getenv("ML_MAX_FEATURE_AGE", "7200"))
    # production selection rule (explicit + visible, not hidden):
    # choose highest test ROC-AUC among candidates that pass
    # min samples + Brier <= threshold. Set "" to disable auto-promotion.
    promotion_rule: str = os.getenv("ML_PROMOTION_RULE", "max_roc_auc")
    max_brier_for_promotion: float = float(os.getenv("ML_MAX_BRIER", "0.25"))
    # drift monitoring thresholds (PSI-style buckets)
    drift_watch_psi: float = float(os.getenv("DRIFT_WATCH_PSI", "0.10"))
    drift_detected_psi: float = float(os.getenv("DRIFT_DETECTED_PSI", "0.25"))
    drift_perf_brier_delta: float = float(os.getenv("DRIFT_PERF_BRIER", "0.05"))
    recent_window_hours: int = int(os.getenv("DRIFT_RECENT_HOURS", "72"))
    # artifact storage
    models_dir: str = os.getenv("MODELS_DIR", str(BASE_DIR / "data" / "models"))
    # periodic retraining cadence (hours); 0 disables background scheduler
    retrain_interval_hours: float = float(os.getenv("ML_RETRAIN_INTERVAL_HOURS", "0"))
    version: str = os.getenv("ML_VERSION", "6.0.0")


ml_settings = MLSettings()


# Phase 3 — ERC-4626 vault share events (Deposit/Withdraw with 3 indexed
# topics) used by the observation-derived pool detector.
ERC4626_DEPOSIT_TOPIC = "0xdcbc1c05240f31ff3ad067ef1ee35ce4997762752e3a095284754544f4c709d7"
ERC4626_WITHDRAW_TOPIC = "0xf341246adaac6f497bc2a656f546ab9e182111d630394f0c57c710a59a2cb567"
