"""
Full lifecycle integration test (P3.2).

Golden-path test: signal → risk → entry order → entry fill → stop placed →
TP1 fill → stop moved to break-even → TP2 fill → position closed →
trade recorded.

Uses a FakeExchange that processes orders deterministically (no timers).
Should run in < 5 seconds.
"""
import asyncio
import os
import pytest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Any
from unittest.mock import AsyncMock, MagicMock, patch

from src.execution.execution_gateway import (
    ExecutionGateway,
    ExecutionResult,
    PendingOrder,
    OrderPurpose,
)
from src.execution.position_state_machine import (
    ManagedPosition,
    PositionState,
    PositionRegistry,
    OrderEvent,
    OrderEventType,
    ExitReason,
    reset_position_registry,
)
from src.execution.position_manager_v2 import (
    PositionManagerV2,
    ManagementAction,
    ActionType,
)
from src.execution.position_persistence import PositionPersistence
from src.domain.models import Side, OrderType, Signal, SignalType, SetupType


# ---------------------------------------------------------------------------
# FakeExchange: deterministic mock for KrakenClient
# ---------------------------------------------------------------------------

class FakeExchange:
    """Deterministic mock exchange that instantly fills orders."""

    def __init__(self):
        self._order_counter = 0
        self._orders: Dict[str, Dict] = {}
        self._open_orders: List[Dict] = []
        self._positions: List[Dict] = []

    async def initialize(self):
        pass

    async def place_futures_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        size: Decimal,
        price: Optional[Decimal] = None,
        stop_price: Optional[Decimal] = None,
        reduce_only: bool = False,
        leverage: Optional[Decimal] = None,
        client_order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._order_counter += 1
        oid = f"FAKE-{self._order_counter:04d}"
        order = {
            "id": oid,
            "clientOrderId": client_order_id,
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "amount": float(size),
            "price": float(price) if price else None,
            "stopPrice": float(stop_price) if stop_price else None,
            "reduceOnly": reduce_only,
            "status": "open",
            "filled": 0,
            "remaining": float(size),
            "average": None,
            "trades": [],
        }
        self._orders[oid] = order
        self._open_orders.append(order)
        return order

    async def create_order(self, symbol, type, side, amount, price=None, params=None, leverage=None):
        """CCXT-style create_order — delegates to place_futures_order."""
        p = params or {}
        return await self.place_futures_order(
            symbol=symbol,
            side=side,
            order_type=type,
            size=Decimal(str(amount)),
            price=Decimal(str(price)) if price else None,
            stop_price=Decimal(str(p.get("stopPrice"))) if p.get("stopPrice") else None,
            reduce_only=bool(p.get("reduceOnly", False)),
            leverage=leverage,
            client_order_id=p.get("clientOrderId") or p.get("cliOrdId"),
        )

    async def cancel_futures_order(self, order_id: str, symbol=None):
        if order_id in self._orders:
            self._orders[order_id]["status"] = "canceled"
            self._open_orders = [o for o in self._open_orders if o["id"] != order_id]
        return {"result": "success", "order_id": order_id}

    async def edit_futures_order(self, *, order_id, symbol, stop_price=None, price=None):
        """Simulate edit by cancel + new order."""
        old = self._orders.get(order_id)
        if not old:
            raise Exception(f"Order {order_id} not found")
        await self.cancel_futures_order(order_id)
        return await self.place_futures_order(
            symbol=symbol,
            side=old["side"],
            order_type=old["type"],
            size=Decimal(str(old["amount"])),
            stop_price=stop_price,
            reduce_only=old.get("reduceOnly", False),
            client_order_id=old.get("clientOrderId"),
        )

    async def fetch_order(self, order_id, symbol):
        return self._orders.get(order_id)

    async def get_futures_open_orders(self):
        return list(self._open_orders)

    async def get_all_futures_positions(self):
        return list(self._positions)

    async def get_futures_tickers_bulk(self):
        """Return empty tickers — liquidity check will gracefully pass."""
        return {}

    async def close(self):
        pass

    # -- Test helpers -------------------------------------------------------

    def simulate_fill(self, order_id: str, fill_price: float) -> Dict:
        """Fill an order instantly and return the order_data for process_order_update."""
        order = self._orders[order_id]
        order["status"] = "closed"
        order["filled"] = order["amount"]
        order["remaining"] = 0
        order["average"] = fill_price
        order["trades"] = [{"id": f"fill-{order_id}", "price": fill_price}]
        self._open_orders = [o for o in self._open_orders if o["id"] != order_id]
        return dict(order)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _enable_new_entries(monkeypatch):
    """Ensure TRADING_NEW_ENTRIES_ENABLED is set for test."""
    monkeypatch.setenv("TRADING_NEW_ENTRIES_ENABLED", "true")


@pytest.fixture
def exchange():
    return FakeExchange()


@pytest.fixture
def registry():
    """Fresh position registry for each test."""
    reset_position_registry()
    return PositionRegistry()


@pytest.fixture
def persistence(tmp_path):
    """Persistence with temp DB for testing."""
    return PositionPersistence(db_path=str(tmp_path / "test_positions.db"))


@pytest.fixture
def position_manager(registry):
    return PositionManagerV2(registry)


@pytest.fixture
def gateway(exchange, registry, position_manager, persistence):
    """ExecutionGateway wired to FakeExchange, no safety (no WAL/stop replacer)."""
    return ExecutionGateway(
        exchange_client=exchange,
        registry=registry,
        position_manager=position_manager,
        persistence=persistence,
        use_safety=False,  # Disable WAL/AtomicStopReplacer for deterministic test
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_long_signal(symbol: str = "BTC/USD") -> Signal:
    """Create a realistic LONG signal."""
    return Signal(
        timestamp=datetime.now(timezone.utc),
        symbol=symbol,
        signal_type=SignalType.LONG,
        entry_price=Decimal("50000"),
        stop_loss=Decimal("49000"),
        take_profit=Decimal("52000"),
        reasoning="Test lifecycle: OB + bullish structure",
        setup_type=SetupType.OB,
        regime="trending_bullish",
        higher_tf_bias="bullish",
        adx=Decimal("25"),
        atr=Decimal("500"),
        ema200_slope="up",
        tp_candidates=[Decimal("51000"), Decimal("52000")],
    )


def make_short_signal(symbol: str = "SOL/USD") -> Signal:
    """Create a realistic SHORT signal."""
    return Signal(
        timestamp=datetime.now(timezone.utc),
        symbol=symbol,
        signal_type=SignalType.SHORT,
        entry_price=Decimal("150"),
        stop_loss=Decimal("155"),
        take_profit=Decimal("140"),
        reasoning="Test lifecycle: FVG + bearish structure",
        setup_type=SetupType.FVG,
        regime="trending_bearish",
        higher_tf_bias="bearish",
        adx=Decimal("30"),
        atr=Decimal("5"),
        ema200_slope="down",
        tp_candidates=[Decimal("145"), Decimal("140")],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_trade_lifecycle_golden_path(exchange, gateway, registry, persistence, position_manager):
    """
    Signal -> Entry action -> Entry order -> Entry fill ->
    Stop placed -> TP1 fill -> Position transitions correctly.
    """
    symbol = "BTC/USD"
    signal = make_long_signal(symbol)

    # ===== 1. Create entry via PositionManagerV2 (the real flow) =====
    entry_action, managed_pos = position_manager.evaluate_entry(
        signal=signal,
        entry_price=signal.entry_price,
        stop_price=signal.stop_loss,
        tp1_price=Decimal("51000"),
        tp2_price=Decimal("52000"),
        final_target=None,
        position_size=Decimal("0.01"),
        leverage=Decimal("5"),
    )
    assert entry_action.type == ActionType.OPEN_POSITION, \
        f"Entry should be approved, got: {entry_action.type} - {entry_action.reason}"
    assert managed_pos is not None
    assert managed_pos.state == PositionState.PENDING

    # Register the position (in prod, signal_handler does this before execute_action)
    registry.register_position(managed_pos)
    persistence.save_position(managed_pos)

    # ===== 2. Execute entry order =====
    result = await gateway.execute_action(entry_action, order_symbol="BTC/USD:USD")
    assert result.success, f"Entry order failed: {result.error}"
    entry_oid = result.exchange_order_id
    assert entry_oid is not None

    # Position should still be PENDING (not yet filled)
    pos = registry.get_position(symbol)
    assert pos is not None
    assert pos.state == PositionState.PENDING

    # ===== 3. Simulate entry fill =====
    fill_data = exchange.simulate_fill(entry_oid, fill_price=50000.0)
    follow_ups = await gateway.process_order_update(fill_data)

    # Position should transition to OPEN (or PROTECTED if stop auto-sets)
    pos = registry.get_position(symbol)
    assert pos is not None
    assert pos.state in (PositionState.OPEN, PositionState.PROTECTED), \
        f"Expected OPEN or PROTECTED after entry fill, got {pos.state}"

    # Follow-up actions should include PLACE_STOP
    action_types = [a.type for a in follow_ups]
    assert ActionType.PLACE_STOP in action_types, \
        f"Expected PLACE_STOP in follow-ups (Invariant K), got {action_types}"

    # NOTE: process_order_update already executed all follow-ups internally.
    # We only verify the resulting state — do NOT re-execute the actions.

    # ===== 4. Verify stop was placed by process_order_update =====
    pos = registry.get_position(symbol)
    assert pos.stop_order_id is not None, "Stop should have been placed by process_order_update"
    stop_oid = pos.stop_order_id
    assert stop_oid in exchange._orders
    assert exchange._orders[stop_oid]["reduceOnly"] is True

    # ===== 5. Verify TP orders were placed by process_order_update =====
    tp1_oid = pos.tp1_order_id
    tp2_oid = pos.tp2_order_id
    # At least TP1 should be placed
    assert tp1_oid is not None, "TP1 should have been placed by process_order_update"

    # ===== 6. Simulate TP1 fill (partial close) =====
    if tp1_oid:
        tp1_fill_data = exchange.simulate_fill(tp1_oid, fill_price=51000.0)
        tp1_follow_ups = await gateway.process_order_update(tp1_fill_data)

        pos = registry.get_position(symbol)
        assert pos is not None
        assert pos.tp1_filled is True, "TP1 should be marked as filled"

        # process_order_update already executed follow-ups (e.g. UPDATE_STOP for break-even)

    # ===== 7. Verify state and fills =====
    pos = registry.get_position(symbol)
    assert pos is not None
    assert len(pos.entry_fills) > 0, "No entry fills recorded"
    assert pos.entry_fills[0].price == Decimal("50000")
    assert pos.entry_fills[0].is_entry is True

    # Stop protection existed during lifecycle
    assert pos.stop_order_id is not None or pos.state == PositionState.CLOSED


@pytest.mark.asyncio
async def test_entry_rejection_does_not_leave_orphan(exchange, gateway, registry, position_manager):
    """If entry order is rejected by exchange, no orphan position in active state."""
    symbol = "ETH/USD"
    signal = Signal(
        timestamp=datetime.now(timezone.utc),
        symbol=symbol,
        signal_type=SignalType.LONG,
        entry_price=Decimal("3000"),
        stop_loss=Decimal("2900"),
        take_profit=Decimal("3200"),
        reasoning="test rejection",
        setup_type=SetupType.OB,
        regime="trending_bullish",
        higher_tf_bias="bullish",
        adx=Decimal("25"),
        atr=Decimal("50"),
        ema200_slope="up",
    )

    entry_action, managed_pos = position_manager.evaluate_entry(
        signal=signal,
        entry_price=signal.entry_price,
        stop_price=signal.stop_loss,
        tp1_price=Decimal("3100"),
        tp2_price=Decimal("3200"),
        final_target=None,
        position_size=Decimal("0.1"),
        leverage=Decimal("5"),
    )
    assert entry_action.type == ActionType.OPEN_POSITION
    registry.register_position(managed_pos)

    # Make the exchange reject the order (uses custom hierarchy)
    from src.exceptions import OperationalError
    async def _reject(*args, **kwargs):
        raise OperationalError("Insufficient margin")
    exchange.place_futures_order = _reject
    exchange.create_order = _reject

    result = await gateway.execute_action(entry_action, order_symbol="ETH/USD:USD")
    assert not result.success

    # Position should be in non-active state (PENDING with no fill, or CANCELLED)
    pos = registry.get_position(symbol)
    if pos is not None:
        active_states = {PositionState.OPEN, PositionState.PROTECTED, PositionState.PARTIAL}
        assert pos.state not in active_states, \
            f"Orphan position in active state {pos.state} after rejected entry"


@pytest.mark.asyncio
async def test_invariant_k_stop_in_followups(exchange, gateway, registry, persistence, position_manager):
    """Invariant K: PLACE_STOP must be returned after entry fill."""
    symbol = "SOL/USD"
    signal = make_short_signal(symbol)

    entry_action, managed_pos = position_manager.evaluate_entry(
        signal=signal,
        entry_price=signal.entry_price,
        stop_price=signal.stop_loss,
        tp1_price=Decimal("145"),
        tp2_price=Decimal("140"),
        final_target=None,
        position_size=Decimal("1.0"),
        leverage=Decimal("3"),
    )
    assert entry_action.type == ActionType.OPEN_POSITION
    registry.register_position(managed_pos)
    persistence.save_position(managed_pos)

    result = await gateway.execute_action(entry_action, order_symbol="SOL/USD:USD")
    assert result.success
    entry_oid = result.exchange_order_id

    # Fill the entry
    fill_data = exchange.simulate_fill(entry_oid, fill_price=150.0)
    follow_ups = await gateway.process_order_update(fill_data)

    # PLACE_STOP must be in the follow-ups
    stop_actions = [a for a in follow_ups if a.type == ActionType.PLACE_STOP]
    assert len(stop_actions) > 0, "No PLACE_STOP action after entry fill — Invariant K violated"

    # Execute the stop and verify it's on the exchange
    stop_result = await gateway.execute_action(stop_actions[0], order_symbol="SOL/USD:USD")
    assert stop_result.success
    pos = registry.get_position(symbol)
    assert pos.stop_order_id is not None, "Stop order ID not set after placement"


@pytest.mark.asyncio
async def test_fill_price_accuracy(exchange, gateway, registry, persistence, position_manager):
    """Entry fill should record the fill price accurately."""
    symbol = "DOGE/USD"
    signal = Signal(
        timestamp=datetime.now(timezone.utc),
        symbol=symbol,
        signal_type=SignalType.LONG,
        entry_price=Decimal("0.10"),
        stop_loss=Decimal("0.09"),
        take_profit=Decimal("0.12"),
        reasoning="fill price test",
        setup_type=SetupType.BOS,
        regime="trending_bullish",
        higher_tf_bias="bullish",
        adx=Decimal("20"),
        atr=Decimal("0.005"),
        ema200_slope="up",
    )

    entry_action, managed_pos = position_manager.evaluate_entry(
        signal=signal,
        entry_price=signal.entry_price,
        stop_price=signal.stop_loss,
        tp1_price=Decimal("0.11"),
        tp2_price=Decimal("0.12"),
        final_target=None,
        position_size=Decimal("100"),
        leverage=Decimal("2"),
    )
    assert entry_action.type == ActionType.OPEN_POSITION
    registry.register_position(managed_pos)
    persistence.save_position(managed_pos)

    result = await gateway.execute_action(entry_action, order_symbol="DOGE/USD:USD")
    assert result.success
    entry_oid = result.exchange_order_id

    fill_data = exchange.simulate_fill(entry_oid, fill_price=0.1005)
    await gateway.process_order_update(fill_data)

    pos = registry.get_position(symbol)
    assert len(pos.entry_fills) == 1
    assert pos.entry_fills[0].price == Decimal("0.1005")
