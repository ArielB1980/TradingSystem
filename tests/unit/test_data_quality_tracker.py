"""
Unit tests for src/data/data_quality_tracker.py -- per-symbol state machine.

Tests cover:
  - State transitions: HEALTHY -> DEGRADED -> SUSPENDED -> HEALTHY
  - should_analyze() scheduling behavior (degraded skip ratio, suspended probe)
  - record_result() pass/fail bookkeeping
  - log_event() rate limiting
  - Persistence: save/restore round-trip
  - get_status_summary()
"""
import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from src.data.data_quality_tracker import (
    DataQualityTracker,
    SymbolHealthState,
    _SymbolRecord,
    DEFAULT_DEGRADED_AFTER_FAILURES,
    DEFAULT_SUSPEND_AFTER_SECONDS,
    DEFAULT_RELEASE_AFTER_SUCCESSES,
    DEFAULT_DEGRADED_SKIP_RATIO,
    DEFAULT_PROBE_INTERVAL_SECONDS,
    DEFAULT_LOG_COOLDOWN_SECONDS,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tracker(tmp_path):
    """Tracker with sensible test defaults (short timeouts)."""
    return DataQualityTracker(
        degraded_after_failures=3,
        suspend_after_seconds=60,        # 1 minute for tests
        release_after_successes=3,
        probe_interval_seconds=10,
        log_cooldown_seconds=0,          # no cooldown in tests
        degraded_skip_ratio=4,
        persist_interval_seconds=0,      # always persist
        state_file=str(tmp_path / "state.json"),
    )


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------

class TestStateTransitions:

    def test_starts_healthy(self, tracker):
        assert tracker.get_state("X") == SymbolHealthState.HEALTHY

    def test_healthy_to_degraded(self, tracker):
        """3 consecutive failures → DEGRADED."""
        for _ in range(3):
            tracker.record_result("X", passed=False, reason="bad")
        assert tracker.get_state("X") == SymbolHealthState.DEGRADED

    def test_stays_healthy_under_threshold(self, tracker):
        """2 failures don't trigger DEGRADED."""
        tracker.record_result("X", passed=False, reason="bad")
        tracker.record_result("X", passed=False, reason="bad")
        assert tracker.get_state("X") == SymbolHealthState.HEALTHY

    def test_pass_resets_failure_streak(self, tracker):
        """A pass in the middle resets the failure counter."""
        tracker.record_result("X", passed=False, reason="bad")
        tracker.record_result("X", passed=False, reason="bad")
        tracker.record_result("X", passed=True)
        tracker.record_result("X", passed=False, reason="bad")
        tracker.record_result("X", passed=False, reason="bad")
        assert tracker.get_state("X") == SymbolHealthState.HEALTHY

    def test_degraded_to_suspended(self, tracker):
        """After 3 failures (→ DEGRADED), continuous failure for suspend_after_seconds → SUSPENDED."""
        # Enter DEGRADED
        for _ in range(3):
            tracker.record_result("X", passed=False, reason="bad")
        assert tracker.get_state("X") == SymbolHealthState.DEGRADED

        # Fake the first failure timestamp to 2 minutes ago
        rec = tracker._get("X")
        rec.first_failure_ts = time.time() - 120  # 2 min > 60s threshold

        # One more failure triggers suspension
        tracker.record_result("X", passed=False, reason="still bad")
        assert tracker.get_state("X") == SymbolHealthState.SUSPENDED

    def test_degraded_to_healthy(self, tracker):
        """3 passes in DEGRADED → HEALTHY."""
        for _ in range(3):
            tracker.record_result("X", passed=False, reason="bad")
        assert tracker.get_state("X") == SymbolHealthState.DEGRADED

        for _ in range(3):
            tracker.record_result("X", passed=True)
        assert tracker.get_state("X") == SymbolHealthState.HEALTHY

    def test_suspended_to_healthy(self, tracker):
        """3 passes in SUSPENDED → HEALTHY."""
        # Enter DEGRADED then SUSPENDED
        for _ in range(3):
            tracker.record_result("X", passed=False, reason="bad")
        rec = tracker._get("X")
        rec.first_failure_ts = time.time() - 120
        tracker.record_result("X", passed=False, reason="bad")
        assert tracker.get_state("X") == SymbolHealthState.SUSPENDED

        for _ in range(3):
            tracker.record_result("X", passed=True)
        assert tracker.get_state("X") == SymbolHealthState.HEALTHY

    def test_partial_recovery_resets(self, tracker):
        """2 passes then a fail should reset consecutive_successes."""
        for _ in range(3):
            tracker.record_result("X", passed=False, reason="bad")
        assert tracker.get_state("X") == SymbolHealthState.DEGRADED

        tracker.record_result("X", passed=True)
        tracker.record_result("X", passed=True)
        tracker.record_result("X", passed=False, reason="relapse")
        # Still degraded, didn't recover
        assert tracker.get_state("X") == SymbolHealthState.DEGRADED


# ---------------------------------------------------------------------------
# should_analyze()
# ---------------------------------------------------------------------------

class TestShouldAnalyze:

    def test_healthy_always_true(self, tracker):
        for _ in range(10):
            assert tracker.should_analyze("X") is True

    def test_degraded_every_nth(self, tracker):
        # Enter DEGRADED
        for _ in range(3):
            tracker.record_result("X", passed=False, reason="bad")
        assert tracker.get_state("X") == SymbolHealthState.DEGRADED

        # Reset cycle counter for clean test
        tracker._get("X").cycle_counter = 0

        results = [tracker.should_analyze("X") for _ in range(8)]
        # With skip_ratio=4, every 4th call returns True
        # Cycle counts: 1,2,3,4,5,6,7,8
        # True on 4,8 (i.e. cycle_counter % 4 == 0)
        assert results == [False, False, False, True, False, False, False, True]

    def test_suspended_probe_interval(self, tracker):
        # Enter SUSPENDED
        for _ in range(3):
            tracker.record_result("X", passed=False, reason="bad")
        rec = tracker._get("X")
        rec.first_failure_ts = time.time() - 120
        tracker.record_result("X", passed=False, reason="bad")
        assert tracker.get_state("X") == SymbolHealthState.SUSPENDED

        # First call after suspension: last_probe_ts was set by transition
        # Force probe by setting last_probe to old time
        rec.last_probe_ts = time.time() - 20  # > probe_interval_seconds (10)

        assert tracker.should_analyze("X") is True
        # Immediately after probe, should be false
        assert tracker.should_analyze("X") is False


# ---------------------------------------------------------------------------
# log_event() rate limiting
# ---------------------------------------------------------------------------

class TestLogEvent:

    def test_rate_limit_suppresses_repeated_logs(self, tmp_path):
        t = DataQualityTracker(
            log_cooldown_seconds=60,
            state_file=str(tmp_path / "state.json"),
        )
        with patch("src.data.data_quality_tracker.logger") as mock_log:
            t.log_event("X", "test", "reason1")
            t.log_event("X", "test", "reason2")  # suppressed
            assert mock_log.info.call_count == 1

    def test_force_bypasses_rate_limit(self, tmp_path):
        t = DataQualityTracker(
            log_cooldown_seconds=60,
            state_file=str(tmp_path / "state.json"),
        )
        with patch("src.data.data_quality_tracker.logger") as mock_log:
            t.log_event("X", "test", "reason1")
            t.log_event("X", "test", "reason2", force=True)
            assert mock_log.info.call_count == 2

    def test_different_symbols_not_suppressed(self, tmp_path):
        t = DataQualityTracker(
            log_cooldown_seconds=60,
            state_file=str(tmp_path / "state.json"),
        )
        with patch("src.data.data_quality_tracker.logger") as mock_log:
            t.log_event("X", "test", "reason1")
            t.log_event("Y", "test", "reason2")
            assert mock_log.info.call_count == 2


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class TestPersistence:

    def test_persist_and_restore(self, tracker, tmp_path):
        # Enter DEGRADED for X
        for _ in range(3):
            tracker.record_result("X", passed=False, reason="bad")
        assert tracker.get_state("X") == SymbolHealthState.DEGRADED

        # HEALTHY for Y
        tracker.record_result("Y", passed=True)

        # Persist
        tracker.force_persist()

        # Create new tracker, restore
        t2 = DataQualityTracker(
            state_file=str(tmp_path / "state.json"),
        )
        t2.restore()

        assert t2.get_state("X") == SymbolHealthState.DEGRADED
        assert t2.get_state("Y") == SymbolHealthState.HEALTHY  # not persisted

    def test_persist_skips_healthy(self, tracker, tmp_path):
        """Only non-HEALTHY symbols should be in the JSON file."""
        tracker.record_result("X", passed=True)
        tracker.force_persist()

        data = json.loads((tmp_path / "state.json").read_text())
        assert len(data) == 0

    def test_restore_nonexistent_file(self, tmp_path):
        """Restoring from missing file should not crash."""
        t = DataQualityTracker(state_file=str(tmp_path / "missing.json"))
        t.restore()  # should log info, not crash
        assert t.get_state("X") == SymbolHealthState.HEALTHY

    def test_restore_corrupt_file(self, tmp_path):
        """Restoring from corrupt JSON should not crash."""
        bad_file = tmp_path / "state.json"
        bad_file.write_text("NOT VALID JSON {{{")
        t = DataQualityTracker(state_file=str(bad_file))
        t.restore()
        assert t.get_state("X") == SymbolHealthState.HEALTHY

    def test_persist_atomic_write(self, tracker, tmp_path):
        """Persist uses tmp -> rename for atomic write."""
        for _ in range(3):
            tracker.record_result("X", passed=False, reason="bad")
        tracker.force_persist()

        state_file = tmp_path / "state.json"
        assert state_file.exists()
        # No .tmp file left behind
        assert not (tmp_path / "state.tmp").exists()


# ---------------------------------------------------------------------------
# get_status_summary()
# ---------------------------------------------------------------------------

class TestStatusSummary:

    def test_empty_tracker(self, tracker):
        s = tracker.get_status_summary()
        assert s == {"healthy": 0, "degraded": [], "suspended": []}

    def test_mixed_states(self, tracker):
        # Y: healthy
        tracker.record_result("Y", passed=True)

        # X: degraded
        for _ in range(3):
            tracker.record_result("X", passed=False, reason="bad")

        s = tracker.get_status_summary()
        assert s["healthy"] == 1
        assert "X" in s["degraded"]
        assert s["suspended"] == []

    def test_suspended_shows_up(self, tracker):
        for _ in range(3):
            tracker.record_result("Z", passed=False, reason="bad")
        rec = tracker._get("Z")
        rec.first_failure_ts = time.time() - 120
        tracker.record_result("Z", passed=False, reason="bad")

        s = tracker.get_status_summary()
        assert "Z" in s["suspended"]
