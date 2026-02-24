from decimal import Decimal
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.domain.models import Signal, SignalType, SetupType
from src.live.signal_handler import handle_signal


@pytest.mark.asyncio
async def test_handle_signal_blocks_entries_when_hardening_gate_closed():
    signal = Signal(
        timestamp=datetime.now(timezone.utc),
        symbol="BTC/USD",
        signal_type=SignalType.LONG,
        entry_price=Decimal("50000"),
        stop_loss=Decimal("49000"),
        take_profit=Decimal("52000"),
        reasoning="gate test",
        setup_type=SetupType.OB,
        regime="tight_smc",
        higher_tf_bias="bullish",
        adx=Decimal("25"),
        atr=Decimal("500"),
        ema200_slope="up",
        score=80.0,
    )
    lt = SimpleNamespace(
        signals_since_emit=0,
        trade_paused=False,
        hardening=SimpleNamespace(is_trading_allowed=lambda: False),
    )

    result = await handle_signal(
        lt=lt,
        signal=signal,
        spot_price=Decimal("50000"),
        mark_price=Decimal("50010"),
    )

    assert result["order_placed"] is False
    assert "hardening_gate_closed" in result["rejection_reasons"]
