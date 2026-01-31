import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from decimal import Decimal


from src.live.live_trading import _exchange_position_side, LiveTrading


def test_exchange_position_side_prefers_explicit_side_field():
    # KrakenClient.get_all_futures_positions() returns size as ABS(size) and provides `side`.
    assert _exchange_position_side({"size": 123, "side": "short"}) == "short"
    assert _exchange_position_side({"size": 123, "side": "long"}) == "long"

    # Fallback: if side missing, infer from signed size (compat with older adapters)
    assert _exchange_position_side({"size": "-1"}) == "short"
    assert _exchange_position_side({"size": "1"}) == "long"


@pytest.mark.asyncio
async def test_auto_place_missing_stops_uses_position_side_for_shorts():
    """
    Regression test:
    - KrakenClient positions have positive size + explicit `side`.
    - LiveTrading previously inferred side from size sign -> treated all as LONG.
    That caused SHORT positions to not get protective BUY stops above entry.
    """
    dummy = SimpleNamespace()
    dummy.config = SimpleNamespace(system=SimpleNamespace(dry_run=False))
    dummy.client = AsyncMock()
    dummy.client.get_futures_open_orders.return_value = []
    dummy.client.place_futures_order.return_value = {"id": "stop-1"}

    raw_positions = [
        {
            "symbol": "PF_XRPUSD",
            "size": Decimal("26"),
            "entry_price": Decimal("1.93136"),
            "side": "short",
        }
    ]

    await LiveTrading._place_missing_stops_for_unprotected(dummy, raw_positions, max_per_tick=3)

    dummy.client.place_futures_order.assert_called_once()
    kwargs = dummy.client.place_futures_order.call_args.kwargs
    assert kwargs["symbol"] == "XRP/USD:USD"
    assert kwargs["order_type"] == "stop"
    assert kwargs["reduce_only"] is True
    assert kwargs["side"] == "buy"  # SHORT protection must BUY to close
    assert kwargs["stop_price"] > Decimal("1.93136")  # SHORT stop must be above entry

