"""
Smoke tests for the Record & Replay Harness.

Uses an in-memory SQLite database for speed and isolation.
Validates determinism, counter consistency, delta accuracy,
and the no-live-API guard.
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.data.data_quality_tracker import DataQualityTracker, SymbolHealthState
from src.data.data_sanity import SanityThresholds
from src.recording.models import MarketSnapshot
from src.replay.replay_candle_meta_provider import ReplayCandleMetaProvider
from src.replay.replay_ticker_provider import ReplayTickerProvider
from src.replay.report_generator import SymbolCoverageAccumulator
from src.replay.run_replay_backtest import run_pass_disabled, run_pass_enabled, run_replay
from src.storage.db import Base

# ---------------------------------------------------------------------------
# Fixture: synthetic recording in SQLite
# ---------------------------------------------------------------------------

_START = datetime(2025, 11, 6, 0, 0, 0, tzinfo=timezone.utc)
_END = datetime(2025, 11, 6, 23, 55, 0, tzinfo=timezone.utc)
_INTERVAL = 300  # 5 min
_NUM_TICKS = (24 * 60 * 60) // _INTERVAL  # 288 ticks in a day

# Decision timeframe thresholds for tests
_THRESHOLDS = SanityThresholds(
    max_spread_pct=Decimal("0.10"),
    min_volume_24h_usd=Decimal("10000"),
    min_decision_tf_candles=250,
    decision_tf="4h",
)


def _build_healthy_snapshot(symbol: str, ts: datetime) -> MarketSnapshot:
    """Build a snapshot that passes all sanity checks."""
    return MarketSnapshot(
        ts_utc=ts,
        symbol=symbol,
        futures_bid=Decimal("50000.00"),
        futures_ask=Decimal("50010.00"),       # 0.02% spread
        futures_spread_pct=Decimal("0.0002"),
        futures_volume_usd_24h=Decimal("5000000"),
        open_interest_usd=Decimal("1000000"),
        funding_rate=Decimal("0.0001"),
        last_candle_ts_json=json.dumps({
            "4h": (ts - timedelta(hours=1)).isoformat(),
            "1d": (ts - timedelta(hours=6)).isoformat(),
            "1h": (ts - timedelta(minutes=30)).isoformat(),
            "15m": (ts - timedelta(minutes=10)).isoformat(),
        }),
        candle_count_json=json.dumps({
            "4h": 260,
            "1d": 365,
            "1h": 700,
            "15m": 1200,
        }),
        error_code=None,
    )


def _build_unhealthy_snapshot(symbol: str, ts: datetime) -> MarketSnapshot:
    """Build a snapshot that fails sanity checks (huge spread, zero volume)."""
    return MarketSnapshot(
        ts_utc=ts,
        symbol=symbol,
        futures_bid=Decimal("1.000"),
        futures_ask=Decimal("2.000"),           # 100% spread
        futures_spread_pct=Decimal("1.0000"),
        futures_volume_usd_24h=Decimal("100"),  # below $10k min
        open_interest_usd=Decimal("500"),
        funding_rate=Decimal("0.001"),
        last_candle_ts_json=json.dumps({
            "4h": (ts - timedelta(hours=48)).isoformat(),  # very stale
            "1d": (ts - timedelta(hours=96)).isoformat(),
            "1h": (ts - timedelta(hours=24)).isoformat(),
            "15m": (ts - timedelta(hours=12)).isoformat(),
        }),
        candle_count_json=json.dumps({
            "4h": 5,    # way below 250 minimum
            "1d": 3,
            "1h": 10,
            "15m": 20,
        }),
        error_code=None,
    )


@pytest.fixture
def sqlite_db_url(tmp_path: Path) -> str:
    """Create an in-memory SQLite DB with synthetic snapshots."""
    db_path = tmp_path / "test_replay.db"
    url = f"sqlite:///{db_path}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine)
    session = SessionFactory()

    # Insert snapshots: 10 snapshots each for SYM_A (healthy) and SYM_B (unhealthy)
    # Spread across the day at 5-minute intervals
    for i in range(10):
        ts = _START + timedelta(minutes=i * 5 * 29)  # ~every 145 min, 10 in a day
        session.add(_build_healthy_snapshot("SYM_A", ts))
        session.add(_build_unhealthy_snapshot("SYM_B", ts))

    session.commit()
    session.close()
    return url


# ---------------------------------------------------------------------------
# Test: Deterministic replay
# ---------------------------------------------------------------------------

class TestReplayDeterministic:
    """Running the same replay twice yields byte-identical JSON reports."""

    def test_replay_deterministic(self, sqlite_db_url: str, tmp_path: Path) -> None:
        out1 = str(tmp_path / "run1")
        out2 = str(tmp_path / "run2")

        cov1, delta1 = run_replay(
            db_url=sqlite_db_url,
            start=_START,
            end=_END,
            tick_seconds=_INTERVAL,
            output_dir=out1,
            thresholds=_THRESHOLDS,
        )
        cov2, delta2 = run_replay(
            db_url=sqlite_db_url,
            start=_START,
            end=_END,
            tick_seconds=_INTERVAL,
            output_dir=out2,
            thresholds=_THRESHOLDS,
        )

        # Compare JSON output byte-for-byte (after canonical serialization)
        cov1_json = json.dumps(cov1, sort_keys=True, default=str)
        cov2_json = json.dumps(cov2, sort_keys=True, default=str)
        assert cov1_json == cov2_json, "Coverage reports differ between runs"

        delta1_json = json.dumps(delta1, sort_keys=True, default=str)
        delta2_json = json.dumps(delta2, sort_keys=True, default=str)
        assert delta1_json == delta2_json, "Delta reports differ between runs"


# ---------------------------------------------------------------------------
# Test: Coverage counters consistency
# ---------------------------------------------------------------------------

class TestCoverageCounters:
    """Verify per-symbol coverage counters are consistent."""

    def test_coverage_counters_consistent(self, sqlite_db_url: str, tmp_path: Path) -> None:
        cov, delta = run_replay(
            db_url=sqlite_db_url,
            start=_START,
            end=_END,
            tick_seconds=_INTERVAL,
            output_dir=str(tmp_path / "cov_test"),
            thresholds=_THRESHOLDS,
        )

        sym_a = cov["symbols"]["SYM_A"]
        sym_b = cov["symbols"]["SYM_B"]

        # SYM_A: healthy -- all cycles should pass
        assert sym_a["cycles_sanity_pass"] == sym_a["analyze_calls_count"]
        assert sym_a["cycles_sanity_pass"] > 0, "SYM_A should have passing cycles"
        assert sym_a["sanity_fail_count"] == 0, "SYM_A should have zero failures"

        # SYM_B: unhealthy -- should fail
        assert sym_b["sanity_fail_count"] > 0, "SYM_B should have failures"
        # SYM_B should eventually reach DEGRADED state (after 3 consecutive failures)
        has_degraded = any("DEGRADED" in k for k in sym_b["transitions_counts"])
        assert has_degraded, f"SYM_B should transition to DEGRADED; transitions: {sym_b['transitions_counts']}"

        # Global: avg symbols analyzed per cycle should be < 2 (SYM_B skipped)
        avg_analyzed = cov["global"]["avg_symbols_analyzed_per_cycle"]
        # SYM_A always analyzed + SYM_B occasionally, so avg < 2
        assert avg_analyzed < 2.0, f"Avg analyzed should be < 2, got {avg_analyzed}"

    def test_counter_arithmetic(self, sqlite_db_url: str, tmp_path: Path) -> None:
        """cycles_total = cycles_pass + fail_count + skipped_count."""
        cov, _ = run_replay(
            db_url=sqlite_db_url,
            start=_START,
            end=_END,
            tick_seconds=_INTERVAL,
            output_dir=str(tmp_path / "arith_test"),
            thresholds=_THRESHOLDS,
        )

        for sym, data in cov["symbols"].items():
            total = data["cycles_total"]
            parts = data["cycles_sanity_pass"] + data["sanity_fail_count"] + data["skipped_count"]
            assert total == parts, (
                f"{sym}: cycles_total={total} != pass+fail+skip={parts}"
            )


# ---------------------------------------------------------------------------
# Test: Delta report -- wasted work
# ---------------------------------------------------------------------------

class TestDeltaWastedWork:
    """Verify the delta report correctly identifies wasted work."""

    def test_delta_wasted_work(self, sqlite_db_url: str, tmp_path: Path) -> None:
        _, delta = run_replay(
            db_url=sqlite_db_url,
            start=_START,
            end=_END,
            tick_seconds=_INTERVAL,
            output_dir=str(tmp_path / "delta_test"),
            thresholds=_THRESHOLDS,
        )

        assert delta["disabled"]["analyze_calls"] > delta["enabled"]["analyze_calls"], (
            "Disabled should analyze more than enabled"
        )
        assert delta["wasted_work_prevented"] > 0, "Should prevent some wasted work"
        assert delta["wasted_work_pct"] > 0, "Wasted work pct should be positive"

    def test_disabled_analyzes_everything(self, sqlite_db_url: str, tmp_path: Path) -> None:
        """Pass 2 (disabled) should analyze every symbol on every tick."""
        _, delta = run_replay(
            db_url=sqlite_db_url,
            start=_START,
            end=_END,
            tick_seconds=_INTERVAL,
            output_dir=str(tmp_path / "dis_test"),
            thresholds=_THRESHOLDS,
        )

        # Each symbol gets one analyze call per tick
        # _START=00:00, _END=23:55 => 288 ticks (00:00, 00:05, ..., 23:55)
        expected_per_sym = _NUM_TICKS
        for sym, d in delta["per_symbol"].items():
            assert d["disabled_analyze_calls"] == expected_per_sym, (
                f"{sym}: expected {expected_per_sym} disabled calls, got {d['disabled_analyze_calls']}"
            )


# ---------------------------------------------------------------------------
# Test: No live API calls
# ---------------------------------------------------------------------------

class TestNoLiveAPICalls:
    """Ensure KrakenClient is never instantiated during replay."""

    def test_no_live_api_calls(self, sqlite_db_url: str, tmp_path: Path) -> None:
        # Temporarily remove KrakenClient from sys.modules if present,
        # then run replay and verify it was not re-imported with instantiation.
        had_module = "src.data.kraken_client" in sys.modules

        run_replay(
            db_url=sqlite_db_url,
            start=_START,
            end=_END,
            tick_seconds=_INTERVAL,
            output_dir=str(tmp_path / "api_test"),
            thresholds=_THRESHOLDS,
        )

        # The module may be imported (for FuturesTicker), but KrakenClient
        # should never be instantiated. We verify by checking that the
        # run_replay_backtest module does NOT import KrakenClient.
        from src.replay import run_replay_backtest as mod
        source = Path(mod.__file__).read_text()
        assert "KrakenClient(" not in source, (
            "run_replay_backtest.py must not instantiate KrakenClient"
        )
        assert "from src.data.kraken_client import KrakenClient" not in source, (
            "run_replay_backtest.py must not import KrakenClient"
        )


# ---------------------------------------------------------------------------
# Test: Report files are written
# ---------------------------------------------------------------------------

class TestReportFiles:
    """Verify that report files are actually created on disk."""

    def test_report_files_exist(self, sqlite_db_url: str, tmp_path: Path) -> None:
        out_dir = str(tmp_path / "report_files_test")
        run_replay(
            db_url=sqlite_db_url,
            start=_START,
            end=_END,
            tick_seconds=_INTERVAL,
            output_dir=out_dir,
            thresholds=_THRESHOLDS,
        )

        out_path = Path(out_dir)
        expected = [
            "coverage_2025-11-06_2025-11-06.json",
            "coverage_2025-11-06_2025-11-06_summary.txt",
            "delta_2025-11-06_2025-11-06.json",
            "delta_2025-11-06_2025-11-06_summary.txt",
        ]
        for fname in expected:
            fp = out_path / fname
            assert fp.exists(), f"Missing report file: {fp}"
            assert fp.stat().st_size > 0, f"Empty report file: {fp}"

    def test_coverage_json_parseable(self, sqlite_db_url: str, tmp_path: Path) -> None:
        out_dir = str(tmp_path / "parse_test")
        run_replay(
            db_url=sqlite_db_url,
            start=_START,
            end=_END,
            tick_seconds=_INTERVAL,
            output_dir=out_dir,
            thresholds=_THRESHOLDS,
        )

        cov_path = Path(out_dir) / "coverage_2025-11-06_2025-11-06.json"
        data = json.loads(cov_path.read_text())
        assert "symbols" in data
        assert "global" in data
        assert "SYM_A" in data["symbols"]
        assert "SYM_B" in data["symbols"]


# ---------------------------------------------------------------------------
# Fixture: dense snapshots with compressed tracker timings
# ---------------------------------------------------------------------------

# Compressed timings so SUSPENDED is reachable quickly:
#   - degraded after 3 failures
#   - suspended after 15 min (900s) of continuous failure while DEGRADED
#   - probe every 10 min (600s) = 2 ticks when SUSPENDED
#   - DEGRADED skip ratio = 2 (analyze every other cycle)
_FAST_START = datetime(2025, 12, 1, 0, 0, 0, tzinfo=timezone.utc)
_FAST_END = datetime(2025, 12, 1, 3, 0, 0, tzinfo=timezone.utc)  # 3-hour window
_FAST_INTERVAL = 300   # 5 min => 37 ticks (00:00..03:00 inclusive)
_FAST_TICKS = 37


@pytest.fixture
def dense_db_url(tmp_path: Path) -> str:
    """Create DB with a snapshot at every 5-minute mark for 3 hours."""
    db_path = tmp_path / "dense_replay.db"
    url = f"sqlite:///{db_path}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine)
    session = SessionFactory()

    ts = _FAST_START
    while ts <= _FAST_END:
        session.add(_build_healthy_snapshot("SYM_A", ts))
        session.add(_build_unhealthy_snapshot("SYM_B", ts))
        ts += timedelta(seconds=_FAST_INTERVAL)

    session.commit()
    session.close()
    return url


# ---------------------------------------------------------------------------
# Test: SUSPENDED + probe behavior
# ---------------------------------------------------------------------------

class TestSuspendedProbeBehavior:
    """Force SYM_B to SUSPENDED and verify probe cadence."""

    def _run_with_fast_tracker(
        self, db_url: str, tmp_path: Path,
    ) -> tuple[Dict[str, SymbolCoverageAccumulator], DataQualityTracker]:
        """Run pass_enabled with compressed tracker timings."""
        ticker_prov = ReplayTickerProvider(db_url)
        candle_prov = ReplayCandleMetaProvider(db_url)

        symbols = ["SYM_A", "SYM_B"]
        ticker_prov.preload(symbols, _FAST_START, _FAST_END)
        candle_prov.preload(symbols, _FAST_START, _FAST_END)

        ticks: List[datetime] = []
        ts = _FAST_START
        while ts <= _FAST_END:
            ticks.append(ts)
            ts += timedelta(seconds=_FAST_INTERVAL)

        current_ts_cell = [ticks[0].timestamp()]
        tracker = DataQualityTracker(
            degraded_after_failures=3,
            suspend_after_seconds=900,           # 15 min
            probe_interval_seconds=600,          # 10 min = 2 ticks
            log_cooldown_seconds=0,              # no log suppression
            degraded_skip_ratio=2,               # analyze every other cycle
            release_after_successes=3,
            persist_interval_seconds=999_999,
            state_file="/dev/null",
            clock=lambda: current_ts_cell[0],
        )

        acc, tracker = run_pass_enabled(
            symbols, ticks, ticker_prov, candle_prov, _THRESHOLDS,
            tracker=tracker,
        )
        return acc, tracker

    def test_sym_b_reaches_suspended(self, dense_db_url: str, tmp_path: Path) -> None:
        acc, tracker = self._run_with_fast_tracker(dense_db_url, tmp_path)
        assert tracker.get_state("SYM_B") == SymbolHealthState.SUSPENDED

    def test_analyze_stops_while_suspended(self, dense_db_url: str, tmp_path: Path) -> None:
        """After SUSPENDED, analyze calls should only come from probes."""
        acc, tracker = self._run_with_fast_tracker(dense_db_url, tmp_path)
        b = acc["SYM_B"]

        # SYM_B must have been skipped many times (most cycles while SUSPENDED)
        assert b.skipped_count > 0, "SYM_B should be skipped while SUSPENDED"
        # analyze_calls = sanity checks that actually ran = pass + fail
        # Most of the 37 ticks should be skipped once SUSPENDED
        assert b.skipped_count > b.analyze_calls_count, (
            f"Skipped ({b.skipped_count}) should exceed analyze calls "
            f"({b.analyze_calls_count}) for a mostly-SUSPENDED symbol"
        )

    def test_probe_checks_occur(self, dense_db_url: str, tmp_path: Path) -> None:
        acc, _ = self._run_with_fast_tracker(dense_db_url, tmp_path)
        b = acc["SYM_B"]
        assert b.probe_checks_count > 0, "SYM_B should have probe checks"

    def test_probe_spacing(self, dense_db_url: str, tmp_path: Path) -> None:
        """Probes should occur at probe_interval (600s = 2 ticks).

        After ~6 ticks to reach SUSPENDED (3 HEALTHY + ~3 DEGRADED),
        remaining ~31 ticks have probes every 2 ticks => ~15 probes.
        """
        acc, _ = self._run_with_fast_tracker(dense_db_url, tmp_path)
        b = acc["SYM_B"]
        # With 37 total ticks, after becoming SUSPENDED the probes should
        # be reasonably bounded.  At least 5 probes and no more than 20.
        assert b.probe_checks_count >= 5, (
            f"Expected >= 5 probe checks, got {b.probe_checks_count}"
        )
        assert b.probe_checks_count <= 20, (
            f"Expected <= 20 probe checks, got {b.probe_checks_count}"
        )

    def test_state_transitions_logged(self, dense_db_url: str, tmp_path: Path) -> None:
        """SYM_B should have HEALTHY->DEGRADED and DEGRADED->SUSPENDED transitions."""
        acc, _ = self._run_with_fast_tracker(dense_db_url, tmp_path)
        b = acc["SYM_B"]
        trans = b.transitions_counts
        assert trans.get("HEALTHY_to_DEGRADED", 0) == 1, (
            f"Expected exactly 1 HEALTHY->DEGRADED, got {trans}"
        )
        assert trans.get("DEGRADED_to_SUSPENDED", 0) == 1, (
            f"Expected exactly 1 DEGRADED->SUSPENDED, got {trans}"
        )

    def test_sym_a_stays_healthy_throughout(self, dense_db_url: str, tmp_path: Path) -> None:
        acc, tracker = self._run_with_fast_tracker(dense_db_url, tmp_path)
        assert tracker.get_state("SYM_A") == SymbolHealthState.HEALTHY
        a = acc["SYM_A"]
        assert a.sanity_fail_count == 0
        assert a.skipped_count == 0
        assert a.cycles_sanity_pass == _FAST_TICKS


# ---------------------------------------------------------------------------
# Test: Restart-resume determinism
# ---------------------------------------------------------------------------

class TestRestartResumeDeterminism:
    """Split replay into two halves, persist tracker state between
    them, and verify the combined result matches a single
    uninterrupted run."""

    def test_split_equals_full_run(self, dense_db_url: str, tmp_path: Path) -> None:
        symbols = ["SYM_A", "SYM_B"]

        ticker_prov = ReplayTickerProvider(dense_db_url)
        candle_prov = ReplayCandleMetaProvider(dense_db_url)
        ticker_prov.preload(symbols, _FAST_START, _FAST_END)
        candle_prov.preload(symbols, _FAST_START, _FAST_END)

        # Build full tick timeline
        all_ticks: List[datetime] = []
        ts = _FAST_START
        while ts <= _FAST_END:
            all_ticks.append(ts)
            ts += timedelta(seconds=_FAST_INTERVAL)

        midpoint = len(all_ticks) // 2
        first_half = all_ticks[:midpoint]
        second_half = all_ticks[midpoint:]

        # --- Full uninterrupted run ---
        full_acc, _ = run_pass_enabled(
            symbols, all_ticks, ticker_prov, candle_prov, _THRESHOLDS,
        )

        # --- Split run: first half ---
        first_acc, tracker_after_first = run_pass_enabled(
            symbols, first_half, ticker_prov, candle_prov, _THRESHOLDS,
        )

        # --- Split run: second half (resume with same tracker + accumulators) ---
        combined_acc, _ = run_pass_enabled(
            symbols, second_half, ticker_prov, candle_prov, _THRESHOLDS,
            tracker=tracker_after_first,
            accumulators=first_acc,
        )

        # --- Compare ---
        for sym in symbols:
            full = full_acc[sym]
            comb = combined_acc[sym]

            assert full.cycles_total == comb.cycles_total, (
                f"{sym}: cycles_total full={full.cycles_total} != combined={comb.cycles_total}"
            )
            assert full.cycles_sanity_pass == comb.cycles_sanity_pass, (
                f"{sym}: pass full={full.cycles_sanity_pass} != combined={comb.cycles_sanity_pass}"
            )
            assert full.sanity_fail_count == comb.sanity_fail_count, (
                f"{sym}: fail full={full.sanity_fail_count} != combined={comb.sanity_fail_count}"
            )
            assert full.skipped_count == comb.skipped_count, (
                f"{sym}: skip full={full.skipped_count} != combined={comb.skipped_count}"
            )
            assert full.analyze_calls_count == comb.analyze_calls_count, (
                f"{sym}: analyze full={full.analyze_calls_count} != combined={comb.analyze_calls_count}"
            )
            assert full.probe_checks_count == comb.probe_checks_count, (
                f"{sym}: probes full={full.probe_checks_count} != combined={comb.probe_checks_count}"
            )
            assert full.transitions_counts == comb.transitions_counts, (
                f"{sym}: transitions full={full.transitions_counts} != combined={comb.transitions_counts}"
            )
