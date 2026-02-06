from decimal import Decimal

from src.domain.models import Side
from src.live.live_trading import LiveTrading


def test_init_managed_position_uses_explicit_side_for_short_when_size_is_positive():
    # Build a lightweight instance without running full LiveTrading initialization.
    live = LiveTrading.__new__(LiveTrading)

    exchange_data = {
        "symbol": "PF_ETHUSD",
        "size": "1.5",  # normalized absolute size
        "side": "short",
        "entry_price": "3000",
        "liquidationPrice": "3500",
        "unrealizedPnl": "0",
    }

    pos = live._init_managed_position(
        exchange_data=exchange_data,
        mark_price=Decimal("3010"),
        db_pos=None,
        orders_for_symbol=[],
    )

    assert pos.side == Side.SHORT
