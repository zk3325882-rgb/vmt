"""Token category classification from metadata (extensible, no scanning logic).

Heuristic rules; can be replaced/extended with better sources later.
Categories (30+): Layer 1, Layer 2, Meme, DeFi, Stablecoin, algo-Stable,
Gaming, GameFi, AI, RWA, NFT ecosystem, Metaverse, SocialFi, DePIN, DeSci,
Prediction Market, Privacy, Oracle, Liquid Staking (LST), Leveraged Token,
Index/Basket, Bridge, Exchange Token, Payment, Infrastructure, Storage,
DAO/Governance, Launchpad/VCA, Wrapped Asset, Utility, New Token, Unknown.
"""
from __future__ import annotations

import re


def _word_re(hints) -> re.Pattern:
    """Word-boundary matcher — avoids substring false positives such as
    'chain' matching 'ai', 'maintenance' matching 'ai', 'domain'/'main'
    matching 'in', etc."""
    joined = "|".join(sorted((h.lower() for h in hints), key=len, reverse=True))
    return re.compile(r"(?:^|[\s_\-/,()·:])(?:" + joined + r")(?:$|[\s_\-/,()·:])")


# ---------------------------------------------------------------------------
# Category hint tables (edit freely — classifier picks the best match below)
# ---------------------------------------------------------------------------
STABLE_SYMBOLS = {
    "usdt", "usdc", "dai", "busd", "fdusd", "tusd", "usdd", "usde", "gusd",
    "susd", "musd", "ustc", "frax", "lusd", "cusd", "usdx", "eusd", "ausd",
    "usdp", "usyld", "usdl", "eurc", "eurt", "usds", "dola", "usdf", "mim",
    "rai", "ohm", "algo", "byte", "esd", "OUSD", "crvusd", "gho", "pyusd",
    "usdglo", "rlusd", "usdn", "usdr", "xusd",
}
ALGO_STABLE_SYMBOLS = {"ust", "ustc", "rai", "ohm", "olymp", "esd", "time",
                       "bind", "bass", "carrot"}
L1_SYMBOLS = {"btc", "wbtc", "eth", "bnb", "avax", "matic", "pol", "ftm",
              "sol", "dot", "trx", "ada", "near", "apt", "xtz", "algo",
              "one", "flow", "hedera", "hbar", "kas", "sei", "tia", "inj"}
L2_HINTS = ("arbitrum", "optimism", "op mainnet", "polygon", "matic", "pol",
            "base network", "blast", "zksync", "starknet", "scroll", "linea",
            "mantle", "metis", "mode", "manta", "loopring", "immutable")
MEME_HINTS = ("dog", "shib", "elon", "pepe", "inu", "cat", "frog", "mem",
              "bonk", "wif", "floki", "baby", "kishu", "doge", "doji",
              "pump", "trump", "moo deng", "turbo", "BRETT", "popcat",
              "mog", "grock", "wog", "kishu", "cheems", "wojaki", "saylor")
DEFI_HINTS = ("uniswap", "aave", "sushi", "curve", "crv", "lending", "swap",
              "yield", "staking", "vault", "dydx", "compound", "maker",
              "pancake", "velodrome", "aerodrome", "camelot", "trader joe",
              "pangolin", "quickswap", "balancer", "synthetix", "yearn",
              "liquity", "eigenlayer", "eigen", "convex", "frax finance",
              "gmx", "jupiter", "raydium", "orca", "mercurial", "mars",
              "radiant", "venus", "kinza", "dolomite", "morpho", "spark")
GAMING_HINTS = ("game", "play to earn", "metaverse", "sandbox", "axie",
                "ilv", "ron", "pixels", "star atlas", "god unchained",
                "big time", "illuvium", "decentraland", "mana", "ygg",
                "guild", "quest", "arcade", "cartridge", "battle", "rpg")
AI_HINTS = ("ai", "gpt", "neural", "brain", "compute", "gpu", "sentient",
            "agent", "llm", "machine learning", "deep learning", "bittensor",
            "fetch", "singularity", "ocean protocol", "render", "virtuals",
            "griffain", "truth terminal", "swarm", "inference", "tensor")
RWA_HINTS = ("bond", "real estate", "gold", "tgold", "xaut", "paxg",
             "treasury", "commodity", "silver", "tokenized", "rwak",
             "on-chain equity", "private credit", "carbon", "offering")
NFT_HINTS = ("nft", "badger", "art", "gallery", "collection", "cryptopunk",
             "bored ape", "bayc", "mayc", "azuki", "pixelmon", "milady",
             "foundation", "super rare", "magic eden", "tensor", "blur")
SOCIAL_HINTS = ("friend", "farcaster", "lenster", "lens", "bluesky", "social",
                "chat", "post", "clout", "tribe", "discourse", "beacon")
DEPIN_HINTS = ("helium", "hnt", "filecoin", "iotex", "io", "theta", "wired",
               "geodnet", "roam", "pyxel", "depin", "hotspot", "mesh",
               "peaq", "nosana", "grass", "breez", "data wallet", "sensor")
DESCI_HINTS = ("science", "research", "lab", "gene", "bio", "medic",
               "dermat", "protein", "open science", "atom", "quantum chem")
PREDICTION_HINTS = ("prediction", "polymarket", "augur", "forecast", "market",
                    "oracle sports", "limitless", "opyn", "thales", "oxly")
PRIVACY_HINTS = ("privacy", "xmr", "secret", "scrt", "zk", "zero knowledge",
                 "tornado", "hush", "zen cash", "mobilecoin", "moc", "phala",
                 "oasis", "rose", "anonym")
ORACLE_HINTS = ("oracle", "chainlink", "link", "pyth", "api3", "band",
                "razor", "tellor", "traace", "redstone")
LST_HINTS = ("steth", "reth", "cbeth", "wsteth", "frxeth", "seth", "rocket",
             "liquid staking", "lst", "jito", "msol", "bnsol", "sst", "ezeth",
             "weeth", "oseeth", "rseth", "lseth", "static")
LEVERAGED_TOKENS = ("uponly", "downonly", "2l", "3l", "2s", "3s", "bull",
                    "bear", "leveraged", "perp")
INDEX_HINTS = ("index", "basket", "dpi", "ci", "hyfi", "diversified")
BRIDGE_HINTS = ("bridge", "multichain", "connext", "hop", "across", "layerzero",
                "lz", "wormhole", "gravity", "axelar", "stargate", "omni")
EXCHANGE_TOKENS = {"bnb", "okb", "htx", "gt", "cvc", "leo", "kcs", "df", "all",
                   "mx", "crypto org", "binance", "coinbase", "kraken"}
PAYMENT_HINTS = ("payment", "pay", "remittance", "send", "xlm", "nano",
                 "xno", "dash", "ltc", "litecoin", "bitcoin cash", "bch",
                 "vipnode", "request network")
INFRA_HINTS = ("infrastructure", "node", "validator", "rpc", "datacenter",
               "blocknative", "flashbots", "mev", "keeper", "automation",
               "chainlink functions", "subgraph", "the graph", "grt")
STORAGE_HINTS = ("storage", "file", "arweave", "permaweb", "storj", "siacoin",
                 "bzz", "swarm", "crust", "w3bstream", "w3b")
DAO_HINTS = ("dao", "governance", "voting", "snapshot", "tally", "boardroom")
LAUNCHPAD_HINTS = ("launchpad", "launch", "ido", "ico", "seedify", "poolside",
                   "fjord", "vca", "accelerator", "bootcamp", "incubator")
UTILITY_HINTS = ("utility", "token", "protocol", "network", "platform")

# Pre-compiled word-boundary regexes for ambiguous short hints
_AI_RE = _word_re(AI_HINTS)
_ZK_RE = _word_re(("zk", "zero knowledge", "zkevm"))
_LST_RE = _word_re(LST_HINTS)
_MEME_WORD_RE = _word_re(("mem", "mog", "io", "in", "cat", "dog"))
_PREDICTION_RE = _word_re(PREDICTION_HINTS)
_PAYMENT_RE = _word_re(PAYMENT_HINTS)
_STORAGE_RE = _word_re(STORAGE_HINTS)
_BRIDGE_RE = _word_re(BRIDGE_HINTS)
_INFRA_RE = _word_re(INFRA_HINTS)
_SOCIAL_RE = _word_re(SOCIAL_HINTS)
_DESCI_RE = _word_re(DESCI_HINTS)
_DEPIN_RE = _word_re(DEPIN_HINTS)
_PRIVACY_RE = _word_re(PRIVACY_HINTS)
_ORACLE_RE = _word_re(ORACLE_HINTS)
_DAO_RE = _word_re(DAO_HINTS)
_LAUNCH_RE = _word_re(LAUNCHPAD_HINTS)
_INDEX_RE = _word_re(INDEX_HINTS)
_LEV_RE = _word_re(LEVERAGED_TOKENS)


def _any_word(rx: re.Pattern, text: str) -> bool:
    return rx.search(text) is not None


def classify_token(symbol: str | None, name: str | None,
                   discovery_source: str | None = None,
                   is_new: bool = False) -> str:
    """Return the single best category label for a token's metadata."""
    s = (symbol or "").lower().strip()
    n = (name or "").lower()
    text = f"{s} {n}"

    # --- exact symbol tables first (highest confidence) ---
    if s in STABLE_SYMBOLS:
        return "Stablecoin"
    if s in ALGO_STABLE_SYMBOLS:
        return "Algo-Stable"
    if s in EXCHANGE_TOKENS:
        return "Exchange Token"
    if s.startswith("w") and s[1:] in L1_SYMBOLS:
        return "Wrapped Asset"
    if s in L1_SYMBOLS:
        return "Layer 1"

    # --- name-based stable / native checks ---
    if "stablecoin" in n or "us dollar" in n or "usd coin" in n:
        return "Stablecoin"
    if "wrapped" in n and ("ether" in n or "btc" in n or "bnb" in n
                           or "matic" in n or "avax" in n):
        return "Wrapped Asset"

    # --- liquid staking before generic staking/DeFi ---
    if _any_word(_LST_RE, text) or "liquid staking" in n:
        return "Liquid Staking"

    # --- leveraged/index tokens ---
    if _any_word(_LEV_RE, text) and ("long" in n or "short" in n
                                     or "3x" in text or "2x" in text):
        return "Leveraged Token"
    if _any_word(_INDEX_RE, text):
        return "Index/Basket"

    # --- meme (strong substrings + word-boundary short hints) ---
    if any(h in text for h in MEME_HINTS if len(h) > 3) \
            or _any_word(_MEME_WORD_RE, text):
        return "Meme"

    # --- AI ---
    if _any_word(_AI_RE, text):
        return "AI"

    # --- DeFi ---
    if any(h in text for h in DEFI_HINTS):
        return "DeFi"

    # --- prediction markets before generic market words ---
    if _any_word(_PREDICTION_RE, text) and "prediction" in text:
        return "Prediction Market"

    # --- privacy ---
    if _any_word(_PRIVACY_RE, text) or _any_word(_ZK_RE, text):
        return "Privacy"

    # --- oracle ---
    if _any_word(_ORACLE_RE, text):
        return "Oracle"

    # --- storage ---
    if _any_word(_STORAGE_RE, text):
        return "Storage"

    # --- DePIN ---
    if _any_word(_DEPIN_RE, text):
        return "DePIN"

    # --- infrastructure ---
    if _any_word(_INFRA_RE, text):
        return "Infrastructure"

    # --- bridge ---
    if _any_word(_BRIDGE_RE, text):
        return "Bridge"

    # --- socialfi ---
    if _any_word(_SOCIAL_RE, text):
        return "SocialFi"

    # --- descii ---
    if _any_word(_DESCI_RE, text):
        return "DeSci"

    # --- gaming / metaverse ---
    if any(h in text for h in GAMING_HINTS):
        return "GameFi" if "metaverse" in text else "Gaming"

    # --- RWA ---
    if any(h in text for h in RWA_HINTS):
        return "RWA"

    # --- NFT ecosystem ---
    if any(h in text for h in NFT_HINTS):
        return "NFT ecosystem"

    # --- DAO / governance ---
    if _any_word(_DAO_RE, text):
        return "DAO/Governance"

    # --- launchpad ---
    if _any_word(_LAUNCH_RE, text):
        return "Launchpad"

    # --- payment ---
    if _any_word(_PAYMENT_RE, text):
        return "Payment"

    # --- layer 2 ---
    if any(h in text for h in L2_HINTS):
        return "Layer 2"

    # --- exchange token by name ---
    if "exchange" in n or "binance" in n or "coinbase" in n:
        return "Exchange Token"

    # --- utility fallback ---
    if any(h in text for h in UTILITY_HINTS):
        return "Utility"

    if is_new and discovery_source in ("pair_created", "new_contract"):
        return "New Token"
    return "Unknown"


CATEGORIES = [
    "Layer 1", "Layer 2", "Meme", "DeFi", "Stablecoin", "Algo-Stable",
    "Gaming", "GameFi", "AI", "RWA", "NFT ecosystem", "Metaverse",
    "SocialFi", "DePIN", "DeSci", "Prediction Market", "Privacy", "Oracle",
    "Liquid Staking", "Leveraged Token", "Index/Basket", "Bridge",
    "Exchange Token", "Payment", "Infrastructure", "Storage",
    "DAO/Governance", "Launchpad", "Wrapped Asset", "Utility",
    "New Token", "Unknown",
]
