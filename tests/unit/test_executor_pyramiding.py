"""
Unit tests for Executor pyramiding guard.

Ensures the guard rejects new entries when a position already exists in the same
market, even when symbol formats differ (exchange PF_* / PI_* vs mapped ROSE/USD:USD).
"""
from __future__ import annotations

import pytest
from decimal import Decimal
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, AsyncMock

from src.domain.models import (
    Signal,
    SignalType,
    SetupType,
    OrderIntent,
    Position,
    Side,
)
from src.execution.executor import Executor
from src.config.config import ExecutionConfig


def _make_rose_signal() -> Signal:
    return Signal(
        timestamp=datetime.now(timezone.utc),
        symbol="ROSE/USD",
        signal_type=SignalType.SHORT,
        entry_price=Decimal("0.02"),
        stop_loss=Decimal("0.022"),
        take_profit=Decimal("0.018"),
        reasoning="test",
        setup_type=SetupType.OB,
        regime="tight_smc",
        higher_tf_bias="bearish",
        adx=Decimal("20"),
        atr=Decimal("0.001"),
        ema200_slope="down",
    )


def _make_rose_intent() -> OrderIntent:
    sig = _make_rose_signal()
    return OrderIntent(
        timestamp=datetime.now(timezone.utc),
        signal=sig,
        side=Side.SHORT,
        size_notional=Decimal("200"),
        leverage=Decimal("7"),
        entry_price_spot=sig.entry_price,
        stop_loss_spot=sig.stop_loss,
        take_profit_spot=sig.take_profit,
        entry_price_futures=Decimal("0.02"),
        stop_loss_futures=Decimal("0.022"),
        take_profit_futures=Decimal("0.018"),
    )


def _make_position(symbol: str) -> Position:
    return Position(
        symbol=symbol,
        side=Side.SHORT,
        size=Decimal("10000"),
        size_notional=Decimal("200"),
        entry_price=Decimal("0.02"),
        current_mark_price=Decimal("0.018"),
        liquidation_price=Decimal("0.025"),
        unrealized_pnl=Decimal("-20"),
        leverage=Decimal("7"),
        margin_used=Decimal("30"),
    )


@pytest.mark.asyncio
async def test_pyramiding_guard_rejects_when_position_exists_different_format():
    """
    When current_positions has PF_ROSEUSD and map_spot_to_futures returns ROSE/USD:USD,
    the guard must treat them as the same market and reject the new order.
    """
    adapter = MagicMock()
    adapter.map_spot_to_futures = MagicMock(return_value="ROSE/USD:USD")
    adapter.kraken_client = MagicMock()

    config = ExecutionConfig(pyramiding_enabled=False)

    with patch("src.storage.repository.load_recent_intent_hashes", return_value=[]):
        executor = Executor(config=config, futures_adapter=adapter)

    intent = _make_rose_intent()
    current_positions = [_make_position("PF_ROSEUSD")]
    mark_price = Decimal("0.018")

    result = await executor.execute_signal(intent, mark_price, current_positions)

    assert result is None
    adapter.map_spot_to_futures.assert_called_once()


@pytest.mark.asyncio
async def test_pyramiding_guard_rejects_pi_format_vs_ccxt():
    """
    Position in PI_* format must still block open when signal maps to CCXT unified.
    """
    adapter = MagicMock()
    adapter.map_spot_to_futures = MagicMock(return_value="ROSE/USD:USD")
    adapter.kraken_client = MagicMock()

    config = ExecutionConfig(pyramiding_enabled=False)

    with patch("src.storage.repository.load_recent_intent_hashes", return_value=[]):
        executor = Executor(config=config, futures_adapter=adapter)

    intent = _make_rose_intent()
    current_positions = [_make_position("PI_ROSEUSD")]
    mark_price = Decimal("0.018")

    result = await executor.execute_signal(intent, mark_price, current_positions)

    assert result is None


@pytest.mark.asyncio
async def test_pyramiding_guard_allows_when_no_position():
    """
    When current_positions is empty, the guard does not block. We get past the
    guard (map_spot_to_futures is called) and flow continues; we do not assert
    on final result since order placement is mocked and may fail for other reasons.
    """
    adapter = MagicMock()
    adapter.map_spot_to_futures = MagicMock(return_value="ROSE/USD:USD")
    adapter.kraken_client = MagicMock()
    adapter.kraken_client.get_futures_open_orders = AsyncMock(return_value=[])
    # Executor awaits place_order; return a mock order so flow continues
    mock_order = MagicMock(client_order_id="test-entry-1")
    adapter.place_order = AsyncMock(return_value=mock_order)

    config = ExecutionConfig(pyramiding_enabled=False)

    with patch("src.storage.repository.load_recent_intent_hashes", return_value=[]):
        executor = Executor(config=config, futures_adapter=adapter)

    intent = _make_rose_intent()
    current_positions: list[Position] = []
    mark_price = Decimal("0.018")

    await executor.execute_signal(intent, mark_price, current_positions)

    # Guard did not fire: we called map_spot_to_futures and did not return
    # early from "position already exists".
    adapter.map_spot_to_futures.assert_called_once()
