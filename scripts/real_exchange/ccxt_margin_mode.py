"""
Manual CCXT Kraken Futures margin/leverage calls.

NOT part of automated tests.
"""

from __future__ import annotations

import asyncio
import os


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
    import ccxt.async_support as ccxt

    setup_logging("INFO", "text")
    logger = get_logger(__name__)

    exchange = ccxt.krakenfutures(
        {
            "apiKey": _require_env("KRAKEN_FUTURES_API_KEY"),
            "secret": _require_env("KRAKEN_FUTURES_API_SECRET"),
            "enableRateLimit": True,
        }
    )

    try:
        symbol = os.getenv("TEST_SYMBOL", "PF_XBTUSD")
        margin_mode = os.getenv("TEST_MARGIN_MODE", "isolated")
        leverage = int(os.getenv("TEST_LEVERAGE", "10"))

        logger.info("Attempting margin/leverage calls", symbol=symbol, margin_mode=margin_mode, leverage=leverage)

        if hasattr(exchange, "set_margin_mode"):
            try:
                await exchange.set_margin_mode(margin_mode, symbol, params={"leverage": leverage})
                logger.info("set_margin_mode succeeded")
            except Exception as e:
                logger.warning("set_margin_mode failed", error=str(e))

        if hasattr(exchange, "set_leverage"):
            try:
                await exchange.set_leverage(leverage, symbol)
                logger.info("set_leverage succeeded")
            except Exception as e:
                logger.warning("set_leverage failed", error=str(e))

    finally:
        await exchange.close()


if __name__ == "__main__":
    asyncio.run(main())

