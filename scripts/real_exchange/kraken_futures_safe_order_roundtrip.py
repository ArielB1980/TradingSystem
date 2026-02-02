"""
Manual real-exchange order roundtrip (place far-from-market limit order, then cancel).

This is *not* part of automated tests.
"""

from __future__ import annotations

import asyncio
import os

from decimal import Decimal


def _require_env(name: str) -> str:
    v = os.getenv(name)
    if not v or not v.strip():
        raise SystemExit(f"Missing required env var: {name}")
    return v


def _ensure_allowed() -> None:
    if os.getenv("RUN_REAL_EXCHANGE_TESTS", "0").strip() not in ("1", "true", "TRUE", "yes", "YES"):
        raise SystemExit("Refusing to run real-exchange checks. Set RUN_REAL_EXCHANGE_TESTS=1 to enable.")
    if os.getenv("RUN_REAL_EXCHANGE_ORDERS", "0").strip() not in ("1", "true", "TRUE", "yes", "YES"):
        raise SystemExit("Refusing to place orders. Set RUN_REAL_EXCHANGE_ORDERS=1 to enable.")
    if os.getenv("CONFIRM_LIVE", "").strip().upper() != "YES":
        raise SystemExit("Refusing to place orders. Set CONFIRM_LIVE=YES to enable.")


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

    order_id: str | None = None

    try:
        await client.initialize()

        mark_price = await client.get_futures_mark_price("BTCUSD-PERP")
        safe_price = mark_price * Decimal("0.80")  # far below market; should not fill
        size = Decimal("1")  # minimal

        logger.warning(
            "Placing SAFE far-from-market order (manual gated)",
            symbol="PF_XBTUSD",
            side="buy",
            size=str(size),
            safe_price=str(safe_price),
            mark_price=str(mark_price),
        )

        resp = await client.place_futures_order(
            symbol="PF_XBTUSD",
            side="buy",
            order_type="lmt",
            size=size,
            price=safe_price,
            leverage=10,
            client_order_id="manual_safe_order_roundtrip_001",
        )
        send_status = resp.get("sendStatus", {}) or {}
        order_id = send_status.get("order_id")
        logger.info("Order placement response", order_id=order_id, status=send_status.get("status"))

        if not order_id:
            raise SystemExit(f"Order placement did not return an order id: {send_status}")

        await asyncio.sleep(1)

        orders = await client.get_futures_open_orders()
        logger.info("Fetched open orders", count=len(orders))

        logger.warning("Cancelling test order", order_id=order_id)
        await client.cancel_futures_order(order_id, symbol="BTC/USD:USD")

        await asyncio.sleep(1)
        orders2 = await client.get_futures_open_orders()
        still = [o for o in orders2 if str(o.get("id")) == str(order_id)]
        logger.info("Cancel verification complete", still_open=bool(still), open_orders=len(orders2))

    finally:
        try:
            await client.close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())

