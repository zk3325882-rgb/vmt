"""Crypto Intelligence Scanner — Phase 1 entry point.

Usage:
    python main.py            # live scan + dashboard at http://127.0.0.1:8000
    MOCK_MODE=true python main.py   # offline synthetic chain data (dev)
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import webbrowser
from datetime import datetime, timezone

import uvicorn

from app.blockchain.manager import ChainManager
from app.collectors.block_collector import BlockCollector
from app.collectors.dex_collector import DexCollector
from app.collectors.transfer_collector import TransferCollector
from app.database.connection import SessionLocal, init_db
from app.market.prices import NativeAssetPrice, PriceService
from app.tokens.discovery import TokenDiscoveryService
from config import BASE_DIR, settings

_LOG_DIR = BASE_DIR / "data"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler(_LOG_DIR / "scanner.log", encoding="utf-8")],
)
log = logging.getLogger("main")


async def run_scanner() -> tuple[dict[str, BlockCollector], "TokenDiscoveryService"]:
    init_db()
    discovery = TokenDiscoveryService(SessionLocal)
    manager = ChainManager(mock=settings.mock_mode)
    await manager.connect_all()
    native = NativeAssetPrice()
    if not settings.mock_mode:
        await native.refresh()

    collectors: dict[str, BlockCollector] = {}
    for key, adapter in manager.adapters.items():
        cfg = settings.chains[key]
        price_service = PriceService(adapter, key, SessionLocal, native)
        transfer_collector = TransferCollector(adapter, key, SessionLocal,
                                               discovery, price_service)
        dex_collector = DexCollector(adapter, cfg.dexes)
        block_collector = BlockCollector(adapter, key, SessionLocal,
                                         transfer_collector, dex_collector, discovery)
        collectors[key] = block_collector
        asyncio.create_task(block_collector.run(), name=f"scan-{key}")
    return collectors, discovery


async def main() -> None:
    log.info("=== Crypto Intelligence Scanner (Phase 1) === mock=%s db=%s",
             settings.mock_mode, settings.database_url)
    collectors, discovery = await run_scanner()

    # hand scanner state to the API/dashboard
    from app.api.server import SCANNER, app
    SCANNER["collectors"] = collectors
    SCANNER["discovery"] = discovery
    SCANNER["started_at"] = datetime.now(timezone.utc).isoformat()
    SCANNER["mock_mode"] = settings.mock_mode

    config = uvicorn.Config(app, host=settings.api_host, port=settings.api_port,
                            log_level="warning")
    server = uvicorn.Server(config)
    tasks = [asyncio.create_task(server.serve())]

    url = f"http://{settings.api_host}:{settings.api_port}/"
    log.info("GUI dashboard available at %s", url)
    if os.getenv("OPEN_GUI", "true").lower() not in ("0", "false", "no"):
        # open the dashboard in the default browser (works on Windows/mac/linux)
        import threading
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()

    stop_event = asyncio.Event()

    def _stop(*_):
        stop_event.set()

    try:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _stop)
    except NotImplementedError:  # Windows
        pass

    await stop_event.wait()
    log.info("shutting down…")
    for col in collectors.values():
        col.stop()
    server.should_exit = True
    await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("interrupted by user; progress is persisted and will resume.")
