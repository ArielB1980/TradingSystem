"""
Manual backtest smoke (last 7 days of BTC/USD).

NOT part of automated tests.
Requires a PostgreSQL DATABASE_URL (backtest uses persistence).
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone, timedelta
from decimal import Decimal


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
    from src.config.config import load_config
    from src.data.kraken_client import KrakenClient
    from src.backtest.backtest_engine import BacktestEngine
    from src.storage.db import init_db

    setup_logging("INFO", "text")
    logger = get_logger(__name__)

    database_url = _require_env("DATABASE_URL")
    if not database_url.startswith(("postgresql://", "postgres://")):
        raise SystemExit("Backtest requires PostgreSQL. Set DATABASE_URL to a postgresql:// connection string.")

    config = load_config("src/config/config.yaml")
    init_db(database_url)

    client = KrakenClient(
        api_key=_require_env("KRAKEN_API_KEY"),
        api_secret=_require_env("KRAKEN_API_SECRET"),
        use_testnet=False,
    )

    config.backtest.starting_equity = float(os.getenv("BACKTEST_START_EQUITY", "3880.0"))
    engine = BacktestEngine(config, symbol="BTC/USD", starting_equity=Decimal(str(config.backtest.starting_equity)))
    engine.set_client(client)

    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=7)

    logger.info("Running 7d backtest", symbol="BTC/USD", start=str(start_date), end=str(end_date))
    metrics = await engine.run(start_date, end_date)
    logger.info(
        "Backtest complete",
        total_trades=metrics.total_trades,
        win_rate=metrics.win_rate,
        total_pnl=str(metrics.total_pnl),
        max_drawdown=str(metrics.max_drawdown),
    )


if __name__ == "__main__":
    asyncio.run(main())

