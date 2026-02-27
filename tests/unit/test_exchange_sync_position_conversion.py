from datetime import datetime, timezone
from decimal import Decimal

from src.live.exchange_sync import convert_to_position


def test_convert_to_position_uses_exchange_open_timestamp_ms():
    opened_at_ms = 1_700_000_000_000  # 2023-11-14T22:13:20Z
    pos = convert_to_position(
        lt=None,  # not used by convert_to_position
        data={
            "symbol": "PF_SOLUSD",
            "side": "long",
            "size": "10",
            "entryPrice": "100",
            "markPrice": "105",
            "initialMargin": "210",
            "openTime": opened_at_ms,
        },
    )

    assert pos.symbol == "PF_SOLUSD"
    assert pos.margin_used == Decimal("210")
    assert pos.opened_at == datetime.fromtimestamp(opened_at_ms / 1000.0, tz=timezone.utc)


def test_convert_to_position_defaults_opened_at_when_missing():
    before = datetime.now(timezone.utc)
    pos = convert_to_position(
        lt=None,  # not used by convert_to_position
        data={
            "symbol": "PF_ETHUSD",
            "side": "short",
            "size": "2",
            "entryPrice": "2000",
            "markPrice": "1990",
            "initialMargin": "300",
        },
    )
    after = datetime.now(timezone.utc)

    assert before <= pos.opened_at <= after
