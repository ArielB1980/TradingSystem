"""
Unit tests for src/data/data_sanity.py -- two-stage stateless sanity gate.

Tests are parameterized to cover:
  - Stage A (ticker): spread, volume, missing data, spot fallback
  - Stage B (candle): count, freshness, timeframe-derived max age
"""
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from src.data.data_sanity import (
    SanityThresholds,
    SanityResult,
    check_ticker_sanity,
    check_candle_sanity,
    _max_candle_age_hours,
    TF_DURATION_HOURS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_futures_ticker(
    *,
    bid: Decimal = Decimal("100"),
    ask: Decimal = Decimal("101"),
    volume_24h: Decimal = Decimal("500000"),
) -> MagicMock:
    """Minimal FuturesTicker-like object."""
    t = MagicMock()
    t.bid = bid
    t.ask = ask
    t.volume_24h = volume_24h
    t.spread_pct = (ask - bid) / bid if bid > 0 else Decimal("1")
    return t


def _make_candle(hours_ago: float = 0):
    c = MagicMock()
    c.timestamp = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return c


def _make_candle_manager(candles_by_tf: dict) -> MagicMock:
    cm = MagicMock()
    cm.get_candles = lambda sym, tf: candles_by_tf.get(tf, [])
    return cm


# ---------------------------------------------------------------------------
# _max_candle_age_hours
# ---------------------------------------------------------------------------

class TestMaxCandleAge:
    """Verify the derived max-age formula: max(2*tf, tf+1)."""

    @pytest.mark.parametrize("tf,expected", [
        ("15m", 1.25),   # max(0.5, 1.25) = 1.25
        ("1h", 2.0),     # max(2.0, 2.0) = 2.0
        ("4h", 8.0),     # max(8.0, 5.0) = 8.0
        ("1d", 48.0),    # max(48.0, 25.0) = 48.0
    ])
    def test_known_timeframes(self, tf, expected):
        assert _max_candle_age_hours(tf) == expected

    def test_unknown_timeframe_falls_back_to_4h(self):
        # Unknown tf uses default 4.0
        assert _max_candle_age_hours("3m") == 8.0


# ---------------------------------------------------------------------------
# Stage A -- check_ticker_sanity
# ---------------------------------------------------------------------------

class TestCheckTickerSanity:
    """Stage A: futures spread + volume."""

    def _thresholds(self, **kwargs) -> SanityThresholds:
        return SanityThresholds(**kwargs)

    # -- PASS cases --

    def test_pass_healthy_futures_ticker(self):
        ft = _make_futures_ticker(
            bid=Decimal("100"), ask=Decimal("101"), volume_24h=Decimal("50000"),
        )
        result = check_ticker_sanity("BTC/USD", ft, None, self._thresholds())
        assert result.passed
        assert result.reason == ""

    def test_pass_just_under_spread_limit(self):
        """Spread at 9.99% should pass (limit is 10%)."""
        bid = Decimal("100")
        ask = bid * Decimal("1.0999")
        ft = _make_futures_ticker(bid=bid, ask=ask, volume_24h=Decimal("50000"))
        result = check_ticker_sanity("X/USD", ft, None, self._thresholds())
        assert result.passed

    def test_pass_exactly_at_volume_floor(self):
        """Volume exactly $10,000 should pass."""
        ft = _make_futures_ticker(volume_24h=Decimal("10000"))
        result = check_ticker_sanity("X/USD", ft, None, self._thresholds())
        assert result.passed

    # -- FAIL cases --

    def test_fail_no_futures_no_spot(self):
        result = check_ticker_sanity("X/USD", None, None, self._thresholds())
        assert not result.passed
        assert "no_futures_ticker" in result.reason

    def test_fail_no_futures_spot_present_but_fallback_off(self):
        spot = {"bid": 100, "ask": 101, "quoteVolume": 50000}
        result = check_ticker_sanity("X/USD", None, spot, self._thresholds(allow_spot_fallback=False))
        assert not result.passed
        assert "no_futures_ticker" in result.reason

    def test_fail_spread_too_wide(self):
        ft = _make_futures_ticker(
            bid=Decimal("100"), ask=Decimal("120"), volume_24h=Decimal("50000"),
        )
        result = check_ticker_sanity("X/USD", ft, None, self._thresholds())
        assert not result.passed
        assert "spread" in result.reason

    def test_fail_spread_exactly_at_limit(self):
        """Spread at exactly 10% should fail (>= check)."""
        bid = Decimal("100")
        ask = bid * Decimal("1.10")
        ft = _make_futures_ticker(bid=bid, ask=ask, volume_24h=Decimal("50000"))
        result = check_ticker_sanity("X/USD", ft, None, self._thresholds())
        assert not result.passed
        assert "spread" in result.reason

    def test_fail_volume_below_floor(self):
        ft = _make_futures_ticker(volume_24h=Decimal("9999"))
        result = check_ticker_sanity("X/USD", ft, None, self._thresholds())
        assert not result.passed
        assert "volume" in result.reason

    def test_fail_zero_bid(self):
        ft = _make_futures_ticker(bid=Decimal("0"), ask=Decimal("100"), volume_24h=Decimal("50000"))
        # spread_pct = fallback 1 (100%), so spread check fails
        result = check_ticker_sanity("X/USD", ft, None, self._thresholds())
        assert not result.passed

    # -- Spot fallback cases --

    def test_spot_fallback_passes_when_enabled(self):
        spot = {"bid": 100, "ask": 101, "quoteVolume": 50000}
        result = check_ticker_sanity(
            "X/USD", None, spot, self._thresholds(allow_spot_fallback=True),
        )
        assert result.passed

    def test_spot_fallback_fails_on_spread(self):
        spot = {"bid": 100, "ask": 200, "quoteVolume": 50000}
        result = check_ticker_sanity(
            "X/USD", None, spot, self._thresholds(allow_spot_fallback=True),
        )
        assert not result.passed
        assert "spread" in result.reason

    def test_spot_fallback_fails_on_volume(self):
        spot = {"bid": 100, "ask": 101, "quoteVolume": 5}
        result = check_ticker_sanity(
            "X/USD", None, spot, self._thresholds(allow_spot_fallback=True),
        )
        assert not result.passed
        assert "volume" in result.reason


# ---------------------------------------------------------------------------
# Stage B -- check_candle_sanity
# ---------------------------------------------------------------------------

class TestCheckCandleSanity:
    """Stage B: candle count + freshness on decision TF."""

    def _thresholds(self, **kwargs) -> SanityThresholds:
        defaults = {"min_decision_tf_candles": 250, "decision_tf": "4h"}
        defaults.update(kwargs)
        return SanityThresholds(**defaults)

    def test_pass_sufficient_candles_and_fresh(self):
        # Oldest first, newest last (candles[-1] is newest = 0h ago)
        candles = [_make_candle(hours_ago=(259 - i) * 4) for i in range(260)]
        cm = _make_candle_manager({"4h": candles})
        result = check_candle_sanity("X/USD", cm, self._thresholds())
        assert result.passed

    def test_fail_insufficient_candles(self):
        candles = [_make_candle(hours_ago=(99 - i) * 4) for i in range(100)]
        cm = _make_candle_manager({"4h": candles})
        result = check_candle_sanity("X/USD", cm, self._thresholds())
        assert not result.passed
        assert "candles_4h=100" in result.reason

    def test_fail_stale_candles(self):
        """Newest candle is 12 hours old; max for 4h is 8h."""
        candles = [_make_candle(hours_ago=12 + (259 - i) * 4) for i in range(260)]
        cm = _make_candle_manager({"4h": candles})
        result = check_candle_sanity("X/USD", cm, self._thresholds())
        assert not result.passed
        assert "candle_age_4h" in result.reason

    def test_pass_just_within_freshness(self):
        """Newest candle is 7 hours old; max for 4h is 8h -- should pass."""
        candles = [_make_candle(hours_ago=7 + (259 - i) * 4) for i in range(260)]
        cm = _make_candle_manager({"4h": candles})
        result = check_candle_sanity("X/USD", cm, self._thresholds())
        assert result.passed

    def test_empty_candles(self):
        cm = _make_candle_manager({"4h": []})
        result = check_candle_sanity("X/USD", cm, self._thresholds())
        assert not result.passed
        assert "candles_4h=0" in result.reason

    def test_1h_decision_tf(self):
        """Verify correct freshness calculation for 1h TF (max age = 2h)."""
        # Newest = 0h ago (last element)
        candles = [_make_candle(hours_ago=(259 - i)) for i in range(260)]
        cm = _make_candle_manager({"1h": candles})
        thresholds = self._thresholds(decision_tf="1h")
        result = check_candle_sanity("X/USD", cm, thresholds)
        assert result.passed

    def test_1h_stale(self):
        """1h candle 3 hours old, max age 2h -- should fail."""
        # Newest = 3h ago
        candles = [_make_candle(hours_ago=3 + (259 - i)) for i in range(260)]
        cm = _make_candle_manager({"1h": candles})
        thresholds = self._thresholds(decision_tf="1h")
        result = check_candle_sanity("X/USD", cm, thresholds)
        assert not result.passed

    def test_candle_without_tzinfo(self):
        """Candles with naive timestamps should be treated as UTC."""
        c = MagicMock()
        c.timestamp = datetime.utcnow()  # naive
        candles = [c] * 260
        cm = _make_candle_manager({"4h": candles})
        result = check_candle_sanity("X/USD", cm, self._thresholds())
        assert result.passed  # just created, so fresh
