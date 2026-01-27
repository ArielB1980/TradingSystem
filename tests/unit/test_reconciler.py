"""
Unit tests for Position Reconciler: adopt, force_close, zombie cleanup.
"""
from __future__ import annotations

import pytest
from decimal import Decimal
from datetime import datetime, timezone
from unittest.mock import MagicMock, AsyncMock, patch

from src.reconciliation.reconciler import Reconciler, _exchange_dict_to_position
from src.domain.models import Position, Side


def _exchange_pos(symbol: str, size: float = 100.0, side: str = "short") -> dict:
    return {
        "symbol": symbol,
        "size": size,
        "side": side,
        "entryPrice": 0.02,
        "entry_price": 0.02,
        "markPrice": 0.018,
        "mark_price": 0.018,
        "liquidationPrice": 0.025,
        "liquidation_price": 0.025,
        "unrealizedPnl": -20,
        "unrealized_pnl": -20,
        "leverage": 7,
        "initialMargin": 30,
        "margin_used": 30,
    }


@pytest.mark.asyncio
async def test_reconcile_adopt_creates_managed_position():
    """Adopt path: unmanaged exchange position is saved to DB via save_position."""
    client = MagicMock()
    client.has_valid_futures_credentials = lambda: True
    client.get_all_futures_positions = AsyncMock(return_value=[
        _exchange_pos("PF_ROSEUSD", 100.0, "short"),
    ])
    config = MagicMock()
    config.reconciliation = MagicMock()
    config.reconciliation.reconcile_enabled = True
    config.reconciliation.unmanaged_position_policy = "adopt"
    config.reconciliation.unmanaged_position_adopt_place_protection = True

    with patch("src.reconciliation.reconciler.get_active_positions", return_value=[]):
        with patch("src.reconciliation.reconciler.save_position") as save_position:
            with patch("src.reconciliation.reconciler.delete_position"):
                recon = Reconciler(client, config, place_futures_order_fn=None, place_protection_callback=None)
                summary = await recon.reconcile_all()

    assert summary["adopted"] == 1
    assert summary["on_exchange"] == 1
    assert summary["zombies_cleaned"] == 0
    assert save_position.call_count == 1
    pos = save_position.call_args[0][0]
    assert pos.symbol == "PF_ROSEUSD"
    assert pos.side == Side.SHORT
    assert float(pos.size) == 100.0


def test_exchange_dict_to_position_respects_position_size_is_notional():
    """Adopt uses config.exchange.position_size_is_notional for size_notional."""
    # Contracts (default): notional = size * mark_price
    cfg_contracts = MagicMock()
    cfg_contracts.exchange = MagicMock()
    cfg_contracts.exchange.position_size_is_notional = False
    pos_c = _exchange_dict_to_position(_exchange_pos("X", 10.0), cfg_contracts)
    assert float(pos_c.size_notional) == pytest.approx(10.0 * 0.018, rel=1e-6)
    # Notional: exchange size is already USD notional
    cfg_notional = MagicMock()
    cfg_notional.exchange = MagicMock()
    cfg_notional.exchange.position_size_is_notional = True
    pos_n = _exchange_dict_to_position(_exchange_pos("X", 500.0), cfg_notional)
    assert float(pos_n.size_notional) == 500.0


@pytest.mark.asyncio
async def test_reconcile_force_close_calls_place_futures_order():
    """Force_close path calls place_futures_order with reduce_only."""
    client = MagicMock()
    client.has_valid_futures_credentials = lambda: True
    client.get_all_futures_positions = AsyncMock(return_value=[
        _exchange_pos("PF_XBTUSD", 0.1, "long"),
    ])
    config = MagicMock()
    config.reconciliation = MagicMock()
    config.reconciliation.reconcile_enabled = True
    config.reconciliation.unmanaged_position_policy = "force_close"
    place_fn = AsyncMock(return_value=None)

    with patch("src.reconciliation.reconciler.get_active_positions", return_value=[]):
        with patch("src.reconciliation.reconciler.save_position"):
            with patch("src.reconciliation.reconciler.delete_position"):
                recon = Reconciler(client, config, place_futures_order_fn=place_fn, place_protection_callback=None)
                summary = await recon.reconcile_all()

    assert summary["force_closed"] == 1
    assert place_fn.call_count == 1
    call_kw = place_fn.call_args[1] if place_fn.call_args[1] else {}
    assert call_kw.get("reduce_only") is True
    assert call_kw.get("side") == "sell"  # close long
    assert call_kw.get("order_type") == "market"


@pytest.mark.asyncio
async def test_reconcile_zombie_cleanup_removes_tracked_absent_on_exchange():
    """Zombie cleanup: internally tracked position not on exchange is removed from DB."""
    client = MagicMock()
    client.has_valid_futures_credentials = lambda: True
    client.get_all_futures_positions = AsyncMock(return_value=[])  # exchange has nothing
    config = MagicMock()
    config.reconciliation = MagicMock()
    config.reconciliation.reconcile_enabled = True
    config.reconciliation.unmanaged_position_policy = "adopt"

    db_pos = Position(
        symbol="PF_EURUSD",
        side=Side.LONG,
        size=Decimal("10"),
        size_notional=Decimal("1000"),
        entry_price=Decimal("100"),
        current_mark_price=Decimal("100"),
        liquidation_price=Decimal("80"),
        unrealized_pnl=Decimal("0"),
        leverage=Decimal("1"),
        margin_used=Decimal("100"),
        opened_at=datetime.now(timezone.utc),
    )

    with patch("src.reconciliation.reconciler.get_active_positions", return_value=[db_pos]):
        with patch("src.reconciliation.reconciler.save_position"):
            with patch("src.reconciliation.reconciler.delete_position") as delete_position:
                recon = Reconciler(client, config)
                summary = await recon.reconcile_all()

    assert summary["zombies_cleaned"] >= 1
    assert delete_position.call_count >= 1
    # delete_position is called with symbol; exact symbol may be normalized in reconciler
    delete_position.assert_any_call("PF_EURUSD")
