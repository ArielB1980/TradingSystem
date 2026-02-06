from datetime import datetime, timezone
from decimal import Decimal

from src.domain.models import Side
from src.execution.position_state_machine import (
    ExitReason,
    ManagedPosition,
    OrderEvent,
    OrderEventType,
    PositionState,
)


def _base_position() -> ManagedPosition:
    return ManagedPosition(
        symbol="BTC/USD:USD",
        side=Side.LONG,
        position_id="pos-test-identity",
        initial_size=Decimal("1"),
        initial_entry_price=Decimal("50000"),
        initial_stop_price=Decimal("49000"),
        initial_tp1_price=None,
        initial_tp2_price=None,
        initial_final_target=None,
    )


def test_entry_fill_matches_client_order_id_when_exchange_order_id_differs():
    pos = _base_position()
    pos.entry_order_id = "entry-pos-test-identity"
    pos.entry_client_order_id = "entry-pos-test-identity"

    # Exchange emits its own order id while clientOrderId remains our entry id.
    event = OrderEvent(
        order_id="exchange-entry-123",
        client_order_id="entry-pos-test-identity",
        event_type=OrderEventType.FILLED,
        event_seq=1,
        timestamp=datetime.now(timezone.utc),
        fill_qty=Decimal("1"),
        fill_price=Decimal("50000"),
    )

    changed = pos.apply_order_event(event)

    assert changed is True
    assert pos.state == PositionState.OPEN
    assert pos.filled_entry_qty == Decimal("1")
    assert pos.remaining_qty == Decimal("1")


def test_exit_fill_matches_pending_exit_client_order_id():
    pos = _base_position()
    pos.entry_order_id = "entry-pos-test-identity"
    pos.entry_client_order_id = "entry-pos-test-identity"

    entry_fill = OrderEvent(
        order_id="entry-pos-test-identity",
        client_order_id="entry-pos-test-identity",
        event_type=OrderEventType.FILLED,
        event_seq=1,
        timestamp=datetime.now(timezone.utc),
        fill_qty=Decimal("1"),
        fill_price=Decimal("50000"),
    )
    assert pos.apply_order_event(entry_fill) is True
    assert pos.state == PositionState.OPEN

    # Exit is initiated with client id first, then exchange id is returned later.
    assert pos.initiate_exit(
        ExitReason.MANUAL,
        order_id="exit-client-1",
        client_order_id="exit-client-1",
    ) is True
    assert pos.state == PositionState.EXIT_PENDING

    exit_fill = OrderEvent(
        order_id="exchange-exit-1",
        client_order_id="exit-client-1",
        event_type=OrderEventType.FILLED,
        event_seq=2,
        timestamp=datetime.now(timezone.utc),
        fill_qty=Decimal("1"),
        fill_price=Decimal("50100"),
    )
    changed = pos.apply_order_event(exit_fill)

    assert changed is True
    assert pos.state == PositionState.CLOSED
    assert pos.remaining_qty == Decimal("0")
