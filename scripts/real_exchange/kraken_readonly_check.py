"""
Manual real-exchange connectivity checks (read-only).

This script is intentionally NOT a pytest test.
"""

from __future__ import annotations

import asyncio
import os
import sys


def _require_env(name: str) -> str:
    v = os.getenv(name)
    if not v or not v.strip():
        raise SystemExit(f"Missing required env var: {name}")
    return v


def _ensure_allowed() -> None:
    if os.getenv("RUN_REAL_EXCHANGE_TESTS", "0").strip() not in ("1", "true", "TRUE", "yes", "YES"):
        raise SystemExit("Refusing to run real-exchange checks. Set RUN_REAL_EXCHANGE_TESTS=1 to enable.")


async def main() -> None:
    _ensure_allowed()

    from src.monitoring.logger import setup_logging, get_logger
    from src.data.kraken_client import KrakenClient

    setup_logging("INFO", "text")
    logger = get_logger(__name__)

    client = KrakenClient(
        api_key=_require_env("KRAKEN_API_KEY"),
        api_secret=_require_env("KRAKEN_API_SECRET"),
        futures_api_key=_require_env("KRAKEN_FUTURES_API_KEY"),
        futures_api_secret=_require_env("KRAKEN_FUTURES_API_SECRET"),
        use_testnet=False,
    )

    try:
        await client.initialize()
        logger.info("Client initialized")

        # Spot ticker sanity
        t = await client.get_spot_ticker("BTC/USD")
        logger.info("Spot ticker fetched", symbol="BTC/USD", last=t.get("last"))

        # Futures mark price sanity (public)
        mp = await client.get_futures_mark_price("BTCUSD-PERP")
        logger.info("Futures mark price fetched", symbol="BTCUSD-PERP", mark_price=str(mp))

        # Private: open orders / positions
        orders = await client.get_futures_open_orders()
        logger.info("Futures open orders fetched", count=len(orders))

        positions = await client.get_all_futures_positions()
        open_positions = [p for p in positions if float(p.get("size", 0) or 0) != 0]
        logger.info("Futures positions fetched", total=len(positions), open=len(open_positions))

    finally:
        try:
            await client.close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())

