from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from src.data.kraken_client import KrakenClient


def _client() -> KrakenClient:
    return KrakenClient(
        api_key="spot_key",
        api_secret="spot_secret",
        futures_api_key="fut_key",
        futures_api_secret="fut_secret",
    )


@pytest.mark.asyncio
async def test_create_order_does_not_force_default_leverage():
    client = _client()
    client.place_futures_order = AsyncMock(return_value={"id": "order-1"})

    await client.create_order(
        symbol="BTC/USD:USD",
        type="limit",
        side="buy",
        amount=1.0,
        price=50000.0,
        params={},
    )

    kwargs = client.place_futures_order.call_args.kwargs
    assert kwargs["leverage"] is None


@pytest.mark.asyncio
async def test_create_order_uses_explicit_leverage_when_provided():
    client = _client()
    client.place_futures_order = AsyncMock(return_value={"id": "order-2"})

    await client.create_order(
        symbol="BTC/USD:USD",
        type="limit",
        side="buy",
        amount=1.0,
        price=50000.0,
        params={},
        leverage=Decimal("4"),
    )

    kwargs = client.place_futures_order.call_args.kwargs
    assert kwargs["leverage"] == Decimal("4")
