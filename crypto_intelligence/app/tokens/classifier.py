"""Token category classification from metadata (extensible, no scanning logic).

Phase 1 heuristic rules; Phase 2+ can replace/extend with better sources.
Categories: Layer 1, Layer 2, Meme, DeFi, Stablecoin, Gaming, AI, RWA,
NFT ecosystem, Utility, New Token, Unknown.
"""
from __future__ import annotations

STABLE_SYMBOLS = {
    "usdt", "usdc", "dai", "busd", "fdusd", "tusd", "usdd", "usde", "gusd",
    "susd", "musd", "ustc", "frax", "lusd", "cusd", "usdx", "eusd", "aUSD",
}
L1_SYMBOLS = {"weth", "wbtc", "bnb", "wtc"}
L2_HINTS = ("arb", "op_", "oov", "matic", "pol", "base", "blast")
MEME_HINTS = ("dog", "shib", "elon", "pepe", "inu", "cat", "frog", "mem",
              "bonk", "wif", "floki", "baby", "tron", "kishu")
DEFI_HINTS = ("uni", "aave", "sushi", "crv", "lend", "swap", "yield", "stake",
              "vault", "dydx", "compound", "maker", "pancake", "cake")
GAMING_HINTS = ("game", "play", "metaverse", "sandbox", "axie", "ilv", "ron")
AI_HINTS = ("ai", "gpt", "neural", "brain", "compute", "gpu", "sentient")
RWA_HINTS = ("bond", "real", "gold", "oil", "realestate", "treasury")
NFT_HINTS = ("nft", "badger", "art")


def classify_token(symbol: str | None, name: str | None,
                   discovery_source: str | None = None,
                   is_new: bool = False) -> str:
    s = (symbol or "").lower().strip()
    n = (name or "").lower()
    text = f"{s} {n}"
    if s in STABLE_SYMBOLS or "stablecoin" in n or "us dollar" in n:
        return "Stablecoin"
    if s in L1_SYMBOLS:
        return "Layer 1"
    if any(h in text for h in MEME_HINTS):
        return "Meme"
    if any(h in text for h in AI_HINTS) and ("ai " in text or text.startswith("ai") or "_ai" in text or "ai_" in text or n):
        return "AI"
    if any(h in text for h in DEFI_HINTS):
        return "DeFi"
    if any(h in text for h in L2_HINTS):
        return "Layer 2"
    if any(h in text for h in GAMING_HINTS):
        return "Gaming"
    if any(h in text for h in RWA_HINTS):
        return "RWA"
    if any(h in text for h in NFT_HINTS):
        return "NFT ecosystem"
    if is_new and discovery_source in ("pair_created", "new_contract"):
        return "New Token"
    return "Unknown"
