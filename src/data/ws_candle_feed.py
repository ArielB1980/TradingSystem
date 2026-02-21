"""
Kraken WebSocket v2 OHLC candle feed.

Connects to wss://ws.kraken.com/v2, subscribes to the ohlc channel for all
candidate symbols, and pushes live candle updates into CandleManager's
in-memory cache.  REST polling remains the authoritative source; WS just
keeps the cache warmer between REST cycles.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Dict, List, Optional, Set

import websockets

from src.domain.models import Candle
from src.monitoring.logger import get_logger

if TYPE_CHECKING:
    from src.data.candle_manager import CandleManager

logger = get_logger(__name__)

WS_ENDPOINT = "wss://ws.kraken.com/v2"
MAX_SYMBOLS_PER_SUB = 50  # Kraken may limit per-message; batch to be safe


class KrakenCandleFeed:
    """Streams 15m OHLC candles over Kraken WebSocket v2 into CandleManager."""

    def __init__(
        self,
        candle_manager: CandleManager,
        symbols: List[str],
        interval: int = 15,
        max_retries: int = 10,
        backoff_base: int = 5,
    ):
        self._cm = candle_manager
        self._symbols = list(symbols)
        self._interval = interval
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._ws: Optional[websockets.ClientConnection] = None
        self._running = False
        self._retry_count = 0
        self._received_count = 0
        self._subscribed_symbols: Set[str] = set()
        logger.info(
            "KrakenCandleFeed initialized",
            symbol_count=len(self._symbols),
            interval=self._interval,
        )

    async def run(self) -> None:
        """Connect, subscribe, and stream indefinitely with auto-reconnect."""
        self._running = True
        while self._running and self._retry_count < self._max_retries:
            try:
                await self._connect_and_stream()
            except asyncio.CancelledError:
                logger.info("KrakenCandleFeed cancelled")
                break
            except Exception as e:
                self._retry_count += 1
                backoff = self._backoff_base * (2 ** min(self._retry_count - 1, 6))
                logger.warning(
                    "WS_CANDLE_FEED_DISCONNECT",
                    error=str(e),
                    error_type=type(e).__name__,
                    retry=self._retry_count,
                    max_retries=self._max_retries,
                    backoff_s=backoff,
                )
                if self._retry_count < self._max_retries:
                    await asyncio.sleep(backoff)
                else:
                    logger.error(
                        "WS_CANDLE_FEED_MAX_RETRIES",
                        retries=self._retry_count,
                    )

        self._running = False
        logger.info("KrakenCandleFeed stopped", total_received=self._received_count)

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def _connect_and_stream(self) -> None:
        async with websockets.connect(
            WS_ENDPOINT,
            ping_interval=30,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            self._ws = ws
            self._retry_count = 0
            logger.info("WS_CANDLE_FEED_CONNECTED")

            await self._subscribe(ws)

            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    continue
                await self._handle_message(msg)

    async def _subscribe(self, ws: websockets.ClientConnection) -> None:
        """Send subscribe requests in batches."""
        for i in range(0, len(self._symbols), MAX_SYMBOLS_PER_SUB):
            batch = self._symbols[i : i + MAX_SYMBOLS_PER_SUB]
            payload = {
                "method": "subscribe",
                "params": {
                    "channel": "ohlc",
                    "symbol": batch,
                    "interval": self._interval,
                    "snapshot": True,
                },
            }
            await ws.send(json.dumps(payload))
            logger.info(
                "WS_CANDLE_SUBSCRIBE_SENT",
                batch_size=len(batch),
                interval=self._interval,
                first=batch[0] if batch else "?",
            )

    async def _handle_message(self, msg: dict) -> None:
        channel = msg.get("channel")

        if channel == "ohlc":
            msg_type = msg.get("type", "")
            data = msg.get("data", [])
            for item in data:
                self._process_candle(item, is_snapshot=(msg_type == "snapshot"))
            return

        if channel == "heartbeat":
            return

        # subscription ack / error
        method = msg.get("method")
        if method == "subscribe":
            success = msg.get("success", False)
            result = msg.get("result", {})
            sym = result.get("symbol") if isinstance(result, dict) else None
            if success and sym:
                self._subscribed_symbols.add(sym)
            elif not success:
                err = msg.get("error", "unknown")
                logger.warning("WS_CANDLE_SUBSCRIBE_FAIL", error=err, detail=str(msg)[:200])
            return

    def _process_candle(self, item: dict, *, is_snapshot: bool) -> None:
        symbol = item.get("symbol")
        interval = item.get("interval")
        if not symbol or interval != self._interval:
            return

        ts_str = item.get("interval_begin") or item.get("timestamp")
        if not ts_str:
            return

        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return

        tf = self._interval_to_timeframe(interval)
        if tf is None:
            return

        candle = Candle(
            timestamp=ts,
            symbol=symbol,
            timeframe=tf,
            open=Decimal(str(item.get("open", 0))),
            high=Decimal(str(item.get("high", 0))),
            low=Decimal(str(item.get("low", 0))),
            close=Decimal(str(item.get("close", 0))),
            volume=Decimal(str(item.get("volume", 0))),
        )

        self._cm.receive_ws_candle(symbol, tf, candle)
        self._received_count += 1

    @staticmethod
    def _interval_to_timeframe(interval: int) -> Optional[str]:
        return {1: "1m", 5: "5m", 15: "15m", 30: "30m", 60: "1h", 240: "4h", 1440: "1d"}.get(interval)
