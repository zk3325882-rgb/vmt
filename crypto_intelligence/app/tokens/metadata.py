"""ERC-20 metadata retrieval via eth_call (symbol/name/decimals).

Missing/malformed metadata never kills the pipeline: the token is still
stored, keyed by its contract address. Results cached in memory to save RPC.
"""
from __future__ import annotations

import logging

from app.blockchain.base import BaseChainAdapter
from app.blockchain.evm import is_valid_address

log = logging.getLogger("metadata")

SEL_NAME = "0x06fdde03"
SEL_SYMBOL = "0x95d89b41"
SEL_DECIMALS = "0x313ce567"


def _decode_string(hexdata: str | None) -> str | None:
    """Decode an ABI-encoded string; fall back to raw bytes interpretation."""
    if not hexdata or hexdata == "0x" or len(hexdata) < 130:
        return None
    try:
        body = bytes.fromhex(hexdata[2:])
        # standard: offset(32) length(32) data
        slen = int.from_bytes(body[32:64], "big")
        raw = body[64:64 + slen]
        txt = raw.decode("utf-8", "ignore").strip("\x00 ").strip()
        if txt and len(txt) <= 128 and txt.isprintable():
            return txt
        # ERC-721 style non-standard (length-prefixed inline)
        alt = body[:64].decode("utf-8", "ignore").strip("\x00 ")
        return alt[:128] if alt else None
    except Exception:
        return None


async def fetch_metadata(adapter: BaseChainAdapter, address: str) -> dict:
    """Return {symbol, name, decimals}; any field may be None."""
    out = {"symbol": None, "name": None, "decimals": None}
    if not is_valid_address(address):
        return out
    sym = await adapter.eth_call(address, SEL_SYMBOL)
    out["symbol"] = _decode_string(sym)
    nm = await adapter.eth_call(address, SEL_NAME)
    out["name"] = _decode_string(nm)
    dec = await adapter.eth_call(address, SEL_DECIMALS)
    if dec and dec != "0x":
        try:
            d = int(dec, 16)
            if 0 <= d <= 36:
                out["decimals"] = d
        except ValueError:
            pass
    if out["symbol"] and out["decimals"] is None:
        out["decimals"] = 18  # only assumed when contract responded to symbol()
    return out
