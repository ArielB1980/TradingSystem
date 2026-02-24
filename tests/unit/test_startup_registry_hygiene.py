"""
Tests for the startup registry hygiene invariant:

    IF exchange has 0 positions AND 0 open orders
    AND registry has stale entries
    THEN registry.hard_reset() is called before reconciliation.

This prevents the orphan → kill-switch cascade on cold restart.
"""
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.domain.models import Side
from src.execution.execution_gateway import ExecutionGateway
from src.execution.position_state_machine import (
    ExitReason,
    ManagedPosition,
    PositionRegistry,
    PositionState,
)
from src.exceptions import InvariantError


def _make_position(symbol: str, state: PositionState = PositionState.OPEN) -> ManagedPosition:
    pos = ManagedPosition(
        symbol=symbol,
        side=Side.SHORT,
        position_id=f"pos-test-{symbol}",
        initial_size=Decimal("10"),
        initial_entry_price=Decimal("100"),
        initial_stop_price=Decimal("105"),
        initial_tp1_price=Decimal("95"),
        initial_tp2_price=Decimal("90"),
        initial_final_target=Decimal("85"),
    )
    pos.state = state
    return pos


def _build_gateway(registry: PositionRegistry) -> ExecutionGateway:
    client = AsyncMock()
    position_manager = MagicMock()
    persistence = MagicMock()
    persistence.load_registry.return_value = PositionRegistry()
    gw = ExecutionGateway(
        exchange_client=client,
        registry=registry,
        position_manager=position_manager,
        persistence=persistence,
        use_safety=False,
    )
    gw.position_manager.reconcile.return_value = []
    return gw


# ---------- hard_reset unit tests ----------


def test_hard_reset_closes_all_positions():
    """hard_reset marks every position CLOSED and clears the active dict."""
    registry = PositionRegistry()
    registry._positions["A"] = _make_position("A")
    registry._positions["B"] = _make_position("B")
    registry._pending_reversals["C"] = Side.LONG

    closed = registry.hard_reset(reason="test")

    assert len(closed) == 2
    assert len(registry._positions) == 0
    assert len(registry._pending_reversals) == 0
    for pos in closed:
        assert pos.state == PositionState.CLOSED
        assert pos.exit_reason == ExitReason.RECONCILIATION


def test_hard_reset_preserves_existing_exit_reason():
    """hard_reset doesn't overwrite an exit_reason that was already set."""
    registry = PositionRegistry()
    pos = _make_position("A")
    pos.exit_reason = ExitReason.STOP_LOSS
    registry._positions["A"] = pos

    closed = registry.hard_reset(reason="test")

    assert closed[0].exit_reason == ExitReason.STOP_LOSS


def test_hard_reset_on_empty_registry():
    """hard_reset on an already-empty registry returns an empty list."""
    registry = PositionRegistry()
    closed = registry.hard_reset(reason="noop")
    assert closed == []


# ---------- startup integration tests ----------


@pytest.mark.asyncio
async def test_stale_registry_wiped_when_exchange_flat():
    """If exchange has 0 positions + 0 orders and registry has stale entries, wipe."""
    registry = PositionRegistry()
    registry._positions["PF_XRPUSD"] = _make_position("PF_XRPUSD")
    registry._positions["PF_TONUSD"] = _make_position("PF_TONUSD")

    gw = _build_gateway(registry)
    gw.client.get_all_futures_positions.return_value = []
    gw.client.get_futures_open_orders.return_value = []

    await gw.startup()

    assert len(registry.get_all_active()) == 0
    closed_symbols = {p.symbol for p in registry._closed_positions}
    assert "PF_XRPUSD" in closed_symbols
    assert "PF_TONUSD" in closed_symbols


@pytest.mark.asyncio
async def test_registry_not_wiped_when_exchange_has_open_orders():
    """If exchange has 0 positions but resting orders, do NOT wipe."""
    registry = PositionRegistry()
    registry._positions["PF_XRPUSD"] = _make_position("PF_XRPUSD")

    gw = _build_gateway(registry)
    gw.client.get_all_futures_positions.return_value = []
    gw.client.get_futures_open_orders.return_value = [
        {"id": "order-1", "symbol": "PF_XRPUSD", "type": "stop", "side": "buy"}
    ]

    # Isolate: skip downstream to focus on hygiene decision
    with patch.object(gw, "sync_with_exchange", new_callable=AsyncMock, return_value={"issues": []}), \
         patch.object(gw, "_import_phantom_positions", new_callable=AsyncMock), \
         patch.object(gw, "_enrich_from_postgres", return_value=0):
        await gw.startup()

    # Position should still be active (hygiene skipped due to open orders)
    assert "PF_XRPUSD" in registry._positions
    assert registry._positions["PF_XRPUSD"].state == PositionState.OPEN


@pytest.mark.asyncio
async def test_registry_preserved_when_exchange_has_matching_positions():
    """If exchange has live positions, the hygiene check does NOT wipe."""
    registry = PositionRegistry()
    pos = _make_position("PF_XRPUSD")
    registry._positions["PF_XRPUSD"] = pos

    gw = _build_gateway(registry)
    gw.client.get_all_futures_positions.return_value = [
        {"symbol": "PF_XRPUSD", "side": "short", "contracts": 10}
    ]
    gw.client.get_futures_open_orders.return_value = []

    with patch.object(gw, "sync_with_exchange", new_callable=AsyncMock, return_value={"issues": []}), \
         patch.object(gw, "_import_phantom_positions", new_callable=AsyncMock), \
         patch.object(gw, "_enrich_from_postgres", return_value=0):
        await gw.startup()

    assert "PF_XRPUSD" in registry._positions
    assert pos.state == PositionState.OPEN


@pytest.mark.asyncio
async def test_empty_registry_skips_hygiene_check():
    """If registry is already empty, the hygiene check is skipped entirely."""
    registry = PositionRegistry()

    gw = _build_gateway(registry)
    gw.client.get_all_futures_positions.return_value = []
    gw.client.get_futures_open_orders.return_value = []

    await gw.startup()

    # sync_with_exchange and _import_phantom_positions each call
    # get_all_futures_positions once. Hygiene check should NOT add
    # an extra call since registry was empty.
    assert gw.client.get_all_futures_positions.call_count == 2


@pytest.mark.asyncio
async def test_stale_registry_adds_extra_exchange_calls():
    """If registry has stale entries, hygiene check adds exchange queries."""
    registry = PositionRegistry()
    registry._positions["PF_XRPUSD"] = _make_position("PF_XRPUSD")

    gw = _build_gateway(registry)
    gw.client.get_all_futures_positions.return_value = []
    gw.client.get_futures_open_orders.return_value = []

    await gw.startup()

    # hygiene positions (1) + hygiene orders (1) + sync_with_exchange positions+orders (2)
    # + _import_phantom_positions positions+orders (2) = varies, but positions >= 3
    assert gw.client.get_all_futures_positions.call_count >= 3


@pytest.mark.asyncio
async def test_hygiene_check_failure_falls_through():
    """If the hygiene exchange query fails, proceed with normal reconciliation."""
    registry = PositionRegistry()
    registry._positions["PF_XRPUSD"] = _make_position("PF_XRPUSD")

    gw = _build_gateway(registry)

    call_count = 0

    async def positions_side_effect():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ConnectionError("API down")
        return []

    gw.client.get_all_futures_positions.side_effect = positions_side_effect
    gw.client.get_futures_open_orders.return_value = []

    await gw.startup()

    # Hygiene failed on first call, but startup continued
    assert call_count >= 2


@pytest.mark.asyncio
async def test_wiped_positions_are_persisted():
    """Wiped positions are saved to persistence with CLOSED state."""
    registry = PositionRegistry()
    registry._positions["PF_XRPUSD"] = _make_position("PF_XRPUSD")

    gw = _build_gateway(registry)
    gw.client.get_all_futures_positions.return_value = []
    gw.client.get_futures_open_orders.return_value = []

    await gw.startup()

    saved = [
        call.args[0]
        for call in gw.persistence.save_position.call_args_list
        if hasattr(call.args[0], "symbol") and call.args[0].symbol == "PF_XRPUSD"
    ]
    assert len(saved) >= 1
    assert saved[0].state == PositionState.CLOSED


@pytest.mark.asyncio
async def test_startup_fails_fast_on_registry_audit_violation():
    """Startup must fail if closed history contains non-terminal states."""
    registry = PositionRegistry()
    bad = _make_position("PF_BADUSD")
    bad.state = PositionState.OPEN
    registry._closed_positions.append(bad)

    gw = _build_gateway(registry)
    gw.client.get_all_futures_positions.return_value = []
    gw.client.get_futures_open_orders.return_value = []

    with pytest.raises(InvariantError, match="Registry audit failed"):
        await gw.startup()
