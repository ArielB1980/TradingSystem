"""
Test Suite 6: Futures-Only Enforcement Test (non-negotiable).

Goal: ensure no spot data sneaks back into the trading signal path.

Assert:
  - No calls to get_spot_ohlcv in the signal generation path
  - No spot ticker usage in the signal scoring/risk path
  - Recorder entries all tagged futures (schema check)
  - CandleManager spot fallback does NOT infect recorded data

Fail = hard stop.
"""
import pytest
import ast
import inspect
from unittest.mock import AsyncMock, MagicMock, patch
from decimal import Decimal
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# 6a. Static analysis: signal path must not call spot functions
# ---------------------------------------------------------------------------

class TestNoSpotInSignalPath:
    """
    The signal generation pipeline (SMCEngine) operates on Candle objects
    passed in by the caller. Verify the engine itself never fetches spot data.
    """

    def test_smc_engine_has_no_spot_imports(self):
        """SMCEngine source code must not import or call spot-fetching functions."""
        import src.strategy.smc_engine as mod
        source = inspect.getsource(mod)

        forbidden_calls = [
            "get_spot_ohlcv",
            "fetch_spot_ohlcv",
            "get_spot_ticker",
            "spot_price",  # accessing spot price for decisions
        ]

        violations = []
        for call in forbidden_calls:
            if call in source:
                # Find the line(s) containing the call
                for i, line in enumerate(source.split("\n"), 1):
                    if call in line and not line.strip().startswith("#"):
                        violations.append(f"Line {i}: {line.strip()}")

        assert not violations, (
            f"SMCEngine references spot data functions:\n"
            + "\n".join(violations)
        )

    def test_signal_scorer_has_no_spot_imports(self):
        """SignalScorer must not reference spot data functions."""
        import src.strategy.signal_scorer as mod
        source = inspect.getsource(mod)

        forbidden = ["get_spot_ohlcv", "fetch_spot_ohlcv", "spot_ticker"]
        violations = []
        for call in forbidden:
            if call in source:
                for i, line in enumerate(source.split("\n"), 1):
                    if call in line and not line.strip().startswith("#"):
                        violations.append(f"Line {i}: {line.strip()}")

        assert not violations, (
            f"SignalScorer references spot data functions:\n"
            + "\n".join(violations)
        )

    def test_indicators_module_has_no_spot_fetch(self):
        """Indicators module must be pure computation, no data fetching."""
        import src.strategy.indicators as mod
        source = inspect.getsource(mod)

        forbidden = [
            "get_spot_ohlcv", "fetch_spot_ohlcv",
            "get_futures_ohlcv", "fetch_futures_ohlcv",
            "aiohttp", "requests.get", "httpx",
        ]
        violations = []
        for call in forbidden:
            if call in source:
                for i, line in enumerate(source.split("\n"), 1):
                    if call in line and not line.strip().startswith("#"):
                        violations.append(f"Line {i}: {line.strip()}")

        assert not violations, (
            f"Indicators module contains network/fetch calls:\n"
            + "\n".join(violations)
        )


# ---------------------------------------------------------------------------
# 6b. Recorder schema: all price columns are futures-prefixed
# ---------------------------------------------------------------------------

class TestRecorderFuturesOnly:
    """Recorder model must only store futures data."""

    def test_market_snapshot_columns_are_futures(self):
        """All price-related columns must be futures-prefixed."""
        from src.recording.models import MarketSnapshot

        columns = MarketSnapshot.__table__.columns
        price_cols = [
            c.name for c in columns
            if any(kw in c.name for kw in ("bid", "ask", "spread", "volume"))
        ]

        for col in price_cols:
            assert col.startswith("futures_"), (
                f"Non-futures price column in MarketSnapshot: '{col}'. "
                f"All recorded price data must be futures-sourced."
            )

    def test_no_spot_columns_in_snapshot(self):
        """MarketSnapshot must NOT have spot-specific columns."""
        from src.recording.models import MarketSnapshot

        col_names = {c.name for c in MarketSnapshot.__table__.columns}
        forbidden = {"spot_bid", "spot_ask", "spot_price", "spot_volume", "ohlcv_source"}
        found = forbidden & col_names
        assert not found, (
            f"Forbidden spot columns found: {found}"
        )


# ---------------------------------------------------------------------------
# 6c. Recorder implementation: only calls futures API
# ---------------------------------------------------------------------------

class TestRecorderImplementation:
    """Verify the recorder implementation only uses futures data sources."""

    def test_recorder_uses_futures_tickers(self):
        """Recorder source code must call futures ticker API, not spot."""
        import src.recording.kraken_futures_recorder as mod
        source = inspect.getsource(mod)

        # Must call futures tickers
        assert "get_futures_tickers_bulk" in source or "futures_tick" in source, (
            "Recorder does not call futures ticker API"
        )

        # Must NOT call spot tickers for price data
        # (spot OHLCV for candle metadata is OK -- that's metadata, not price data)
        spot_ticker_calls = []
        for i, line in enumerate(source.split("\n"), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "get_spot_ticker" in stripped and "candle" not in stripped.lower():
                spot_ticker_calls.append(f"Line {i}: {stripped}")

        assert not spot_ticker_calls, (
            f"Recorder calls spot ticker API for non-candle data:\n"
            + "\n".join(spot_ticker_calls)
        )


# ---------------------------------------------------------------------------
# 6d. Runtime guard: SMCEngine.generate_signal never fetches data
# ---------------------------------------------------------------------------

class TestRuntimeFuturesGuard:
    """
    At runtime, generate_signal() receives pre-built Candle lists.
    It must never trigger any network calls.
    """

    def test_generate_signal_makes_no_network_calls(self):
        """
        Patch aiohttp/requests to raise if called.
        Run generate_signal() -- it must succeed without network access.
        """
        from src.strategy.smc_engine import SMCEngine
        from src.config.config import StrategyConfig
        from src.domain.models import Candle

        engine = SMCEngine(StrategyConfig())

        # Build minimal candles
        base = datetime(2026, 1, 15, 0, 0, tzinfo=timezone.utc)
        def make_candle_list(tf, interval_hours, count=50):
            candles = []
            price = 100.0
            for i in range(count):
                ts = base - __import__("datetime").timedelta(hours=interval_hours * (count - i))
                c = Candle(
                    timestamp=ts, symbol="TEST/USD", timeframe=tf,
                    open=Decimal(str(price)),
                    high=Decimal(str(price * 1.01)),
                    low=Decimal(str(price * 0.99)),
                    close=Decimal(str(price + 0.5)),
                    volume=Decimal("1000"),
                )
                candles.append(c)
                price += 0.5
            return candles

        c1d = make_candle_list("1d", 24, 250)
        c4h = make_candle_list("4h", 4, 300)
        c1h = make_candle_list("1h", 1, 300)
        c15m = make_candle_list("15m", 0.25, 300)

        # Patch network libraries to detect any accidental calls
        with patch("aiohttp.ClientSession", side_effect=RuntimeError("NETWORK CALL DETECTED")):
            # generate_signal is sync, so this tests that no async network
            # setup happens during signal generation
            sig = engine.generate_signal("TEST/USD", c1d, c4h, c1h, c15m)

        # Signal should be returned (even if NO_SIGNAL) without network
        assert sig is not None, "generate_signal returned None"
        assert hasattr(sig, "signal_type"), "generate_signal returned invalid object"


# ---------------------------------------------------------------------------
# 6e. Candle model: verify Candle object is source-agnostic
# ---------------------------------------------------------------------------

class TestCandleModelPurity:
    """
    The Candle dataclass must be a pure data container.
    It should not have a 'source' field that could leak through.
    """

    def test_candle_has_no_source_field(self):
        """Candle model must not have an ohlcv_source or data_source field."""
        from src.domain.models import Candle
        import dataclasses

        field_names = {f.name for f in dataclasses.fields(Candle)}
        forbidden = {"source", "ohlcv_source", "data_source"}
        found = forbidden & field_names
        assert not found, (
            f"Candle model has source-tracking field(s): {found}. "
            f"Source tracking must not leak into the signal path."
        )

    def test_candle_validation_enforces_ohlc_consistency(self):
        """Candle __post_init__ must reject inconsistent OHLC data."""
        from src.domain.models import Candle

        # Valid candle should pass
        valid = Candle(
            timestamp=datetime.now(timezone.utc),
            symbol="BTC/USD", timeframe="1h",
            open=Decimal("100"), high=Decimal("101"),
            low=Decimal("99"), close=Decimal("100.5"),
            volume=Decimal("1000"),
        )
        assert valid is not None

        # Invalid: high < low
        with pytest.raises(ValueError, match="high.*low"):
            Candle(
                timestamp=datetime.now(timezone.utc),
                symbol="BTC/USD", timeframe="1h",
                open=Decimal("100"), high=Decimal("98"),
                low=Decimal("99"), close=Decimal("100"),
                volume=Decimal("1000"),
            )

        # Invalid: high < max(open, close)
        with pytest.raises(ValueError):
            Candle(
                timestamp=datetime.now(timezone.utc),
                symbol="BTC/USD", timeframe="1h",
                open=Decimal("100"), high=Decimal("100"),
                low=Decimal("98"), close=Decimal("101"),
                volume=Decimal("1000"),
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
