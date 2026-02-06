from datetime import datetime, timezone
from decimal import Decimal

from src.domain.models import Side
from src.execution.position_persistence import PositionPersistence
from src.execution.position_state_machine import (
    ExitReason,
    FillRecord,
    ManagedPosition,
    PositionRegistry,
    PositionState,
)


def test_load_registry_archives_stale_zero_qty_non_terminal_positions(tmp_path):
    db_path = tmp_path / "positions.db"
    persistence = PositionPersistence(db_path=str(db_path))
    registry = PositionRegistry()

    pos = ManagedPosition(
        symbol="ENA/USD",
        side=Side.SHORT,
        position_id="pos-stale-zero",
        initial_size=Decimal("111"),
        initial_entry_price=Decimal("0.1546"),
        initial_stop_price=Decimal("0.1596"),
        initial_tp1_price=Decimal("0.1490"),
        initial_tp2_price=None,
        initial_final_target=None,
    )
    pos.state = PositionState.OPEN
    pos.entry_fills.append(
        FillRecord(
            fill_id="entry-fill-1",
            order_id="entry-order-1",
            side=Side.SHORT,
            qty=Decimal("111"),
            price=Decimal("0.1546"),
            timestamp=datetime.now(timezone.utc),
            is_entry=True,
        )
    )
    pos.exit_fills.append(
        FillRecord(
            fill_id="exit-fill-1",
            order_id="exit-order-1",
            side=Side.LONG,
            qty=Decimal("111"),
            price=Decimal("0.1530"),
            timestamp=datetime.now(timezone.utc),
            is_entry=False,
        )
    )
    registry.register_position(pos)
    persistence.save_registry(registry)

    loaded = persistence.load_registry()

    assert loaded.get_position("ENA/USD") is None
    stale = [p for p in loaded._closed_positions if p.symbol == "ENA/USD"]
    assert len(stale) == 1
    assert stale[0].state == PositionState.CLOSED
    assert stale[0].exit_reason == ExitReason.RECONCILIATION
