"""
Tests for Production Takeover Protocol.

Verifies correct handling of:
- Case A: Protected positions (stop exists)
- Case B: Naked positions (stop placement)
- Case C: Chaos (multiple stops -> resolve)
- Case D: Duplicate/Stale local state (purge)
"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from decimal import Decimal
from datetime import datetime, timezone

from src.execution.production_takeover import (
    ProductionTakeover, 
    TakeoverConfig, 
    TakeoverCase
)
from src.execution.execution_gateway import ExecutionGateway
from src.domain.models import Side

@pytest.fixture
def mock_gateway():
    gateway = MagicMock(spec=ExecutionGateway)
    gateway.client = AsyncMock()
    gateway.registry = MagicMock()
    gateway.registry._positions = {}
    gateway.registry.has_position.return_value = False
    gateway.persistence = MagicMock()
    return gateway

@pytest.fixture
def takeover(mock_gateway):
    config = TakeoverConfig(
        takeover_stop_pct=Decimal("0.02"),
        stop_replace_atomically=True,
        dry_run=False
    )
    return ProductionTakeover(mock_gateway, config)

@pytest.mark.asyncio
async def test_case_a_protected_position(takeover, mock_gateway):
    """Case A: Position exists with valid stop."""
    symbol = "BTC/USD:USD"
    pos_data = {
        "symbol": symbol,
        "side": Side.LONG,
        "qty": Decimal("1.0"),
        "entry_price": Decimal("50000")
    }
    
    # Existing valid stop
    stop_order = {
        "id": "stop-1",
        "symbol": symbol,
        "type": "stop",
        "side": "sell",
        "amount": "1.0",
        "stopPrice": "49000",
        "status": "open"
    }
    
    orders = [stop_order]
    mock_gateway.client.get_futures_open_orders.return_value = orders
    mock_gateway.client.get_all_futures_positions.return_value = [
        {"symbol": symbol, "side": "long", "size": 1.0, "entry_price": 50000}
    ]
    
    stats = await takeover.execute_takeover()
    
    assert stats["imported"] == 1
    assert stats["stops_placed"] == 0
    assert stats["orders_cancelled"] == 0
    
    # Validated stop price used for import
    mock_gateway.persistence.save_position.assert_called_once()
    saved_pos = mock_gateway.persistence.save_position.call_args[0][0]
    assert saved_pos.initial_stop_price == Decimal("49000")

@pytest.mark.asyncio
async def test_case_b_naked_position(takeover, mock_gateway):
    """Case B: Naked position -> Place fresh stop."""
    symbol = "ETH/USD:USD"
    pos_data = {
        "symbol": symbol,
        "side": Side.SHORT,
        "qty": Decimal("10.0"),
        "entry_price": Decimal("3000")
    }
    
    # No orders
    mock_gateway.client.get_futures_open_orders.return_value = []
    mock_gateway.client.get_all_futures_positions.return_value = [
        {"symbol": symbol, "side": "short", "size": 10.0, "entry_price": 3000}
    ]
    # Mock ticker for current price
    mock_gateway.client.get_futures_mark_price.return_value = Decimal("3000")
    mock_gateway.client.place_futures_order.return_value = {"id": "new-stop-1"}
    
    stats = await takeover.execute_takeover()
    
    assert stats["imported"] == 1
    assert stats["stops_placed"] == 1
    
    # Verify stop placement
    # Short -> Stop above entry (plus 2%)
    expected_stop = Decimal("3000") * Decimal("1.02")
    mock_gateway.client.place_futures_order.assert_called_once()
    args = mock_gateway.client.place_futures_order.call_args[1]
    assert args["side"] == "buy"
    assert args["order_type"] == "stop"
    assert args["stop_price"] == expected_stop

@pytest.mark.asyncio
async def test_case_c_chaos_resolution(takeover, mock_gateway):
    """Case C: Multiple conflicting stops -> Cancel all & Replace."""
    symbol = "SOL/USD:USD"
    
    # Two stop orders
    orders = [
        {"id": "stop-1", "symbol": symbol, "type": "stop", "stopPrice": "20"},
        {"id": "stop-2", "symbol": symbol, "type": "stop", "stopPrice": "22"}
    ]
    
    mock_gateway.client.get_futures_open_orders.return_value = orders
    mock_gateway.client.get_all_futures_positions.return_value = [
        {"symbol": symbol, "side": "long", "size": 100.0, "entry_price": 25.0}
    ]
    mock_gateway.client.get_futures_mark_price.return_value = Decimal("25.0")
    
    stats = await takeover.execute_takeover()
    
    # Both cancelled
    assert mock_gateway.client.cancel_futures_order.call_count == 2
    # One fresh stop placed
    assert stats["stops_placed"] == 1
    assert stats["imported"] == 1

@pytest.mark.asyncio
async def test_case_d_duplicate_purge(takeover, mock_gateway):
    """Case D: Local stale state -> Purge before import."""
    symbol = "XRP/USD:USD"
    
    # Registry has stale position
    mock_gateway.registry.has_position.return_value = True
    mock_gateway.registry._positions = {symbol: MagicMock()}
    
    mock_gateway.client.get_futures_open_orders.return_value = []
    mock_gateway.client.get_all_futures_positions.return_value = [
        {"symbol": symbol, "side": "long", "size": 1000.0, "entry_price": 0.5}
    ]
    mock_gateway.client.get_futures_mark_price.return_value = Decimal("0.5")
    
    stats = await takeover.execute_takeover()
    
    # Registry cleaned
    assert symbol not in mock_gateway.registry._positions
    
    # Fresh import
    assert stats["imported"] == 1
    assert stats["stops_placed"] == 1  # Was naked on exchange

@pytest.mark.asyncio
async def test_emergency_quarantine_on_failure(takeover, mock_gateway):
    """Failsafe: If stop placement fails -> Quarantine."""
    symbol = "DOGE/USD:USD"
    
    # Naked position
    mock_gateway.client.get_all_futures_positions.return_value = [
        {"symbol": symbol, "side": "long", "size": 10000.0, "entry_price": 0.1}
    ]
    mock_gateway.client.get_futures_open_orders.return_value = []
    
    # Stop placement FAILS
    mock_gateway.client.place_futures_order.side_effect = Exception("API Error")
    mock_gateway.client.get_futures_mark_price.return_value = Decimal("0.1")
    
    stats = await takeover.execute_takeover()
    
    # Not imported
    assert stats["imported"] == 0
    # Quarantined
    assert stats["quarantined"] == 1
    assert symbol in takeover.quarantined_positions
    
    # Emergency flatten attempt happened
    assert mock_gateway.client.place_futures_order.call_count >= 1 # Stop fail + Flatten attempt
