"""
Tests for Spot DCA (daily scheduled spot purchases).

Covers:
1. Config validation and defaults
2. DCA purchase logic (balance checks, sizing, order placement)
3. Schedule calculation
4. Safety guards (min amount, reserve, max cap, dry run)
"""
import pytest
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, AsyncMock, patch

from src.config.config import SpotDCAConfig
from src.live.spot_dca import _execute_dca_purchase, _seconds_until_next_run


# ============================================================
# Fixtures
# ============================================================


def _make_dca_config(**overrides) -> SpotDCAConfig:
    defaults = dict(
        enabled=True,
        asset="SOL",
        quote_currency="USD",
        schedule_hour_utc=0,
        schedule_minute_utc=0,
        use_full_balance=True,
        min_purchase_usd=5.0,
        reserve_usd=0.0,
    )
    defaults.update(overrides)
    return SpotDCAConfig(**defaults)


def _make_lt_mock(
    free_usd: float = 100.0,
    ask_price: float = 20.0,
    order_result: dict = None,
) -> MagicMock:
    """Create a mock LiveTrading instance with client mocks."""
    lt = MagicMock()
    lt.active = True

    # Spot balance
    lt.client.get_spot_balance = AsyncMock(return_value={
        "free": {"USD": free_usd, "SOL": 5.0},
        "total": {"USD": free_usd + 50.0, "SOL": 10.0},
    })

    # Spot ticker
    lt.client.get_spot_ticker = AsyncMock(return_value={
        "ask": ask_price,
        "bid": ask_price - 0.5,
        "last": ask_price,
    })

    # Place spot order
    if order_result is None:
        order_result = {
            "id": "order-123",
            "status": "closed",
            "filled": 5.0,
            "average": ask_price,
        }
    lt.client.place_spot_order = AsyncMock(return_value=order_result)

    return lt


# ============================================================
# Test 1: Config
# ============================================================


class TestSpotDCAConfig:

    def test_default_config(self):
        cfg = SpotDCAConfig()
        assert cfg.enabled is False
        assert cfg.asset == "SOL"
        assert cfg.quote_currency == "USD"
        assert cfg.schedule_hour_utc == 0
        assert cfg.min_purchase_usd == 5.0
        assert cfg.reserve_usd == 0.0
        assert cfg.use_full_balance is True
        assert cfg.fixed_amount_usd is None
        assert cfg.max_purchase_usd is None

    def test_custom_config(self):
        cfg = SpotDCAConfig(
            enabled=True,
            asset="ETH",
            fixed_amount_usd=50.0,
            max_purchase_usd=200.0,
            reserve_usd=10.0,
        )
        assert cfg.asset == "ETH"
        assert cfg.fixed_amount_usd == 50.0
        assert cfg.max_purchase_usd == 200.0
        assert cfg.reserve_usd == 10.0

    def test_schedule_validation(self):
        cfg = SpotDCAConfig(schedule_hour_utc=23, schedule_minute_utc=59)
        assert cfg.schedule_hour_utc == 23
        assert cfg.schedule_minute_utc == 59

    def test_invalid_hour_rejected(self):
        with pytest.raises(Exception):
            SpotDCAConfig(schedule_hour_utc=24)

    def test_invalid_min_purchase_rejected(self):
        with pytest.raises(Exception):
            SpotDCAConfig(min_purchase_usd=0.0)


# ============================================================
# Test 2: Schedule calculation
# ============================================================


class TestScheduleCalculation:

    def test_seconds_until_next_run_future_today(self):
        """If target time is later today, should return positive seconds."""
        now = datetime.now(timezone.utc)
        future_hour = (now.hour + 2) % 24
        secs = _seconds_until_next_run(future_hour, 0)
        assert secs > 0
        assert secs <= 24 * 3600

    def test_seconds_until_next_run_past_today(self):
        """If target time already passed today, should schedule for tomorrow."""
        now = datetime.now(timezone.utc)
        past_hour = (now.hour - 2) % 24
        secs = _seconds_until_next_run(past_hour, 0)
        assert secs > 0
        # Should be roughly 22-24 hours away
        assert secs > 20 * 3600


# ============================================================
# Test 3: Purchase logic
# ============================================================


class TestDCAPurchaseLogic:

    @pytest.mark.asyncio
    async def test_full_balance_purchase(self):
        """Should spend full available USD balance."""
        cfg = _make_dca_config(use_full_balance=True)
        lt = _make_lt_mock(free_usd=100.0, ask_price=20.0)

        await _execute_dca_purchase(lt, cfg, "SOL/USD")

        lt.client.place_spot_order.assert_called_once()
        call_args = lt.client.place_spot_order.call_args
        assert call_args.kwargs["symbol"] == "SOL/USD"
        assert call_args.kwargs["side"] == "buy"
        assert call_args.kwargs["order_type"] == "market"
        # 100 USD / 20 price = 5.0 SOL
        assert call_args.kwargs["amount"] == Decimal("5.0000")

    @pytest.mark.asyncio
    async def test_fixed_amount_purchase(self):
        """Should spend exactly the fixed amount."""
        cfg = _make_dca_config(fixed_amount_usd=50.0, use_full_balance=False)
        lt = _make_lt_mock(free_usd=200.0, ask_price=25.0)

        await _execute_dca_purchase(lt, cfg, "SOL/USD")

        call_args = lt.client.place_spot_order.call_args
        # 50 USD / 25 price = 2.0 SOL
        assert call_args.kwargs["amount"] == Decimal("2.0000")

    @pytest.mark.asyncio
    async def test_max_cap_applied(self):
        """Should cap at max_purchase_usd."""
        cfg = _make_dca_config(use_full_balance=True, max_purchase_usd=30.0)
        lt = _make_lt_mock(free_usd=100.0, ask_price=10.0)

        await _execute_dca_purchase(lt, cfg, "SOL/USD")

        call_args = lt.client.place_spot_order.call_args
        # Capped at 30 USD / 10 price = 3.0 SOL
        assert call_args.kwargs["amount"] == Decimal("3.0000")

    @pytest.mark.asyncio
    async def test_reserve_deducted(self):
        """Should not spend the reserve amount."""
        cfg = _make_dca_config(use_full_balance=True, reserve_usd=20.0)
        lt = _make_lt_mock(free_usd=50.0, ask_price=10.0)

        await _execute_dca_purchase(lt, cfg, "SOL/USD")

        call_args = lt.client.place_spot_order.call_args
        # (50 - 20 reserve) = 30 USD / 10 price = 3.0 SOL
        assert call_args.kwargs["amount"] == Decimal("3.0000")


# ============================================================
# Test 4: Safety guards
# ============================================================


class TestDCASafetyGuards:

    @pytest.mark.asyncio
    async def test_skip_below_min_purchase(self):
        """Should skip if balance below min_purchase_usd."""
        cfg = _make_dca_config(min_purchase_usd=10.0)
        lt = _make_lt_mock(free_usd=5.0)

        await _execute_dca_purchase(lt, cfg, "SOL/USD")

        lt.client.place_spot_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_skip_zero_balance(self):
        """Should skip if no USD available."""
        cfg = _make_dca_config()
        lt = _make_lt_mock(free_usd=0.0)

        await _execute_dca_purchase(lt, cfg, "SOL/USD")

        lt.client.place_spot_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_skip_reserve_exceeds_balance(self):
        """Should skip if reserve > available balance."""
        cfg = _make_dca_config(reserve_usd=200.0)
        lt = _make_lt_mock(free_usd=100.0)

        await _execute_dca_purchase(lt, cfg, "SOL/USD")

        lt.client.place_spot_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_balance_fetch_failure_handled(self):
        """Should handle balance fetch failure gracefully."""
        cfg = _make_dca_config()
        lt = _make_lt_mock()
        lt.client.get_spot_balance = AsyncMock(side_effect=Exception("API error"))

        await _execute_dca_purchase(lt, cfg, "SOL/USD")

        lt.client.place_spot_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_ticker_fetch_failure_handled(self):
        """Should handle ticker fetch failure gracefully."""
        cfg = _make_dca_config()
        lt = _make_lt_mock()
        lt.client.get_spot_ticker = AsyncMock(side_effect=Exception("Ticker error"))

        await _execute_dca_purchase(lt, cfg, "SOL/USD")

        lt.client.place_spot_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_order_failure_handled(self):
        """Should handle order placement failure gracefully (no crash)."""
        cfg = _make_dca_config()
        lt = _make_lt_mock()
        lt.client.place_spot_order = AsyncMock(side_effect=Exception("Order failed"))

        # Should not raise
        await _execute_dca_purchase(lt, cfg, "SOL/USD")

    @pytest.mark.asyncio
    async def test_fixed_amount_capped_by_available(self):
        """Fixed amount should not exceed available balance."""
        cfg = _make_dca_config(fixed_amount_usd=500.0)
        lt = _make_lt_mock(free_usd=100.0, ask_price=10.0)

        await _execute_dca_purchase(lt, cfg, "SOL/USD")

        call_args = lt.client.place_spot_order.call_args
        # min(500, 100 available) = 100 / 10 = 10.0 SOL
        assert call_args.kwargs["amount"] == Decimal("10.0000")
