"""
Test Suite 5: Filter Relaxation Impact Test.

Goal: ensure filter changes unlocked trades without junking quality.

Compare (same signal inputs):
  - Old rules (strict): min_score=65, adx_threshold=25, etc.
  - New rules (relaxed): min_score=60, adx_threshold=20, etc.

Metrics:
  - signals -> contenders -> accepted
  - average R:R
  - no explosion in immediate stop-outs (R:R quality)

Pass criteria:
  - Relaxed rules produce >= as many accepted signals
  - Avg R:R >= 1.8 (quality floor)
  - Relaxed rules do NOT accept signals with R:R < 1.0
"""
import pytest
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Tuple

from src.domain.models import Candle, Signal, SignalType, SetupType
from src.strategy.smc_engine import SMCEngine
from src.config.config import StrategyConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_candle(
    symbol: str, tf: str, ts: datetime,
    o: float, h: float, l: float, c: float, v: float = 50000.0,
) -> Candle:
    return Candle(
        timestamp=ts, symbol=symbol, timeframe=tf,
        open=Decimal(str(o)), high=Decimal(str(h)),
        low=Decimal(str(l)), close=Decimal(str(c)),
        volume=Decimal(str(v)),
    )


def _generate_candles(
    symbol: str, tf: str, count: int,
    start_price: float, trend: float,
    start_time: datetime, interval: timedelta,
) -> List[Candle]:
    candles = []
    price = start_price
    for i in range(count):
        o = price
        c = price + trend
        h = max(o, c) * 1.003
        l = min(o, c) * 0.997
        ts = start_time + interval * i
        candles.append(_make_candle(symbol, tf, ts, o, h, l, c))
        price = c
    return candles


def _build_candle_universe(symbols: List[str]) -> Dict[str, Dict[str, List[Candle]]]:
    """Build candle sets for multiple symbols."""
    base_time = datetime(2026, 1, 15, 0, 0, tzinfo=timezone.utc)
    universe = {}
    for i, symbol in enumerate(symbols):
        base_price = 100.0 + i * 50
        trend_mult = 1.0 if i % 2 == 0 else -0.5  # Alternating trends
        universe[symbol] = {
            "1d": _generate_candles(
                symbol, "1d", 250, base_price, 0.5 * trend_mult,
                base_time - timedelta(days=250), timedelta(days=1),
            ),
            "4h": _generate_candles(
                symbol, "4h", 300, base_price + 100, 0.1 * trend_mult,
                base_time - timedelta(hours=300 * 4), timedelta(hours=4),
            ),
            "1h": _generate_candles(
                symbol, "1h", 300, base_price + 120, 0.03 * trend_mult,
                base_time - timedelta(hours=300), timedelta(hours=1),
            ),
            "15m": _generate_candles(
                symbol, "15m", 300, base_price + 125, 0.01 * trend_mult,
                base_time - timedelta(minutes=300 * 15), timedelta(minutes=15),
            ),
        }
    return universe


def _run_signals(
    engine: SMCEngine,
    universe: Dict[str, Dict[str, List[Candle]]],
) -> Dict[str, Signal]:
    """Run signal generation for all symbols, return {symbol: signal}."""
    results = {}
    for symbol, candles in universe.items():
        sig = engine.generate_signal(
            symbol,
            candles["1d"],
            candles["4h"],
            candles["1h"],
            candles["15m"],
        )
        results[symbol] = sig
    return results


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

_SYMBOLS = [
    "BTC/USD", "ETH/USD", "SOL/USD", "ADA/USD", "AVAX/USD",
    "DOGE/USD", "LINK/USD", "XRP/USD", "DOT/USD", "MATIC/USD",
    "NEAR/USD", "FTM/USD", "ATOM/USD", "UNI/USD", "AAVE/USD",
]


class TestFilterRelaxationImpact:
    """Compare strict vs relaxed filter configuration."""

    @pytest.fixture
    def candle_universe(self) -> Dict[str, Dict[str, List[Candle]]]:
        return _build_candle_universe(_SYMBOLS)

    @pytest.fixture
    def strict_engine(self) -> SMCEngine:
        """Old (strict) configuration."""
        config = StrategyConfig()
        # Override to strict values
        config.adx_threshold = 25.0
        return SMCEngine(config)

    @pytest.fixture
    def relaxed_engine(self) -> SMCEngine:
        """New (relaxed) configuration."""
        config = StrategyConfig()
        # Override to relaxed values
        config.adx_threshold = 20.0
        return SMCEngine(config)

    def test_relaxed_produces_at_least_as_many_signals(
        self, strict_engine, relaxed_engine, candle_universe,
    ):
        """Relaxed rules must produce >= as many non-NO_SIGNAL signals."""
        strict_results = _run_signals(strict_engine, candle_universe)
        relaxed_results = _run_signals(relaxed_engine, candle_universe)

        strict_signals = sum(
            1 for s in strict_results.values()
            if s.signal_type != SignalType.NO_SIGNAL
        )
        relaxed_signals = sum(
            1 for s in relaxed_results.values()
            if s.signal_type != SignalType.NO_SIGNAL
        )

        # Relaxed should allow at least as many (possibly more)
        assert relaxed_signals >= strict_signals, (
            f"Relaxed rules produced FEWER signals ({relaxed_signals}) "
            f"than strict ({strict_signals}). Filter relaxation is broken."
        )

    def test_no_garbage_signals_accepted(self, relaxed_engine, candle_universe):
        """
        Even with relaxed filters, no signal with R:R < 1.0 should be produced.
        Quality floor must be maintained.
        """
        results = _run_signals(relaxed_engine, candle_universe)
        garbage = []
        for symbol, sig in results.items():
            if sig.signal_type == SignalType.NO_SIGNAL:
                continue
            if sig.take_profit and sig.stop_loss and sig.entry_price:
                tp_dist = abs(sig.take_profit - sig.entry_price)
                sl_dist = abs(sig.stop_loss - sig.entry_price)
                if sl_dist > 0:
                    rr = float(tp_dist / sl_dist)
                    if rr < 1.0:
                        garbage.append(f"{symbol}: R:R={rr:.2f}")

        assert not garbage, (
            f"Relaxed filters accepted garbage signals (R:R < 1.0):\n"
            + "\n".join(garbage)
        )

    def test_average_rr_quality_floor(self, relaxed_engine, candle_universe):
        """Average R:R of accepted signals must be >= 1.8."""
        results = _run_signals(relaxed_engine, candle_universe)
        rr_values = []
        for symbol, sig in results.items():
            if sig.signal_type == SignalType.NO_SIGNAL:
                continue
            if sig.take_profit and sig.stop_loss and sig.entry_price:
                tp_dist = abs(sig.take_profit - sig.entry_price)
                sl_dist = abs(sig.stop_loss - sig.entry_price)
                if sl_dist > 0:
                    rr_values.append(float(tp_dist / sl_dist))

        if not rr_values:
            pytest.skip("No signals generated -- cannot compute avg R:R")

        avg_rr = sum(rr_values) / len(rr_values)
        assert avg_rr >= 1.8, (
            f"Average R:R = {avg_rr:.2f} < 1.8 quality floor. "
            f"Filter relaxation may have degraded signal quality. "
            f"R:R values: {[f'{v:.2f}' for v in sorted(rr_values)]}"
        )

    def test_relaxed_does_not_flip_strict_signals(
        self, strict_engine, relaxed_engine, candle_universe,
    ):
        """
        Signals that passed strict must also pass relaxed.
        Relaxation must be a superset, not a different set.
        """
        strict_results = _run_signals(strict_engine, candle_universe)
        relaxed_results = _run_signals(relaxed_engine, candle_universe)

        flipped = []
        for symbol in _SYMBOLS:
            strict_sig = strict_results[symbol]
            relaxed_sig = relaxed_results[symbol]
            if strict_sig.signal_type != SignalType.NO_SIGNAL:
                if relaxed_sig.signal_type == SignalType.NO_SIGNAL:
                    flipped.append(
                        f"{symbol}: strict={strict_sig.signal_type.value}, "
                        f"relaxed=NO_SIGNAL"
                    )
                elif strict_sig.signal_type != relaxed_sig.signal_type:
                    flipped.append(
                        f"{symbol}: strict={strict_sig.signal_type.value}, "
                        f"relaxed={relaxed_sig.signal_type.value} (direction flip!)"
                    )

        assert not flipped, (
            f"Relaxed rules flipped/rejected signals that passed strict:\n"
            + "\n".join(flipped)
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
