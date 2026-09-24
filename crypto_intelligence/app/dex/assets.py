"""Phase 3 — asset-role helpers: quote/stable detection, native/wrapped
handling and safe buy/sell-ratio math.

Design rules:
* Prefer VERIFIED contract addresses (config-driven STABLES / WRAPPED_NATIVE)
  over symbol strings; symbols are only a fallback for tokens whose address
  is not in the configured set (e.g. dev/mock assets).
* A wrap/unwrap (native <-> wrapped) is NEVER classified as a market trade.
* All ratio math must survive zero/near-zero denominators.
"""
from __future__ import annotations

from app.market.prices import STABLES, WRAPPED_NATIVE
from config import dex_settings as DS


def verified_stable_price(chain_key: str, token_address: str | None) -> float | None:
    """USD price for a *configured* stablecoin address, else None."""
    if not token_address:
        return None
    return STABLES.get(chain_key, {}).get(token_address.lower())


def is_quote_asset(chain_key: str, token_address: str | None,
                   symbol: str | None) -> bool:
    """Quote asset = accepted side of a market pair: a stablecoin or the
    (wrapped) native asset. Address verification wins; symbol is fallback."""
    if not token_address:
        return False
    addr = token_address.lower()
    if addr in STABLES.get(chain_key, {}):
        return True
    if WRAPPED_NATIVE.get(chain_key, "").lower() == addr:
        return True
    sym = (symbol or "").upper().strip()
    if sym in DS.quote_symbols or sym in DS.native_quote_symbols:
        return True
    # mock/dev stable convention (MUSD etc.)
    if sym.startswith("USD") and len(sym) <= 6:
        return True
    return False


def is_native_or_wrapped(chain_key: str, token_address: str | None,
                         symbol: str | None) -> bool:
    if token_address and token_address.lower() == WRAPPED_NATIVE.get(
            chain_key, "").lower():
        return True
    sym = (symbol or "").upper().strip()
    cfg = None
    from config import CHAINS
    cfg = CHAINS.get(chain_key)
    if cfg and sym == cfg.symbol.upper():
        return True
    return False


def is_wrap_unwrap(token_in_addr: str | None, token_out_addr: str | None,
                   chain_key: str) -> bool:
    """True when both sides represent the same underlying asset
    (e.g. ETH<->WETH, BNB<->WBNB). Such swaps are NOT market buys/sells."""
    if not token_in_addr or not token_out_addr:
        return False
    a, b = token_in_addr.lower(), token_out_addr.lower()
    if a == b:
        return True
    wnat = WRAPPED_NATIVE.get(chain_key, "").lower()
    # one side wrapped native, other side native sentinel ("0x...00" style)
    if wnat and (a == wnat or b == wnat) and (a == "0x" + "0" * 40 or
                                              b == "0x" + "0" * 40):
        return True
    return False


# ---------------------------------------------------------------------------
# BUY / SELL classification (contextual labels — never claims of intention)
# ---------------------------------------------------------------------------

def classify_swap_sides(chain_key: str,
                        token_in: str | None, symbol_in: str | None,
                        token_out: str | None, symbol_out: str | None) -> str:
    """Classify a wallet-side swap:

      wallet gives QUOTE, receives TOKEN   -> BUY_LIKE
      wallet gives TOKEN, receives QUOTE   -> SELL_LIKE
      both sides non-quote                 -> SWAP_OTHER
      ambiguous / unknown decimals etc.    -> UNKNOWN_SWAP

    Wrap/unwrap pairs must be filtered out by the caller BEFORE this runs.
    """
    if not token_in and not token_out:
        return "UNKNOWN_SWAP"
    q_in = is_quote_asset(chain_key, token_in, symbol_in)
    q_out = is_quote_asset(chain_key, token_out, symbol_out)
    if q_in and not q_out:
        return "BUY_LIKE"
    if q_out and not q_in:
        return "SELL_LIKE"
    if q_in and q_out:
        return "SWAP_OTHER"          # stable->stable / ETH->wBTC-style quote pair
    if token_in and token_out:
        return "SWAP_OTHER"          # token->token, neither is a quote asset
    return "UNKNOWN_SWAP"


# ---------------------------------------------------------------------------
# safe flow math
# ---------------------------------------------------------------------------

def buy_sell_ratio(buy_usd: float, sell_usd: float) -> float | None:
    """Mathematically safe ratio. One-sided flows saturate at 999.0 instead
    of dividing by zero; both zero -> None (no information)."""
    b, s = max(buy_usd or 0.0, 0.0), max(sell_usd or 0.0, 0.0)
    if b <= 0 and s <= 0:
        return None
    if s <= 0:
        return 999.0
    if b <= 0:
        return 0.0
    return round(min(b / s, 999.0), 4)


def buy_sell_imbalance(buy_usd: float, sell_usd: float) -> float | None:
    """(buy - sell) / (buy + sell) in [-1, +1]; None when no volume."""
    b, s = max(buy_usd or 0.0, 0.0), max(sell_usd or 0.0, 0.0)
    tot = b + s
    if tot <= 0:
        return None
    return round((b - s) / tot, 6)


def safe_acceleration(current: float, previous: float,
                      floor: float = 1000.0) -> float | None:
    """Flow acceleration % with a USD noise floor in the denominator so tiny
    baselines never produce meaningless infinite percentages."""
    c, p = current or 0.0, previous or 0.0
    if abs(p) < floor:
        if abs(c) < floor:
            return None
        return None  # baseline too small to compute a meaningful percentage
    return round((c - p) / abs(p) * 100.0, 2)
