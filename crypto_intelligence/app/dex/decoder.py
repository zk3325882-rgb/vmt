"""Phase 3 — DEX event decoder + transaction grouping.

Decodes Uniswap/Pancake V2-style Swap/Mint/Burn logs (generic "v2 kind":
the pair itself emits the event; sender/receiver are indexed topics) and
V3-style pool Swap logs (amounts are signed; sender is non-indexed).

Transaction grouping rule (critical): a single tx can contain many Transfer
+ Swap + pool-interaction logs. User trades are attributed from DECODED SWAP
EVENTS ONLY — internal ERC-20 transfer hops (router -> pool -> user, or
multi-hop A -> B -> C inside one aggregator tx) are never counted as separate
buys/sells. Router/aggregator detection uses configured router addresses plus
best-effort tx-input selectors; when ambiguous we still record the swap but
flag it via `is_user_trade` heuristics rather than inventing data.

Amount attribution for V2 pools:
  amount0In/amount1In > 0 side = tokens the wallet gave the pool (token_in)
  amount0Out/amount1Out > 0 side = tokens the pool sent out (token_out)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from app.blockchain.base import LogEntry
from config import TRANSFER_TOPIC, V2_BURN_TOPIC, V2_MINT_TOPIC, V2_SWAP_TOPIC

log = logging.getLogger("dex.decoder")


def topic_addr(topic: str) -> str:
    return "0x" + topic[-40:].lower()


def u256(data: str, word: int) -> int | None:
    body = data[2:] if data.startswith("0x") else data
    chunk = body[word * 64:(word + 1) * 64]
    if len(chunk) < 64:
        return None
    try:
        return int(chunk, 16)
    except ValueError:
        return None


def i256(data: str, word: int) -> int | None:
    v = u256(data, word)
    if v is None:
        return None
    return v - (1 << 256) if v >= (1 << 255) else v


@dataclass
class DecodedSwap:
    """Normalized user-level swap derived from ONE Swap event."""
    tx_hash: str
    log_index: int
    block_number: int
    pool_address: str
    dex_name: str
    kind: str                       # "v2" | "v3"
    wallet_address: str             # attributed user (sender/receiver best guess)
    token_in: str | None            # token wallet gave (pool side: amountXIn)
    token_out: str | None           # token wallet received
    amount_in_raw: int | None
    amount_out_raw: int | None
    is_user_trade: bool = True      # False => router-internal hop candidate
    notes: str = ""


@dataclass
class DecodedLiquidity:
    tx_hash: str
    log_index: int
    block_number: int
    pool_address: str
    dex_name: str
    wallet_address: str
    event_type: str                 # LIQUIDITY_ADD | LIQUIDITY_REMOVE
    amount0_raw: int | None
    amount1_raw: int | None


@dataclass
class TxGroup:
    """All relevant logs of one transaction, ordered by log_index."""
    tx_hash: str
    block_number: int
    transfer_logs: list[LogEntry] = field(default_factory=list)
    swap_events: list[DecodedSwap] = field(default_factory=list)
    liquidity_events: list[DecodedLiquidity] = field(default_factory=list)


def decode_v2_swap(entry: LogEntry, pool_tokens: tuple[str, str],
                   dex_name: str) -> DecodedSwap | None:
    """Uniswap/Pancake V2 Swap(sender, amount0In, amount1In, amount0Out,
    amount1Out, to) — sender & to are indexed topics."""
    try:
        if len(entry.topics) < 3:
            return None
        a0in = u256(entry.data, 0)
        a1in = u256(entry.data, 1)
        a0out = u256(entry.data, 2)
        a1out = u256(entry.data, 3)
        if None in (a0in, a1in, a0out, a1out):
            return None
        t0, t1 = pool_tokens
        sender = topic_addr(entry.topics[1])
        receiver = topic_addr(entry.topics[2]) if len(entry.topics) > 2 else sender
        token_in = token_out = None
        amt_in = amt_out = None
        if a0in > 0 and a1out > 0:          # gave token0, got token1
            token_in, token_out = t0, t1
            amt_in, amt_out = a0in, a1out
        elif a1in > 0 and a0out > 0:        # gave token1, got token0
            token_in, token_out = t1, t0
            amt_in, amt_out = a1in, a0out
        elif a0in > 0 and a1in > 0:         # both sides in: no clean output leg
            token_in, token_out = None, None
        # wallet attribution: prefer receiver (user) over sender (often router)
        wallet = receiver if receiver != "0x" + "0" * 40 else sender
        return DecodedSwap(
            tx_hash=entry.transaction_hash, log_index=entry.log_index,
            block_number=entry.block_number, pool_address=entry.address.lower(),
            dex_name=dex_name, kind="v2", wallet_address=wallet,
            token_in=token_in, token_out=token_out,
            amount_in_raw=amt_in, amount_out_raw=amt_out,
            notes="" if (token_in or token_out) else "ambiguous swap legs")
    except Exception as e:
        log.debug("decode_v2_swap failed %s/%s: %s",
                  entry.transaction_hash[:12], entry.log_index, e)
        return None


def decode_v3_swap(entry: LogEntry, pool_tokens: tuple[str, str],
                   dex_name: str) -> DecodedSwap | None:
    """Uniswap V3 Swap(sender, recipient, amount0, amount1, sqrtPriceX96,
    liquidity, tick): positive amounts = pool RECEIVED (wallet gave),
    negative = pool SENT (wallet received). sender is NOT indexed (topic[1]
    is recipient)."""
    try:
        amount0 = i256(entry.data, 0)
        amount1 = i256(entry.data, 1)
        if amount0 is None or amount1 is None:
            return None
        t0, t1 = pool_tokens
        recipient = topic_addr(entry.topics[1]) if len(entry.topics) > 1 else None
        # data layout: amount0, amount1, sender(address), recipient(address)
        d_sender = ("0x" + entry.data[2:][64:128][-40:].lower()) \
            if len(entry.data[2:]) >= 128 else None
        wallet = recipient or d_sender or "0x" + "0" * 40
        token_in = token_out = None
        amt_in = amt_out = None
        if amount0 > 0 and amount1 < 0:
            token_in, token_out, amt_in, amt_out = t0, t1, amount0, -amount1
        elif amount1 > 0 and amount0 < 0:
            token_in, token_out, amt_in, amt_out = t1, t0, amount1, -amount0
        return DecodedSwap(
            tx_hash=entry.transaction_hash, log_index=entry.log_index,
            block_number=entry.block_number, pool_address=entry.address.lower(),
            dex_name=dex_name, kind="v3", wallet_address=wallet,
            token_in=token_in, token_out=token_out,
            amount_in_raw=amt_in, amount_out_raw=amt_out,
            is_user_trade=bool(recipient),
            notes="v3 pool event; sender needs receipt/input context")
    except Exception as e:
        log.debug("decode_v3_swap failed: %s", e)
        return None


def decode_v2_liquidity(entry: LogEntry, dex_name: str) -> DecodedLiquidity | None:
    """Mint(sender, amount0, amount1) / Burn(sender, amount0, amount1, to)."""
    try:
        topic = entry.topics[0].lower()
        if topic == V2_MINT_TOPIC:
            etype = "LIQUIDITY_ADD"
        elif topic == V2_BURN_TOPIC:
            etype = "LIQUIDITY_REMOVE"
        else:
            return None
        if len(entry.topics) < 2:
            return None
        sender = topic_addr(entry.topics[1])
        a0 = u256(entry.data, 0)
        a1 = u256(entry.data, 1)
        return DecodedLiquidity(
            tx_hash=entry.transaction_hash, log_index=entry.log_index,
            block_number=entry.block_number, pool_address=entry.address.lower(),
            dex_name=dex_name, wallet_address=sender, event_type=etype,
            amount0_raw=a0, amount1_raw=a1)
    except Exception as e:
        log.debug("decode_v2_liquidity failed: %s", e)
        return None


def group_transaction_logs(entries: list[LogEntry]) -> dict[str, TxGroup]:
    """Group raw logs per tx_hash, preserving log order. Transfer logs stay
    attached to their group so the engine can SUPPRESS counting internal
    transfer hops as independent trades."""
    groups: dict[str, TxGroup] = {}
    for e in sorted(entries, key=lambda x: (x.transaction_hash, x.log_index)):
        g = groups.setdefault(e.transaction_hash,
                              TxGroup(tx_hash=e.transaction_hash,
                                      block_number=e.block_number))
        if e.topics and e.topics[0].lower() == TRANSFER_TOPIC:
            g.transfer_logs.append(e)
    return groups
