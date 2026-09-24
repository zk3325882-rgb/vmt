"""Configurable address-label system (Phase 2).

Labels come from data/address_labels.json or any future public label export
(never hardcoded in Python source). They are imported into the
``address_labels`` table and cached per chain for fast classification.

Interpretation rule: a label is an *observed/on-chain classification* —
it never asserts real-world ownership identity.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.database.models import AddressLabel, Chain
from config import LABELS_FILE

log = logging.getLogger("labels")


def load_labels_from_file(session_factory, path=LABELS_FILE) -> int:
    """Import JSON labels into DB (idempotent upsert). Returns row count."""
    if not path.exists():
        log.info("no label file at %s — skipping import", path)
        return 0
    try:
        data = json.loads(path.read_text())
    except (ValueError, OSError) as e:
        log.warning("label file unreadable: %s", e)
        return 0
    n = 0
    with session_factory() as s:
        chains = {c.key: c.id for c in s.query(Chain).all()}
        dialect = s.bind.dialect.name if s.bind else "sqlite"
        ins_cls = pg_insert if dialect == "postgresql" else sqlite_insert
        for chain_key, entries in data.items():
            pk = chains.get(chain_key)
            if pk is None or not isinstance(entries, list):
                continue
            for ent in entries:
                addr = (ent.get("address") or "").lower()
                if len(addr) != 42:
                    continue
                vals = dict(chain_pk=pk, address=addr,
                            label=ent.get("label") or "labelled",
                            entity_type=ent.get("entity_type") or "unknown",
                            source=ent.get("source") or "file_import",
                            confidence=float(ent.get("confidence") or 0.9),
                            verified=bool(ent.get("verified")),
                            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
                            updated_at=datetime.now(timezone.utc).replace(tzinfo=None))
                stmt = ins_cls(AddressLabel).values(**vals).on_conflict_do_update(
                    index_elements=["chain_pk", "address", "source"],
                    set_={"label": vals["label"], "entity_type": vals["entity_type"],
                          "confidence": vals["confidence"],
                          "updated_at": vals["updated_at"]})
                s.execute(stmt)
                n += 1
        s.commit()
    log.info("imported %d address labels from %s", n, path.name)
    return n


class LabelCache:
    """In-memory per-chain label lookup used by the intelligence engine."""

    def __init__(self, session_factory):
        self.session_factory = session_factory
        self._by_chain: dict[int, dict[str, AddressLabel]] = {}

    def get(self, chain_pk: int, address: str) -> AddressLabel | None:
        if chain_pk not in self._by_chain:
            with self.session_factory() as s:
                rows = s.query(AddressLabel).filter_by(chain_pk=chain_pk).all()
            self._by_chain[chain_pk] = {r.address.lower(): r for r in rows}
        return self._by_chain[chain_pk].get(address.lower())

    def addresses_of_type(self, chain_pk: int, entity_type: str) -> set[str]:
        cache = self._by_chain.get(chain_pk)
        if cache is None:
            self.get(chain_pk, "0x" + "0" * 40)   # trigger load
            cache = self._by_chain.get(chain_pk, {})
        return {a for a, lbl in cache.items() if lbl.entity_type == entity_type}

    def invalidate(self, chain_pk: int | None = None):
        if chain_pk is None:
            self._by_chain.clear()
        else:
            self._by_chain.pop(chain_pk, None)
