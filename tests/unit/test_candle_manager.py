"""Unit tests for CandleManager (hydration, update_candles, futures fallback)."""
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.data.candle_manager import CandleManager, _candles_with_symbol
from src.domain.models import Candle


def _candle(symbol: str, tf: str, ts: datetime) -> Candle:
    return Candle(
        timestamp=ts,
        symbol=symbol,
        timeframe=tf,
        open=Decimal("1"),
        high=Decimal("1"),
        low=Decimal("1"),
        close=Decimal("1"),
        volume=Decimal("1"),
    )


def test_candles_with_symbol():
    ts = datetime.now(timezone.utc)
    c = _candle("PF_XBTUSD", "15m", ts)
    out = _candles_with_symbol([c], "BTC/USD")
    assert len(out) == 1
    assert out[0].symbol == "BTC/USD"
    assert out[0].timeframe == "15m"


@pytest.mark.asyncio
async def test_update_candles_futures_fallback_when_spot_fails():
    """When spot OHLCV fails and use_futures_fallback=True, we use futures OHLCV."""
    with patch("src.data.candle_manager.get_latest_candle_timestamp", return_value=None):
        client = MagicMock()
        from src.exceptions import DataError
        client.get_spot_ohlcv = AsyncMock(side_effect=DataError("BadSymbol"))
        client.get_futures_ohlcv = AsyncMock(
            return_value=[
                _candle("PF_ZILUSD", "15m", datetime.now(timezone.utc)),
            ]
        )
        cm = CandleManager(
            client,
            spot_to_futures=lambda s: "PF_ZILUSD" if "ZIL" in s else f"PF_{s.split('/')[0]}USD",
            use_futures_fallback=True,
        )
        cm.last_candle_update["ZIL/USD"] = {
            "15m": datetime.min.replace(tzinfo=timezone.utc),
            "1h": datetime.min.replace(tzinfo=timezone.utc),
            "4h": datetime.min.replace(tzinfo=timezone.utc),
            "1d": datetime.min.replace(tzinfo=timezone.utc),
        }

        await cm.update_candles("ZIL/USD")

        candles = cm.get_candles("ZIL/USD", "15m")
        assert len(candles) >= 1
        assert candles[0].symbol == "ZIL/USD"
        assert cm.pop_futures_fallback_count() == 1
