"""Database connection / session management (SQLAlchemy 2.x, sync engine).

PostgreSQL is the target; SQLite fallback keeps Phase 1 runnable without a
local Postgres install. The scanner uses async tasks but DB work runs in
threads via asyncio.to_thread — simple and RAM-friendly.
"""
from __future__ import annotations

import logging

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.database.models import Base, Chain
from config import settings

log = logging.getLogger("db")

_engine_kwargs: dict = {"pool_pre_ping": True, "future": True}
if settings.database_url.startswith("sqlite"):
    _engine_kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
else:
    _engine_kwargs.update({"pool_size": 5, "max_overflow": 10})

engine = create_engine(settings.database_url, **_engine_kwargs)

if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _rec):  # better concurrency on SQLite
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    """Create tables and seed configured chains."""
    Base.metadata.create_all(engine)
    from config import CHAINS
    with SessionLocal() as s:
        for key, cfg in CHAINS.items():
            chain = s.query(Chain).filter_by(key=key).first()
            if not chain:
                chain = Chain(key=key, name=cfg.name, chain_id=cfg.chain_id,
                              native_symbol=cfg.symbol)
                s.add(chain)
        s.commit()
    log.info("Database initialized at %s", settings.database_url.split("@")[-1])


def get_session():
    """FastAPI dependency."""
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()
