"""
Startup State Machine (P2.3).

Enforces explicit phase ordering during system startup to prevent:
  - Bug #3 (phantom position flattening from reordered steps)
  - Trading actions before sync/reconciliation completes
  - Silent regression from refactoring startup code

Phases:
  INITIALIZING → SYNCING → RECONCILING → READY
  Any phase → FAILED (terminal)

Usage:
    sm = StartupStateMachine()
    sm.advance_to(StartupPhase.SYNCING)
    sm.assert_phase(StartupPhase.SYNCING)
    sm.advance_to(StartupPhase.RECONCILING)
    sm.advance_to(StartupPhase.READY)
    sm.assert_ready()  # Use before any trading action
"""
from datetime import datetime, timezone
from enum import Enum, unique
from typing import Optional

from src.monitoring.logger import get_logger

logger = get_logger(__name__)


@unique
class StartupPhase(str, Enum):
    """Startup phases in required order."""
    INITIALIZING = "initializing"    # Components created, config loaded
    SYNCING = "syncing"              # Exchange client + account state synced
    RECONCILING = "reconciling"      # Position registry aligned with exchange
    READY = "ready"                  # Trading allowed
    FAILED = "failed"                # Startup failed, exit

    @property
    def order(self) -> int:
        """Numeric order for comparison. FAILED is special (terminal)."""
        return {
            "initializing": 0,
            "syncing": 1,
            "reconciling": 2,
            "ready": 3,
            "failed": -1,
        }[self.value]


# Valid transitions: from_phase -> set of allowed next phases
_VALID_TRANSITIONS = {
    StartupPhase.INITIALIZING: {StartupPhase.SYNCING, StartupPhase.FAILED},
    StartupPhase.SYNCING: {StartupPhase.RECONCILING, StartupPhase.FAILED},
    StartupPhase.RECONCILING: {StartupPhase.READY, StartupPhase.FAILED},
    StartupPhase.READY: {StartupPhase.FAILED},  # READY can only go to FAILED
    StartupPhase.FAILED: set(),  # Terminal state — no transitions out
}


class StartupStateMachine:
    """Enforces startup phase ordering.

    Thread-safe: designed for single-threaded async, but uses no
    global state. Each LiveTrading instance owns one.
    """

    def __init__(self):
        self._phase: StartupPhase = StartupPhase.INITIALIZING
        self._phase_timestamps: dict[StartupPhase, datetime] = {
            StartupPhase.INITIALIZING: datetime.now(timezone.utc),
        }
        self._startup_epoch: Optional[datetime] = None
        self._failure_reason: Optional[str] = None

    # -- Public API ----------------------------------------------------------

    @property
    def phase(self) -> StartupPhase:
        """Current startup phase."""
        return self._phase

    @property
    def is_ready(self) -> bool:
        """True if trading is allowed."""
        return self._phase == StartupPhase.READY

    @property
    def is_failed(self) -> bool:
        """True if startup failed."""
        return self._phase == StartupPhase.FAILED

    @property
    def startup_epoch(self) -> Optional[datetime]:
        """Timestamp when READY was reached. None if not yet ready."""
        return self._startup_epoch

    @property
    def failure_reason(self) -> Optional[str]:
        """Reason for failure, if in FAILED state."""
        return self._failure_reason

    def advance_to(self, next_phase: StartupPhase, *, reason: str = "") -> None:
        """Transition to the next phase.

        Raises AssertionError if the transition is invalid (wrong ordering,
        skipped step, or transition from terminal state).
        """
        if self._phase == StartupPhase.FAILED:
            raise AssertionError(
                f"Cannot advance from FAILED state. "
                f"Failure reason: {self._failure_reason}"
            )

        if next_phase not in _VALID_TRANSITIONS.get(self._phase, set()):
            raise AssertionError(
                f"Invalid startup transition: {self._phase.value} → {next_phase.value}. "
                f"Valid targets: {[p.value for p in _VALID_TRANSITIONS.get(self._phase, set())]}"
            )

        now = datetime.now(timezone.utc)
        prev_phase = self._phase
        self._phase = next_phase
        self._phase_timestamps[next_phase] = now

        if next_phase == StartupPhase.READY:
            self._startup_epoch = now

        if next_phase == StartupPhase.FAILED:
            self._failure_reason = reason or "unspecified"
            logger.critical(
                "Startup FAILED",
                from_phase=prev_phase.value,
                reason=self._failure_reason,
            )
        else:
            elapsed = (now - self._phase_timestamps.get(prev_phase, now)).total_seconds()
            logger.info(
                "Startup phase transition",
                from_phase=prev_phase.value,
                to_phase=next_phase.value,
                elapsed_seconds=f"{elapsed:.2f}",
                reason=reason or None,
            )

    def fail(self, reason: str) -> None:
        """Transition directly to FAILED from any non-terminal state."""
        if self._phase == StartupPhase.FAILED:
            return  # Already failed, don't overwrite reason
        self.advance_to(StartupPhase.FAILED, reason=reason)

    def assert_phase(self, required: StartupPhase) -> None:
        """Assert the current phase matches `required`.

        Raises AssertionError if not.
        """
        if self._phase != required:
            raise AssertionError(
                f"Expected startup phase {required.value}, "
                f"but current phase is {self._phase.value}"
            )

    def assert_ready(self) -> None:
        """Assert that the system has completed startup and is READY.

        Call this before any trading action (order placement, etc.).
        Raises AssertionError if not READY.
        """
        if self._phase != StartupPhase.READY:
            raise AssertionError(
                f"Trading action attempted in phase '{self._phase.value}'. "
                f"System must be in READY phase. "
                f"Current phase: {self._phase.value}"
            )

    def assert_at_least(self, minimum: StartupPhase) -> None:
        """Assert the current phase is at least `minimum` (by order).

        Useful for operations that require sync but not full readiness.
        """
        if self._phase == StartupPhase.FAILED:
            raise AssertionError(
                f"System is in FAILED state (reason: {self._failure_reason})"
            )
        if self._phase.order < minimum.order:
            raise AssertionError(
                f"Operation requires at least phase {minimum.value}, "
                f"but current phase is {self._phase.value}"
            )

    def get_status(self) -> dict:
        """Return current status for metrics / logging."""
        return {
            "phase": self._phase.value,
            "startup_epoch": self._startup_epoch.isoformat() if self._startup_epoch else None,
            "failure_reason": self._failure_reason,
            "phase_timestamps": {
                p.value: ts.isoformat()
                for p, ts in self._phase_timestamps.items()
            },
        }
