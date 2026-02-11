"""
Integration tests for reconciliation flow (mocked exchange).
"""
import pytest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from src.reconciliation.reconciler import Reconciler, _exchange_dict_to_position
from src.domain.models import Position, Side


def test_exchange_dict_to_position():
    """Parse exchange position dict into Position model."""
    data = {
        "symbol": "PF_XBTUSD",
        "side": "long",
        "size": 0.1,
        "entryPrice": 50000,
        "markPrice": 50100,
        "liquidationPrice": 40000,
        "unrealizedPnl": 10,
        "leverage": 5,
        "initialMargin": 1000,
    }
    pos = _exchange_dict_to_position(data)
    assert pos.symbol == "PF_XBTUSD"
    assert pos.side == Side.LONG
    assert pos.size == Decimal("0.1")
    assert pos.entry_price == Decimal("50000")
    assert pos.current_mark_price == Decimal("50100")
    assert pos.liquidation_price == Decimal("40000")


@pytest.mark.asyncio
async def test_reconciler_skips_fetch_when_no_futures_credentials():
    """Reconciler returns summary with 0 on_exchange when client has no futures credentials."""
    client = MagicMock()
    client.has_valid_futures_credentials.return_value = False
    config = MagicMock()
    config.reconciliation = MagicMock()
    config.reconciliation.reconcile_enabled = True
    config.reconciliation.unmanaged_position_policy = "adopt"
    reconciler = Reconciler(client, config)
    with patch("src.reconciliation.reconciler.get_active_positions", return_value=[]):
        result = await reconciler.reconcile_all()
    assert result is not None
    assert result["on_exchange"] == 0
    assert not client.get_all_futures_positions.called
