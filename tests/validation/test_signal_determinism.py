"""
Test Suite 2: Signal Determinism Replay Test (critical).

Goal: prove signals are reproducible from the same candle data.

Method:
  Run SMCEngine.generate_signal() twice on the same candle inputs.
  Assert for each (symbol, tf, candle_close_ts):
    - same signal direction
    - same score (within epsilon)
    - same rejection reason(s), if any

  Fail = nondeterminism bug (floating math, lookahead, state leak).
"""
import pytest
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from typing import List, Tuple
from copy import deepcopy

from src.domain.models import Candle, SignalType
from src.strategy.smc_engine import SMCEngine
from src.config.config import StrategyConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SCORE_EPSILON = 1e-9  # Scores must match within this tolerance


def _make_candle(
    symbol: str,
    tf: str,
    ts: datetime,
    o: float,
    h: float,
    l: float,
    c: float,
    v: float = 1000.0,
) -> Candle:
    return Candle(
        timestamp=ts,
        symbol=symbol,
        timeframe=tf,
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(l)),
        close=Decimal(str(c)),
        volume=Decimal(str(v)),
    )


def _generate_trending_candles(
    symbol: str,
    tf: str,
    count: int,
    start_price: float,
    trend: float,  # per-bar change
    start_time: datetime,
    interval: timedelta,
) -> List[Candle]:
    """Generate a series of candles with a consistent trend."""
    candles = []
    price = start_price
    for i in range(count):
        o = price
        c = price + trend
        h = max(o, c) * 1.002  # Small wick
        l = min(o, c) * 0.998
        ts = start_time + interval * i
        candles.append(_make_candle(symbol, tf, ts, o, h, l, c, v=50000.0))
        price = c
    return candles


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def engine():
    """Fresh SMCEngine instance per test."""
    config = StrategyConfig()
    return SMCEngine(config)


@pytest.fixture
def sample_candles() -> dict:
    """
    Deterministic candle set for BTC/USD across all required timeframes.
    Creates a clear bullish trend with valid structure for signal generation.
    """
    symbol = "BTC/USD"
    base_time = datetime(2026, 1, 15, 0, 0, tzinfo=timezone.utc)

    # 1D candles: bullish regime (above EMA200 needs ~200 bars trending up)
    candles_1d = _generate_trending_candles(
        symbol, "1d", 250,
        start_price=40000.0, trend=100.0,
        start_time=base_time - timedelta(days=250),
        interval=timedelta(days=1),
    )

    # 4H candles: decision layer (OB/FVG/BOS detection needs structure)
    candles_4h = _generate_trending_candles(
        symbol, "4h", 300,
        start_price=60000.0, trend=20.0,
        start_time=base_time - timedelta(hours=300 * 4),
        interval=timedelta(hours=4),
    )

    # 1H candles: refinement (ADX filter)
    candles_1h = _generate_trending_candles(
        symbol, "1h", 300,
        start_price=64000.0, trend=5.0,
        start_time=base_time - timedelta(hours=300),
        interval=timedelta(hours=1),
    )

    # 15m candles: entry timing
    candles_15m = _generate_trending_candles(
        symbol, "15m", 300,
        start_price=65000.0, trend=1.0,
        start_time=base_time - timedelta(minutes=300 * 15),
        interval=timedelta(minutes=15),
    )

    return {
        "1d": candles_1d,
        "4h": candles_4h,
        "1h": candles_1h,
        "15m": candles_15m,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSignalDeterminism:
    """
    Run generate_signal() twice on identical inputs.
    Every output field must match exactly.
    """

    def test_same_direction_on_repeat(self, engine, sample_candles):
        """Signal direction must be identical on repeated runs."""
        sig1 = engine.generate_signal(
            "BTC/USD",
            sample_candles["1d"],
            sample_candles["4h"],
            sample_candles["1h"],
            sample_candles["15m"],
        )
        sig2 = engine.generate_signal(
            "BTC/USD",
            sample_candles["1d"],
            sample_candles["4h"],
            sample_candles["1h"],
            sample_candles["15m"],
        )
        assert sig1.signal_type == sig2.signal_type, (
            f"Direction mismatch: run1={sig1.signal_type}, run2={sig2.signal_type}"
        )

    def test_same_score_on_repeat(self, engine, sample_candles):
        """Score must be identical (within epsilon) on repeated runs."""
        sig1 = engine.generate_signal(
            "BTC/USD",
            sample_candles["1d"],
            sample_candles["4h"],
            sample_candles["1h"],
            sample_candles["15m"],
        )
        sig2 = engine.generate_signal(
            "BTC/USD",
            sample_candles["1d"],
            sample_candles["4h"],
            sample_candles["1h"],
            sample_candles["15m"],
        )
        assert abs(sig1.score - sig2.score) < _SCORE_EPSILON, (
            f"Score mismatch: run1={sig1.score}, run2={sig2.score}, "
            f"diff={abs(sig1.score - sig2.score)}"
        )

    def test_same_rejection_reasons(self, engine, sample_candles):
        """Rejection reasoning must be identical on repeated runs."""
        sig1 = engine.generate_signal(
            "BTC/USD",
            sample_candles["1d"],
            sample_candles["4h"],
            sample_candles["1h"],
            sample_candles["15m"],
        )
        sig2 = engine.generate_signal(
            "BTC/USD",
            sample_candles["1d"],
            sample_candles["4h"],
            sample_candles["1h"],
            sample_candles["15m"],
        )
        assert sig1.reasoning == sig2.reasoning, (
            f"Reasoning mismatch:\n  run1: {sig1.reasoning}\n  run2: {sig2.reasoning}"
        )

    def test_same_entry_stop_tp(self, engine, sample_candles):
        """Entry, stop, TP prices must be identical."""
        sig1 = engine.generate_signal(
            "BTC/USD",
            sample_candles["1d"],
            sample_candles["4h"],
            sample_candles["1h"],
            sample_candles["15m"],
        )
        sig2 = engine.generate_signal(
            "BTC/USD",
            sample_candles["1d"],
            sample_candles["4h"],
            sample_candles["1h"],
            sample_candles["15m"],
        )
        assert sig1.entry_price == sig2.entry_price, (
            f"Entry mismatch: {sig1.entry_price} vs {sig2.entry_price}"
        )
        assert sig1.stop_loss == sig2.stop_loss, (
            f"Stop mismatch: {sig1.stop_loss} vs {sig2.stop_loss}"
        )
        assert sig1.take_profit == sig2.take_profit, (
            f"TP mismatch: {sig1.take_profit} vs {sig2.take_profit}"
        )

    def test_deep_copy_input_prevents_state_leak(self, engine, sample_candles):
        """Deep-copying inputs before run2 must produce same results (no mutation)."""
        sig1 = engine.generate_signal(
            "BTC/USD",
            sample_candles["1d"],
            sample_candles["4h"],
            sample_candles["1h"],
            sample_candles["15m"],
        )

        # Deep copy to prove engine didn't mutate the candles
        candles_copy = {k: deepcopy(v) for k, v in sample_candles.items()}
        sig2 = engine.generate_signal(
            "BTC/USD",
            candles_copy["1d"],
            candles_copy["4h"],
            candles_copy["1h"],
            candles_copy["15m"],
        )

        assert sig1.signal_type == sig2.signal_type
        assert abs(sig1.score - sig2.score) < _SCORE_EPSILON
        assert sig1.entry_price == sig2.entry_price


class TestMultiSymbolDeterminism:
    """Running signals for different symbols must not leak state."""

    def test_no_cross_symbol_contamination(self, engine, sample_candles):
        """
        Generate signal for BTC, then ETH, then BTC again.
        The two BTC signals must be identical.
        """
        btc_sig1 = engine.generate_signal(
            "BTC/USD",
            sample_candles["1d"],
            sample_candles["4h"],
            sample_candles["1h"],
            sample_candles["15m"],
        )

        # Generate for "ETH" (reusing candles but different symbol label)
        _eth_sig = engine.generate_signal(
            "ETH/USD",
            sample_candles["1d"],
            sample_candles["4h"],
            sample_candles["1h"],
            sample_candles["15m"],
        )

        btc_sig2 = engine.generate_signal(
            "BTC/USD",
            sample_candles["1d"],
            sample_candles["4h"],
            sample_candles["1h"],
            sample_candles["15m"],
        )

        assert btc_sig1.signal_type == btc_sig2.signal_type, (
            f"Cross-symbol contamination: BTC direction changed after ETH run"
        )
        assert abs(btc_sig1.score - btc_sig2.score) < _SCORE_EPSILON, (
            f"Cross-symbol contamination: BTC score changed after ETH run"
        )


class TestFreshEngineEquivalence:
    """A fresh engine instance must produce the same result as a reused one."""

    def test_fresh_vs_reused_engine(self, sample_candles):
        config = StrategyConfig()

        engine1 = SMCEngine(config)
        sig1 = engine1.generate_signal(
            "BTC/USD",
            sample_candles["1d"],
            sample_candles["4h"],
            sample_candles["1h"],
            sample_candles["15m"],
        )

        engine2 = SMCEngine(config)
        sig2 = engine2.generate_signal(
            "BTC/USD",
            sample_candles["1d"],
            sample_candles["4h"],
            sample_candles["1h"],
            sample_candles["15m"],
        )

        assert sig1.signal_type == sig2.signal_type
        assert abs(sig1.score - sig2.score) < _SCORE_EPSILON
        assert sig1.reasoning == sig2.reasoning


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
