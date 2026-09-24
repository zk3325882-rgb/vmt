"""Large / unusual transaction detection and anomaly scoring.

Multi-metric (NOT a single fixed $ threshold):
  1. absolute USD value
  2. rolling normal transfer size per token (median of recent transfers)
  3. relative size = usd / normal_size   (e.g. $800k vs $20k normal = 40x)
  4. volume ratio (transfer vs rolling 24h token volume, when available)
  5. liquidity ratio (transfer vs pool liquidity, when available)
  6. percentile vs recent activity for the same token

Produces an ALERT PRIORITY SCORE 0-100 (not a price prediction).
Rolling stats are kept in bounded in-memory deques per token (RAM-safe);
authoritative history stays in PostgreSQL.
"""
from __future__ import annotations

import math
from bisect import insort
from collections import deque
from dataclasses import dataclass


@dataclass
class AnomalyResult:
    anomaly_score: float
    relative_size: float | None
    volume_ratio: float | None
    liquidity_ratio: float | None
    percentile: float | None


class RollingStats:
    """Bounded per-token window of recent transfer USD values + volumes."""
    WINDOW = 500

    def __init__(self):
        self._values: dict[str, deque] = {}
        self._sorted: dict[str, list] = {}

    def add(self, token_address: str, usd: float) -> None:
        key = token_address.lower()
        dq = self._values.setdefault(key, deque(maxlen=self.WINDOW))
        sl = self._sorted.setdefault(key, [])
        if len(dq) == dq.maxlen:
            old = dq[0]
            try:
                sl.remove(old)
            except ValueError:
                pass
        dq.append(usd)
        insort(sl, usd)

    def median(self, token_address: str) -> float | None:
        sl = self._sorted.get(token_address.lower())
        if not sl or len(sl) < 5:   # need a minimum sample for "normal"
            return None
        n = len(sl)
        return sl[n // 2] if n % 2 else (sl[n // 2 - 1] + sl[n // 2]) / 2

    def percentile(self, token_address: str, usd: float) -> float | None:
        sl = self._sorted.get(token_address.lower())
        if not sl or len(sl) < 5:
            return None
        below = sum(1 for v in sl if v <= usd)
        return round(100.0 * below / len(sl), 2)


def _score_from_ratio(ratio: float) -> float:
    """Map a multiple-of-normal ratio to 0..100 using log scaling.
    1x -> ~17, 5x -> ~50, 20x -> ~76, 100x -> ~100."""
    if ratio <= 0:
        return 0.0
    return min(100.0, max(0.0, (math.log10(1 + ratio) / math.log10(101)) * 100))


def compute_anomaly(token_address: str, usd_value: float | None,
                    stats: RollingStats,
                    volume_24h: float | None = None,
                    liquidity_usd: float | None = None) -> AnomalyResult:
    rel = vol_r = liq_r = pct = None
    parts: list[tuple[float, float]] = []  # (weight, sub-score)

    if usd_value and usd_value > 0:
        # 1) absolute magnitude component (log-scaled: $10k->~20 ... $10M->~80, $100M->100)
        abs_score = min(100.0, max(0.0, (math.log10(max(usd_value, 1)) - 3.5) / 4.5 * 100))
        parts.append((0.25, abs_score))

        # 2/3) relative size vs rolling normal
        med = stats.median(token_address)
        if med and med > 0:
            rel = usd_value / med
            parts.append((0.35, _score_from_ratio(rel)))

        # 4) share of 24h volume
        if volume_24h and volume_24h > 0:
            vol_r = usd_value / volume_24h
            parts.append((0.15, min(100.0, vol_r * 400)))  # 25% of daily volume -> 100

        # 5) liquidity impact
        if liquidity_usd and liquidity_usd > 0:
            liq_r = usd_value / liquidity_usd
            parts.append((0.15, min(100.0, liq_r * 200)))  # 50% of liquidity -> 100

        # 6) percentile vs recent activity
        pct = stats.percentile(token_address, usd_value)
        if pct is not None:
            parts.append((0.10, pct))

    if not parts:
        score = 0.0
    else:
        wsum = sum(w for w, _ in parts)
        score = sum(w * s for w, s in parts) / wsum
    return AnomalyResult(round(min(100.0, max(0.0, score)), 2),
                         None if rel is None else round(rel, 4),
                         None if vol_r is None else round(vol_r, 6),
                         None if liq_r is None else round(liq_r, 6),
                         pct)


# ---------------- flow & type classification (extensible) ----------------
ZERO_ADDR = "0x" + "0" * 40

# Known DEX router/pair-ish patterns; Phase 2 adds exchange wallet labels.
KNOWN_CONTRACT_HINTS = {
    "ethereum": {
        "0x7a250d5630b4cf539739df2c5dacb4c659f2488d": "Uniswap V2 Router",
        "0x33128a8edc20500410455b2aba02487012f12a55": "DEX",
        "0xdef1c0ded9bec7f1a1670819833240f7f3653e5c": "0x Router",
        "0x68b3465833fb72a70ecdef46cd56a25da5507a6e": "Coinbase",
        "0x28c6c06298d514db089934071355e5743bf21d60": "Binance",
        "0x47ac0fb4f2d8461e0d1df13b31a7dee01fdbe7b6": "MicroStrategy",
    },
    "bsc": {
        "0x10ed43c715d61eb8b5d0e0acc3c10bbdb12e97ee": "PancakeSwap Router",
        "0xbb2cf3b6a0df361bc4dc2b2e757bc15f5407ff9f": "Pancake Router V2",
        "0xecae5d275ed843bb6e43e4ae4fcb270dac81de18": "Binance Hot",
        "0xf977814e90da44bfa03b6295a0616a897441acec": "Binance 8",
    },
}


def classify_transfer(chain_key: str, from_addr: str, to_addr: str,
                      pair_addresses: set[str]) -> tuple[str, str]:
    """Return (transaction_type, flow).

    Conservative on purpose: wallet-to-wallet is TRANSFER, never BUY/SELL.
    Flow IN/OUT is defined relative to non-pool side vs pool/known-contract:
      - into a known DEX pair/router or labelled contract => OUT (leaving wallets toward venue)
      - out of such a venue to a plain wallet              => IN
      - mint (from zero) => MINT/IN ; burn (to zero) => BURN/OUT
      - plain wallet->wallet => TRANSFER with flow IN (receiver-centric default)
    """
    fl = from_addr.lower()
    tl = to_addr.lower()
    venues = pair_addresses | set(KNOWN_CONTRACT_HINTS.get(chain_key, {}))
    if fl == ZERO_ADDR:
        return ("MINT", "IN")
    if tl == ZERO_ADDR:
        return ("BURN", "OUT")
    if tl in venues or tl in pair_addresses:
        return ("DEX_ACTIVITY" if (tl in pair_addresses or tl in venues) else "TRANSFER", "OUT")
    if fl in venues or fl in pair_addresses:
        return ("DEX_ACTIVITY", "IN")
    return ("TRANSFER", "IN")
