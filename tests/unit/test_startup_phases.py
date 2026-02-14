"""
Tests for StartupStateMachine (P2.3).

Validates:
  - Cannot place orders during SYNCING phase
  - Cannot skip SYNCING and go directly to RECONCILING
  - Reordering steps raises assertion
  - Failed startup transitions to FAILED, not READY
"""
import pytest
from src.runtime.startup_phases import StartupStateMachine, StartupPhase


class TestNormalFlow:
    """Happy-path startup sequence."""

    def test_starts_in_initializing(self):
        sm = StartupStateMachine()
        assert sm.phase == StartupPhase.INITIALIZING

    def test_full_startup_sequence(self):
        sm = StartupStateMachine()
        sm.advance_to(StartupPhase.SYNCING)
        assert sm.phase == StartupPhase.SYNCING

        sm.advance_to(StartupPhase.RECONCILING)
        assert sm.phase == StartupPhase.RECONCILING

        sm.advance_to(StartupPhase.READY)
        assert sm.phase == StartupPhase.READY
        assert sm.is_ready
        assert sm.startup_epoch is not None

    def test_assert_ready_passes_when_ready(self):
        sm = StartupStateMachine()
        sm.advance_to(StartupPhase.SYNCING)
        sm.advance_to(StartupPhase.RECONCILING)
        sm.advance_to(StartupPhase.READY)
        sm.assert_ready()  # Should not raise

    def test_get_status_contains_all_phases(self):
        sm = StartupStateMachine()
        sm.advance_to(StartupPhase.SYNCING)
        sm.advance_to(StartupPhase.RECONCILING)
        sm.advance_to(StartupPhase.READY)
        status = sm.get_status()
        assert status["phase"] == "ready"
        assert status["startup_epoch"] is not None
        assert "initializing" in status["phase_timestamps"]
        assert "syncing" in status["phase_timestamps"]
        assert "reconciling" in status["phase_timestamps"]
        assert "ready" in status["phase_timestamps"]


class TestOrderBlocking:
    """Cannot place orders before READY."""

    def test_cannot_place_orders_during_initializing(self):
        sm = StartupStateMachine()
        with pytest.raises(AssertionError, match="READY"):
            sm.assert_ready()

    def test_cannot_place_orders_during_syncing(self):
        sm = StartupStateMachine()
        sm.advance_to(StartupPhase.SYNCING)
        with pytest.raises(AssertionError, match="READY"):
            sm.assert_ready()

    def test_cannot_place_orders_during_reconciling(self):
        sm = StartupStateMachine()
        sm.advance_to(StartupPhase.SYNCING)
        sm.advance_to(StartupPhase.RECONCILING)
        with pytest.raises(AssertionError, match="READY"):
            sm.assert_ready()


class TestInvalidTransitions:
    """Cannot skip phases or go backwards."""

    def test_cannot_skip_syncing(self):
        sm = StartupStateMachine()
        with pytest.raises(AssertionError, match="Invalid startup transition"):
            sm.advance_to(StartupPhase.RECONCILING)

    def test_cannot_skip_to_ready(self):
        sm = StartupStateMachine()
        with pytest.raises(AssertionError, match="Invalid startup transition"):
            sm.advance_to(StartupPhase.READY)

    def test_cannot_go_backwards(self):
        sm = StartupStateMachine()
        sm.advance_to(StartupPhase.SYNCING)
        sm.advance_to(StartupPhase.RECONCILING)
        with pytest.raises(AssertionError, match="Invalid startup transition"):
            sm.advance_to(StartupPhase.SYNCING)

    def test_cannot_advance_from_ready_except_to_failed(self):
        sm = StartupStateMachine()
        sm.advance_to(StartupPhase.SYNCING)
        sm.advance_to(StartupPhase.RECONCILING)
        sm.advance_to(StartupPhase.READY)
        with pytest.raises(AssertionError, match="Invalid startup transition"):
            sm.advance_to(StartupPhase.SYNCING)


class TestFailedState:
    """FAILED is terminal."""

    def test_fail_from_initializing(self):
        sm = StartupStateMachine()
        sm.fail("test failure")
        assert sm.phase == StartupPhase.FAILED
        assert sm.is_failed
        assert not sm.is_ready
        assert sm.failure_reason == "test failure"

    def test_fail_from_syncing(self):
        sm = StartupStateMachine()
        sm.advance_to(StartupPhase.SYNCING)
        sm.fail("sync failed")
        assert sm.is_failed

    def test_fail_from_reconciling(self):
        sm = StartupStateMachine()
        sm.advance_to(StartupPhase.SYNCING)
        sm.advance_to(StartupPhase.RECONCILING)
        sm.fail("recon failed")
        assert sm.is_failed

    def test_cannot_advance_from_failed(self):
        sm = StartupStateMachine()
        sm.fail("done")
        with pytest.raises(AssertionError, match="Cannot advance from FAILED"):
            sm.advance_to(StartupPhase.SYNCING)

    def test_double_fail_is_noop(self):
        sm = StartupStateMachine()
        sm.fail("first")
        sm.fail("second")  # Should not raise
        assert sm.failure_reason == "first"  # Original reason preserved

    def test_assert_ready_fails_when_failed(self):
        sm = StartupStateMachine()
        sm.fail("test")
        with pytest.raises(AssertionError, match="READY"):
            sm.assert_ready()


class TestAssertPhase:
    """assert_phase and assert_at_least."""

    def test_assert_phase_matches(self):
        sm = StartupStateMachine()
        sm.assert_phase(StartupPhase.INITIALIZING)  # Should not raise
        sm.advance_to(StartupPhase.SYNCING)
        sm.assert_phase(StartupPhase.SYNCING)

    def test_assert_phase_mismatch(self):
        sm = StartupStateMachine()
        with pytest.raises(AssertionError, match="Expected.*syncing"):
            sm.assert_phase(StartupPhase.SYNCING)

    def test_assert_at_least_passes(self):
        sm = StartupStateMachine()
        sm.advance_to(StartupPhase.SYNCING)
        sm.advance_to(StartupPhase.RECONCILING)
        sm.assert_at_least(StartupPhase.SYNCING)  # Should not raise
        sm.assert_at_least(StartupPhase.RECONCILING)  # Should not raise

    def test_assert_at_least_fails(self):
        sm = StartupStateMachine()
        sm.advance_to(StartupPhase.SYNCING)
        with pytest.raises(AssertionError, match="at least.*reconciling"):
            sm.assert_at_least(StartupPhase.RECONCILING)

    def test_assert_at_least_fails_when_failed(self):
        sm = StartupStateMachine()
        sm.fail("broken")
        with pytest.raises(AssertionError, match="FAILED"):
            sm.assert_at_least(StartupPhase.SYNCING)
