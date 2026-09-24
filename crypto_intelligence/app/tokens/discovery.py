"""Event/activity-driven token discovery.

A token becomes a monitored asset when seen in relevant chain activity:
ERC-20 Transfer logs, DEX PairCreated events, swap-related contracts...
No manual registration, no hardcoded list; unknown tokens are kept anyway.
DB access helpers run in threads (sync SQLAlchemy session).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.database.models import Chain, Token, TokenPair
from app.tokens.classifier import classify_token

log = logging.getLogger("discovery")


def _upsert(session: Session, stmt_model, values: dict, conflict_cols: list[str],
            update_cols: dict):
    """Cross-dialect INSERT ... ON CONFLICT DO UPDATE."""
    dialect = session.bind.dialect.name if session.bind else "sqlite"
    ins = (pg_insert if dialect == "postgresql" else sqlite_insert)(stmt_model)
    ins = ins.values(**values)
    ins = ins.on_conflict_do_update(index_elements=conflict_cols, set_=update_cols)
    session.execute(ins)


class TokenDiscoveryService:
    def __init__(self, session_factory):
        self.session_factory = session_factory
        self._known: dict[int, set[str]] = {}   # chain_pk -> known addresses (RAM cache)
        self._pairs: dict[int, set[str]] = {}   # chain_pk -> known pair addresses

    def chain_pk(self, chain_key: str) -> int | None:
        with self.session_factory() as s:
            return s.query(Chain.id).filter_by(key=chain_key).scalar()

    def is_known_token(self, chain_pk: int, address: str) -> bool:
        addr = address.lower()
        cached = self._known.get(chain_pk)
        if cached is not None and addr in cached:
            return True
        with self.session_factory() as s:
            exists = s.query(Token.id).filter_by(chain_pk=chain_pk, address=addr).first()
        if exists:
            self._known.setdefault(chain_pk, set()).add(addr)
            return True
        return False

    def register_token(self, chain_pk: int, address: str, source: str,
                       meta: dict | None = None, block_number: int | None = None,
                       ts: datetime | None = None) -> bool:
        """Insert or update a discovered token. Returns True if newly added."""
        address = address.lower()
        ts = ts or datetime.now(timezone.utc).replace(tzinfo=None)
        meta = meta or {}
        sym = meta.get("symbol")
        nm = meta.get("name")
        dec = meta.get("decimals")
        # check DB directly (authoritative "was it ever registered" answer)
        with self.session_factory() as s:
            existing = s.query(Token).filter_by(chain_pk=chain_pk, address=address).first()
        new = existing is None
        category = classify_token(sym, nm, discovery_source=source, is_new=new)
        with self.session_factory() as s:
            if existing:
                existing.last_seen = ts
                if not existing.has_metadata and (sym or nm or dec is not None):
                    existing.symbol = sym or existing.symbol
                    existing.name = nm or existing.name
                    existing.decimals = dec if dec is not None else existing.decimals
                    existing.has_metadata = bool(sym or nm or dec is not None)
                    existing.category = classify_token(existing.symbol, existing.name,
                                                       existing.discovery_source)
                if existing.first_transfer_block is None and block_number:
                    existing.first_transfer_block = block_number
                s.commit()
                return False
            vals = dict(
                chain_pk=chain_pk, address=address, symbol=sym, name=nm, decimals=dec,
                category=category, first_seen=ts, last_seen=ts, active=True,
                discovery_source=source, transfer_count=0,
                has_metadata=bool(sym or nm or dec is not None),
                first_transfer_block=block_number,
            )
            _upsert(s, Token, vals, ["chain_pk", "address"],
                    {"last_seen": ts})
            s.commit()
        self._known.setdefault(chain_pk, set()).add(address)
        log.info("discovered token %s (%s) via %s", address[:12], sym or "no-symbol", source)
        return True

    def bump_transfer_count(self, chain_pk: int, address: str, n: int = 1) -> None:
        with self.session_factory() as s:
            t = s.query(Token).filter_by(chain_pk=chain_pk, address=address.lower()).first()
            if t:
                t.transfer_count = (t.transfer_count or 0) + n
                t.last_seen = datetime.now(timezone.utc).replace(tzinfo=None)
                s.commit()

    def register_pair(self, chain_pk: int, pair_address: str, dex_name: str,
                      token0: str, token1: str, block_number: int | None = None,
                      ts: datetime | None = None) -> None:
        """Record a DEX pair and register both side tokens as discovered."""
        pair_address, token0, token1 = pair_address.lower(), token0.lower(), token1.lower()
        ts = ts or datetime.now(timezone.utc).replace(tzinfo=None)
        with self.session_factory() as s:
            vals = dict(chain_pk=chain_pk, pair_address=pair_address, dex_name=dex_name,
                        token0=token0, token1=token1, first_seen=ts, last_seen=ts)
            _upsert(s, TokenPair, vals, ["chain_pk", "pair_address"], {"last_seen": ts})
            s.commit()
        self._pairs.setdefault(chain_pk, set()).add(pair_address)
        for tok in (token0, token1):
            self.register_token(chain_pk, tok, source="pair_created",
                                block_number=block_number, ts=ts)
        log.info("discovered pair %s [%s] %s/%s", pair_address[:12], dex_name,
                 token0[:8], token1[:8])

    def known_pairs(self, chain_pk: int) -> set[str]:
        if chain_pk not in self._pairs:
            with self.session_factory() as s:
                rows = s.query(TokenPair.pair_address).filter_by(chain_pk=chain_pk).all()
            self._pairs[chain_pk] = {r[0] for r in rows}
        return self._pairs[chain_pk]

    def update_price(self, chain_pk: int, address: str, price: float | None,
                     source: str | None, liquidity_usd: float | None = None) -> None:
        if price is None:
            return
        with self.session_factory() as s:
            t = s.query(Token).filter_by(chain_pk=chain_pk, address=address.lower()).first()
            if t:
                t.price_usd = price
                t.price_timestamp = datetime.now(timezone.utc).replace(tzinfo=None)
                t.price_source = source
                if liquidity_usd is not None:
                    t.liquidity_usd = liquidity_usd
                s.commit()
