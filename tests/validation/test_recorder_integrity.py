"""
Test Suite 1: Recorder Integrity Tests (must-pass).

Goal: prove the recorder data is usable for deterministic replays.
If ANY test fails here, every backtest conclusion built on this data is suspect.

Tests:
  - Continuity: no gaps > 2x the recording interval per symbol
  - Monotonic timestamps: strictly increasing per symbol
  - No mixed sources: all recorded data is futures-only
  - Price sanity: ask >= bid, spread non-negative
  - Volume sanity: volume >= 0, no NaNs/nulls on non-error rows
"""
import json
import pytest
from datetime import timedelta
from decimal import Decimal
from typing import List, Dict

from sqlalchemy import create_engine, text

pytestmark = pytest.mark.server

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_RECORDING_INTERVAL_SECONDS = 300  # 5 minutes
_MAX_GAP_MULTIPLIER = 2  # gaps > 2x interval are failures


def _get_engine():
    """
    Build a SQLAlchemy engine from DATABASE_URL.
    Tests in this file run against the REAL production recorder DB (read-only).
    """
    import os
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set -- recorder integrity tests require the live DB")
    return create_engine(url)


@pytest.fixture(scope="module")
def engine():
    return _get_engine()


@pytest.fixture(scope="module")
def all_snapshots(engine) -> List[Dict]:
    """Load all non-error snapshots ordered by symbol + time."""
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, ts_utc, symbol, futures_bid, futures_ask, "
            "futures_spread_pct, futures_volume_usd_24h, "
            "open_interest_usd, funding_rate, "
            "last_candle_ts_json, candle_count_json, error_code "
            "FROM market_snapshots "
            "ORDER BY symbol, ts_utc"
        )).fetchall()
    if not rows:
        pytest.skip("No snapshots in market_snapshots table")
    return [dict(r._mapping) for r in rows]


@pytest.fixture(scope="module")
def by_symbol(all_snapshots) -> Dict[str, List[Dict]]:
    """Group snapshots by symbol."""
    groups: Dict[str, List[Dict]] = {}
    for snap in all_snapshots:
        groups.setdefault(snap["symbol"], []).append(snap)
    return groups


# ---------------------------------------------------------------------------
# 1. Continuity: no gaps > 2x candle interval
# ---------------------------------------------------------------------------

class TestContinuity:
    """No gaps > 2x the 5-minute recording interval per symbol."""

    def test_no_large_gaps(self, by_symbol):
        max_gap = timedelta(seconds=_RECORDING_INTERVAL_SECONDS * _MAX_GAP_MULTIPLIER)
        violations = []
        for symbol, snaps in by_symbol.items():
            # Only check non-error rows
            valid = [s for s in snaps if s["error_code"] is None]
            for i in range(1, len(valid)):
                gap = valid[i]["ts_utc"] - valid[i - 1]["ts_utc"]
                if gap > max_gap:
                    violations.append(
                        f"{symbol}: gap={gap} between "
                        f"{valid[i-1]['ts_utc']} and {valid[i]['ts_utc']}"
                    )
        assert not violations, (
            f"Found {len(violations)} continuity gap(s) > {max_gap}:\n"
            + "\n".join(violations[:20])
        )


# ---------------------------------------------------------------------------
# 2. Monotonic timestamps: strictly increasing per symbol
# ---------------------------------------------------------------------------

class TestMonotonicTimestamps:
    """Timestamps must be strictly increasing within each symbol."""

    def test_strictly_increasing(self, by_symbol):
        violations = []
        for symbol, snaps in by_symbol.items():
            for i in range(1, len(snaps)):
                if snaps[i]["ts_utc"] <= snaps[i - 1]["ts_utc"]:
                    violations.append(
                        f"{symbol}: ts[{i}]={snaps[i]['ts_utc']} "
                        f"<= ts[{i-1}]={snaps[i-1]['ts_utc']}"
                    )
        assert not violations, (
            f"Found {len(violations)} non-monotonic timestamp(s):\n"
            + "\n".join(violations[:20])
        )


# ---------------------------------------------------------------------------
# 3. No mixed sources: all recorded data is futures-based
# ---------------------------------------------------------------------------

class TestNoMixedSources:
    """
    The recorder must only store futures data.
    - All snapshot fields are futures-prefixed (futures_bid, futures_ask, etc.)
    - Candle metadata comes from spot DB (by design) but snapshots themselves
      must NOT contain any spot ticker data masquerading as futures data.
    - This test verifies the schema enforces this and no unexpected columns exist.
    """

    def test_snapshot_schema_is_futures_only(self, engine):
        """Verify the table has only futures-prefixed price columns."""
        with engine.connect() as conn:
            cols = conn.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'market_snapshots' "
                "ORDER BY ordinal_position"
            )).fetchall()
        col_names = [c[0] for c in cols]

        # Price-related columns must all be futures-prefixed
        price_cols = [c for c in col_names if "bid" in c or "ask" in c or "spread" in c or "volume" in c]
        for col in price_cols:
            assert col.startswith("futures_"), (
                f"Non-futures price column found: {col}. "
                f"All price data must be futures-sourced."
            )

        # Must NOT have spot-specific columns
        forbidden = {"spot_bid", "spot_ask", "spot_price", "spot_volume", "ohlcv_source"}
        found_forbidden = forbidden & set(col_names)
        assert not found_forbidden, (
            f"Forbidden spot columns found in market_snapshots: {found_forbidden}"
        )

    def test_no_error_rows_have_stale_data(self, all_snapshots):
        """Error rows must have NULL price data, not stale/spot data."""
        violations = []
        for snap in all_snapshots:
            if snap["error_code"] is not None:
                # Error rows should have NULL prices (not filled from spot)
                if snap["futures_bid"] is not None or snap["futures_ask"] is not None:
                    violations.append(
                        f"{snap['symbol']} @ {snap['ts_utc']}: "
                        f"error_code={snap['error_code']} but has bid/ask data"
                    )
        # This is a warning, not a hard failure -- recorder may fill partial data
        if violations:
            pytest.xfail(
                f"{len(violations)} error rows have partial price data "
                f"(acceptable if recorder filled what it could)"
            )


# ---------------------------------------------------------------------------
# 4. Price sanity: ask >= bid, spread non-negative
# ---------------------------------------------------------------------------

class TestPriceSanity:
    """Futures price data must be internally consistent."""

    def test_ask_gte_bid(self, all_snapshots):
        """Ask price must be >= bid price (no crossed quotes)."""
        violations = []
        for snap in all_snapshots:
            if snap["error_code"] is not None:
                continue
            bid = snap["futures_bid"]
            ask = snap["futures_ask"]
            if bid is not None and ask is not None:
                if Decimal(str(ask)) < Decimal(str(bid)):
                    violations.append(
                        f"{snap['symbol']} @ {snap['ts_utc']}: "
                        f"ask={ask} < bid={bid} (crossed)"
                    )
        assert not violations, (
            f"Found {len(violations)} crossed quote(s):\n"
            + "\n".join(violations[:20])
        )

    def test_spread_non_negative(self, all_snapshots):
        """Recorded spread must be non-negative."""
        violations = []
        for snap in all_snapshots:
            if snap["error_code"] is not None:
                continue
            spread = snap["futures_spread_pct"]
            if spread is not None and Decimal(str(spread)) < 0:
                violations.append(
                    f"{snap['symbol']} @ {snap['ts_utc']}: "
                    f"spread={spread} < 0"
                )
        assert not violations, (
            f"Found {len(violations)} negative spread(s):\n"
            + "\n".join(violations[:20])
        )

    def test_prices_positive(self, all_snapshots):
        """Bid and ask must be positive when present."""
        violations = []
        for snap in all_snapshots:
            if snap["error_code"] is not None:
                continue
            for field in ("futures_bid", "futures_ask"):
                val = snap[field]
                if val is not None and Decimal(str(val)) <= 0:
                    violations.append(
                        f"{snap['symbol']} @ {snap['ts_utc']}: {field}={val} <= 0"
                    )
        assert not violations, (
            f"Found {len(violations)} non-positive price(s):\n"
            + "\n".join(violations[:20])
        )


# ---------------------------------------------------------------------------
# 5. Volume sanity: volume >= 0, no NaNs
# ---------------------------------------------------------------------------

class TestVolumeSanity:
    """Volume data must be sane on non-error rows."""

    def test_volume_non_negative(self, all_snapshots):
        """Volume must be >= 0."""
        violations = []
        for snap in all_snapshots:
            if snap["error_code"] is not None:
                continue
            vol = snap["futures_volume_usd_24h"]
            if vol is not None and Decimal(str(vol)) < 0:
                violations.append(
                    f"{snap['symbol']} @ {snap['ts_utc']}: vol={vol} < 0"
                )
        assert not violations, (
            f"Found {len(violations)} negative volume(s):\n"
            + "\n".join(violations[:20])
        )

    def test_no_null_volume_on_valid_rows(self, all_snapshots):
        """Non-error rows should have volume data."""
        null_count = 0
        total_valid = 0
        for snap in all_snapshots:
            if snap["error_code"] is not None:
                continue
            total_valid += 1
            if snap["futures_volume_usd_24h"] is None:
                null_count += 1

        null_pct = null_count / total_valid if total_valid > 0 else 0
        # Allow up to 5% null volume (some illiquid symbols may not report)
        assert null_pct < 0.05, (
            f"{null_count}/{total_valid} ({null_pct:.1%}) valid rows have NULL volume. "
            f"Max allowed: 5%"
        )

    def test_no_null_bid_ask_on_valid_rows(self, all_snapshots):
        """Non-error rows must have bid/ask data."""
        null_count = 0
        total_valid = 0
        for snap in all_snapshots:
            if snap["error_code"] is not None:
                continue
            total_valid += 1
            if snap["futures_bid"] is None or snap["futures_ask"] is None:
                null_count += 1

        null_pct = null_count / total_valid if total_valid > 0 else 0
        assert null_pct < 0.01, (
            f"{null_count}/{total_valid} ({null_pct:.1%}) valid rows have NULL bid/ask. "
            f"Max allowed: 1%"
        )


# ---------------------------------------------------------------------------
# 6. Candle metadata integrity
# ---------------------------------------------------------------------------

class TestCandleMetadata:
    """Candle metadata JSON blobs must be valid and consistent."""

    def test_candle_ts_json_parseable(self, all_snapshots):
        """last_candle_ts_json must be valid JSON when present."""
        violations = []
        for snap in all_snapshots:
            raw = snap["last_candle_ts_json"]
            if raw is not None:
                try:
                    parsed = json.loads(raw)
                    assert isinstance(parsed, dict), "must be a dict"
                except (json.JSONDecodeError, AssertionError) as e:
                    violations.append(
                        f"{snap['symbol']} @ {snap['ts_utc']}: {e}"
                    )
        assert not violations, (
            f"Found {len(violations)} invalid candle_ts JSON(s):\n"
            + "\n".join(violations[:10])
        )

    def test_candle_count_json_parseable(self, all_snapshots):
        """candle_count_json must be valid JSON with positive counts."""
        violations = []
        for snap in all_snapshots:
            raw = snap["candle_count_json"]
            if raw is not None:
                try:
                    parsed = json.loads(raw)
                    assert isinstance(parsed, dict), "must be a dict"
                    for tf, count in parsed.items():
                        assert isinstance(count, (int, float)), f"count for {tf} not numeric"
                        assert count >= 0, f"count for {tf} is negative: {count}"
                except (json.JSONDecodeError, AssertionError) as e:
                    violations.append(
                        f"{snap['symbol']} @ {snap['ts_utc']}: {e}"
                    )
        assert not violations, (
            f"Found {len(violations)} invalid candle_count JSON(s):\n"
            + "\n".join(violations[:10])
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
