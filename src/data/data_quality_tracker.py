"""
Per-symbol data quality state machine.

Tracks consecutive failures, manages state transitions
(HEALTHY -> DEGRADED -> SUSPENDED), controls analysis eligibility,
and provides unified rate-limited logging.

Also maintains a rolling **data trust score** per symbol: the fraction
of sanity checks that passed over a configurable window (default 7 days).
The trust score is read-only / informational today.  A future enhancement
may use it for risk-weight scaling (never for hard exclusion -- that is
the sanity gate's job).

Persists non-HEALTHY state to ``.local/data_quality_state.json``
every 5 minutes so SUSPENDED/DEGRADED symbols survive restarts.
"""
from __future__ import annotations

import json
import os
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from src.monitoring.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants / defaults
# ---------------------------------------------------------------------------

DEFAULT_DEGRADED_AFTER_FAILURES = 3
DEFAULT_SUSPEND_AFTER_SECONDS = 6 * 3600  # 6 hours
DEFAULT_RELEASE_AFTER_SUCCESSES = 3
DEFAULT_PROBE_INTERVAL_SECONDS = 30 * 60  # 30 minutes
DEFAULT_LOG_COOLDOWN_SECONDS = 1800       # 30 minutes
DEFAULT_DEGRADED_SKIP_RATIO = 4           # analyze 1 in 4 cycles
DEFAULT_PERSIST_INTERVAL_SECONDS = 5 * 60 # 5 minutes
DEFAULT_STATE_FILE = ".local/data_quality_state.json"
DEFAULT_TRUST_WINDOW_SECONDS = 7 * 24 * 3600  # 7 days


# ---------------------------------------------------------------------------
# State enum
# ---------------------------------------------------------------------------

class SymbolHealthState(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    SUSPENDED = "SUSPENDED"


# ---------------------------------------------------------------------------
# Per-symbol bookkeeping
# ---------------------------------------------------------------------------

@dataclass
class _SymbolRecord:
    """Internal mutable record for one symbol."""

    state: SymbolHealthState = SymbolHealthState.HEALTHY
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    first_failure_ts: float = 0.0      # time.time() of first failure in current streak
    last_probe_ts: float = 0.0         # last time we allowed a probe in SUSPENDED
    cycle_counter: int = 0             # used for DEGRADED skip ratio
    # Rolling trust score history: deque of (ts, passed) tuples.
    # Entries older than the trust window are pruned on each insertion.
    trust_history: Deque[Tuple[float, bool]] = field(default_factory=deque)


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------

class DataQualityTracker:
    """Per-symbol state machine for data quality gating."""

    def __init__(
        self,
        *,
        degraded_after_failures: int = DEFAULT_DEGRADED_AFTER_FAILURES,
        suspend_after_seconds: float = DEFAULT_SUSPEND_AFTER_SECONDS,
        release_after_successes: int = DEFAULT_RELEASE_AFTER_SUCCESSES,
        probe_interval_seconds: float = DEFAULT_PROBE_INTERVAL_SECONDS,
        log_cooldown_seconds: float = DEFAULT_LOG_COOLDOWN_SECONDS,
        degraded_skip_ratio: int = DEFAULT_DEGRADED_SKIP_RATIO,
        persist_interval_seconds: float = DEFAULT_PERSIST_INTERVAL_SECONDS,
        state_file: str = DEFAULT_STATE_FILE,
        clock: Optional[Callable[[], float]] = None,
        trust_window_seconds: float = DEFAULT_TRUST_WINDOW_SECONDS,
    ) -> None:
        self.degraded_after_failures = degraded_after_failures
        self.suspend_after_seconds = suspend_after_seconds
        self.release_after_successes = release_after_successes
        self.probe_interval_seconds = probe_interval_seconds
        self.log_cooldown_seconds = log_cooldown_seconds
        self.degraded_skip_ratio = degraded_skip_ratio
        self.persist_interval_seconds = persist_interval_seconds
        self.state_file = Path(state_file)
        self._clock: Callable[[], float] = clock or time.time
        self.trust_window_seconds = trust_window_seconds

        self._symbols: Dict[str, _SymbolRecord] = {}
        self._log_cooldowns: Dict[str, float] = {}   # symbol -> last log ts
        self._last_persist_ts: float = 0.0

    # -- helpers --

    def _get(self, symbol: str) -> _SymbolRecord:
        if symbol not in self._symbols:
            self._symbols[symbol] = _SymbolRecord()
        return self._symbols[symbol]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def should_analyze(self, symbol: str) -> bool:
        """Decide whether *symbol* should be scheduled for analysis this cycle.

        Called at the scheduling level (before ``asyncio.gather``).
        """
        rec = self._get(symbol)
        rec.cycle_counter += 1

        if rec.state == SymbolHealthState.HEALTHY:
            return True

        if rec.state == SymbolHealthState.DEGRADED:
            # Analyze 1 in N cycles
            if rec.cycle_counter % self.degraded_skip_ratio == 0:
                return True
            self.log_event(symbol, "degraded_skip", "skipping cycle (DEGRADED)")
            return False

        if rec.state == SymbolHealthState.SUSPENDED:
            now = self._clock()
            if now - rec.last_probe_ts >= self.probe_interval_seconds:
                rec.last_probe_ts = now
                self.log_event(symbol, "probe", "probing SUSPENDED symbol")
                return True
            # Silently skip -- no log needed each cycle
            return False

        return True  # unreachable fallback

    def record_result(self, symbol: str, passed: bool, reason: str = "") -> None:
        """Record whether the latest sanity check passed or failed.

        Handles all state transitions and updates the rolling trust score.
        """
        rec = self._get(symbol)
        now = self._clock()

        if passed:
            self._handle_pass(rec, symbol, now)
        else:
            self._handle_fail(rec, symbol, now, reason)

        # --- Update rolling trust history ---
        rec.trust_history.append((now, passed))
        self._prune_trust_history(rec, now)

    def get_state(self, symbol: str) -> SymbolHealthState:
        return self._get(symbol).state

    def get_status_summary(self) -> Dict[str, Any]:
        """Snapshot for health-check / Telegram status.

        Includes per-symbol trust scores for any symbol below 1.0.
        """
        summary: Dict[str, Any] = {
            "healthy": 0,
            "degraded": [],
            "suspended": [],
            "trust_scores": {},   # symbol -> score (only those < 1.0)
        }
        for sym, rec in self._symbols.items():
            if rec.state == SymbolHealthState.HEALTHY:
                summary["healthy"] += 1
            elif rec.state == SymbolHealthState.DEGRADED:
                summary["degraded"].append(sym)
            elif rec.state == SymbolHealthState.SUSPENDED:
                summary["suspended"].append(sym)

            score_info = self.get_trust_score(sym)
            if score_info["total_checks"] > 0 and score_info["score"] < 1.0:
                summary["trust_scores"][sym] = score_info["score"]

        return summary

    # ------------------------------------------------------------------
    # Data trust score (read-only / informational)
    # ------------------------------------------------------------------

    def get_trust_score(
        self,
        symbol: str,
        window_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Return the data trust score for *symbol*.

        The trust score is the fraction of sanity checks that passed
        within the rolling window (default: ``trust_window_seconds``).

        Returns a dict with::

            {
                "score": float,         # 0.0 – 1.0
                "total_checks": int,
                "healthy_checks": int,
                "window_seconds": float,
            }

        This is **read-only / informational**.  No gating or eligibility
        decisions are made from this value today.

        .. note::
           Future enhancement: risk-weight scaling based on trust score.
           The trust score should **never** hard-exclude a symbol; hard
           exclusion is the sanity gate's responsibility.
        """
        window = window_seconds if window_seconds is not None else self.trust_window_seconds
        rec = self._get(symbol)
        now = self._clock()
        self._prune_trust_history(rec, now)

        total = len(rec.trust_history)
        healthy = sum(1 for _, passed in rec.trust_history if passed)
        score = healthy / total if total > 0 else 1.0  # assume healthy if no data

        return {
            "score": round(score, 4),
            "total_checks": total,
            "healthy_checks": healthy,
            "window_seconds": window,
        }

    def get_trust_scores_all(self) -> Dict[str, Dict[str, Any]]:
        """Return trust scores for all tracked symbols."""
        return {sym: self.get_trust_score(sym) for sym in self._symbols}

    def _prune_trust_history(self, rec: _SymbolRecord, now: float) -> None:
        """Remove entries older than the trust window from the deque."""
        cutoff = now - self.trust_window_seconds
        while rec.trust_history and rec.trust_history[0][0] < cutoff:
            rec.trust_history.popleft()

    # ------------------------------------------------------------------
    # Unified logging with per-symbol cooldown
    # ------------------------------------------------------------------

    def log_event(
        self,
        symbol: str,
        event_type: str,
        reason: str = "",
        *,
        force: bool = False,
    ) -> None:
        """Rate-limited logger.  State transitions always force-log."""
        now = self._clock()
        last = self._log_cooldowns.get(symbol, 0.0)

        if not force and (now - last) < self.log_cooldown_seconds:
            return  # suppressed

        self._log_cooldowns[symbol] = now
        logger.info(
            "data_quality_event",
            symbol=symbol,
            event_type=event_type,
            reason=reason,
            state=self._get(symbol).state.value,
        )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def persist(self) -> None:
        """Write non-HEALTHY symbols to disk if enough time has elapsed."""
        now = self._clock()
        if now - self._last_persist_ts < self.persist_interval_seconds:
            return
        self._last_persist_ts = now
        self._do_persist()

    def force_persist(self) -> None:
        """Persist immediately (e.g. on shutdown)."""
        self._last_persist_ts = self._clock()
        self._do_persist()

    def _do_persist(self) -> None:
        data: Dict[str, Any] = {}
        now = self._clock()
        for sym, rec in self._symbols.items():
            # Persist non-HEALTHY symbols AND any symbol with trust history
            has_trust_data = len(rec.trust_history) > 0
            if rec.state == SymbolHealthState.HEALTHY and not has_trust_data:
                continue
            entry: Dict[str, Any] = {
                "state": rec.state.value,
                "consecutive_failures": rec.consecutive_failures,
                "first_failure_ts": rec.first_failure_ts,
                "last_probe_ts": rec.last_probe_ts,
            }
            # Save trust history (pruned)
            if has_trust_data:
                self._prune_trust_history(rec, now)
                entry["trust_history"] = list(rec.trust_history)
            data[sym] = entry

        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(self.state_file)  # atomic on POSIX
            logger.debug("data_quality_state_persisted", symbols=len(data))
        except Exception:
            logger.exception("data_quality_persist_failed")

    def restore(self) -> None:
        """Restore non-HEALTHY state from disk on boot."""
        if not self.state_file.exists():
            logger.info("data_quality_no_saved_state", path=str(self.state_file))
            return

        try:
            raw = json.loads(self.state_file.read_text())
        except Exception:
            logger.exception("data_quality_restore_failed", path=str(self.state_file))
            return

        restored = 0
        now = self._clock()
        for sym, info in raw.items():
            rec = self._get(sym)
            try:
                rec.state = SymbolHealthState(info["state"])
            except (KeyError, ValueError):
                continue
            rec.consecutive_failures = info.get("consecutive_failures", 0)
            rec.first_failure_ts = info.get("first_failure_ts", 0.0)
            rec.last_probe_ts = info.get("last_probe_ts", 0.0)
            # Restore trust history (prune stale entries)
            saved_history = info.get("trust_history", [])
            if saved_history:
                rec.trust_history = deque(
                    (ts, passed) for ts, passed in saved_history
                )
                self._prune_trust_history(rec, now)
            restored += 1

        logger.info(
            "data_quality_state_restored",
            restored=restored,
            degraded=[s for s, r in self._symbols.items() if r.state == SymbolHealthState.DEGRADED],
            suspended=[s for s, r in self._symbols.items() if r.state == SymbolHealthState.SUSPENDED],
        )

    # ------------------------------------------------------------------
    # Internal transition logic
    # ------------------------------------------------------------------

    def _handle_pass(self, rec: _SymbolRecord, symbol: str, now: float) -> None:
        rec.consecutive_successes += 1
        rec.consecutive_failures = 0

        if rec.state in (SymbolHealthState.DEGRADED, SymbolHealthState.SUSPENDED):
            if rec.consecutive_successes >= self.release_after_successes:
                old = rec.state
                rec.state = SymbolHealthState.HEALTHY
                rec.first_failure_ts = 0.0
                self.log_event(
                    symbol,
                    "state_transition",
                    f"{old.value} -> HEALTHY after {rec.consecutive_successes} consecutive passes",
                    force=True,
                )
                rec.consecutive_successes = 0

    def _handle_fail(
        self, rec: _SymbolRecord, symbol: str, now: float, reason: str
    ) -> None:
        rec.consecutive_successes = 0
        rec.consecutive_failures += 1

        if rec.consecutive_failures == 1:
            rec.first_failure_ts = now

        if rec.state == SymbolHealthState.HEALTHY:
            if rec.consecutive_failures >= self.degraded_after_failures:
                rec.state = SymbolHealthState.DEGRADED
                self.log_event(
                    symbol,
                    "state_transition",
                    f"HEALTHY -> DEGRADED after {rec.consecutive_failures} failures: {reason}",
                    force=True,
                )

        elif rec.state == SymbolHealthState.DEGRADED:
            elapsed = now - rec.first_failure_ts
            if elapsed >= self.suspend_after_seconds:
                rec.state = SymbolHealthState.SUSPENDED
                rec.last_probe_ts = now  # don't probe immediately
                self.log_event(
                    symbol,
                    "state_transition",
                    f"DEGRADED -> SUSPENDED after {elapsed/3600:.1f}h continuous failure: {reason}",
                    force=True,
                )
            else:
                self.log_event(symbol, "sanity_fail", reason)

        elif rec.state == SymbolHealthState.SUSPENDED:
            # Already suspended -- just log the probe failure
            self.log_event(symbol, "probe_fail", reason)
