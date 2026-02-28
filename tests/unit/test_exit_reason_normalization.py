from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.models import Side
from src.execution.execution_gateway import ExecutionGateway
from src.execution.position_manager_v2 import ActionType, ManagementAction
from src.execution.position_state_machine import ExitReason, ManagedPosition, PositionState
from src.live.auction_runner import _build_strategic_close_action


def _build_gateway() -> ExecutionGateway:
    client = AsyncMock()
    registry = MagicMock()
    position_manager = MagicMock()
    persistence = MagicMock()
    return ExecutionGateway(
        exchange_client=client,
        registry=registry,
        position_manager=position_manager,
        persistence=persistence,
        use_safety=False,
    )


@pytest.mark.asyncio
async def test_gateway_normalizes_none_reason_does_not_crash():
    gateway = _build_gateway()
    gateway.client.create_order.return_value = {"id": "close-1"}

    class _StubPosition:
        symbol = "HBAR/USD"
        futures_symbol = None

        def __init__(self):
            self.initiated_reason = None

        def initiate_exit(self, reason, order_id, client_order_id=None):
            self.initiated_reason = reason
            return True

    stub_position = _StubPosition()
    gateway.registry.get_position.return_value = stub_position

    action = ManagementAction(
        type=ActionType.CLOSE_FULL,
        symbol="HBAR/USD",
        reason="AUCTION_STRATEGIC_CLOSE",
        side=Side.SHORT,
        size=Decimal("10"),
        position_id="pos-1",
        client_order_id="close-client-1",
        exit_reason=None,  # Regression trigger: used to crash state-machine boundary.
    )

    result = await gateway._execute_close(action)

    assert result.success is True
    assert stub_position.initiated_reason == ExitReason.TIME_BASED


def test_state_machine_initiate_exit_none_reason_falls_back():
    position = ManagedPosition(
        symbol="HBAR/USD",
        side=Side.SHORT,
        position_id="pos-hbar",
        initial_size=Decimal("10"),
        initial_entry_price=Decimal("0.10"),
        initial_stop_price=Decimal("0.11"),
        initial_tp1_price=Decimal("0.095"),
        initial_tp2_price=None,
        initial_final_target=None,
    )
    position.state = PositionState.OPEN

    ok = position.initiate_exit(None, "close-client-2")

    assert ok is True
    assert position.exit_reason == ExitReason.TIME_BASED
    assert position.state == PositionState.EXIT_PENDING


def test_auction_runner_strategic_close_sets_reason():
    position = SimpleNamespace(
        symbol="HBAR/USD",
        side=Side.SHORT,
        remaining_qty=Decimal("10"),
        position_id="pos-hbar-2",
    )

    action = _build_strategic_close_action(position)

    assert action.type == ActionType.CLOSE_FULL
    assert action.reason == "AUCTION_STRATEGIC_CLOSE"
    assert action.exit_reason == ExitReason.TIME_BASED
