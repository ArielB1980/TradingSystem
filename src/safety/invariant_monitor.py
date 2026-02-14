"""
InvariantMonitor: Hard safety limits for production trading.

This module enforces critical invariants that, when violated, trigger
immediate system state changes (DEGRADED, HALTED, or EMERGENCY).

CRITICAL: All trading operations MUST check this before proceeding.

Invariants enforced:
1. Max equity drawdown percentage
2. Max open notional exposure
3. Max concurrent positions
4. Max margin utilization
5. Max rejected orders per cycle
6. Max API errors per minute
7. Max single position size as % of equity
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import List, Optional, Dict, Any
import asyncio
import json
import os

from src.exceptions import OperationalError, DataError
from src.monitoring.logger import get_logger
from src.utils.kill_switch import KillSwitch, KillSwitchReason

logger = get_logger(__name__)

# ===== PEAK EQUITY PERSISTENCE =====
# Prevents drawdown protection from resetting on restart.
# Without this, a restart after 14% drawdown resets the high-water mark,
# allowing another 15% before halt — cumulative 27% loss.

_DEFAULT_STATE_DIR = Path.home() / ".trading_system"
_PEAK_EQUITY_FILE = "peak_equity_state.json"

# Epsilon to avoid float noise: only update peak when equity exceeds by > $0.01
_PEAK_EQUITY_EPSILON = Decimal("0.01")


def _peak_equity_path() -> Path:
    """State file path: PEAK_EQUITY_STATE_PATH env, or ~/.trading_system/peak_equity_state.json."""
    env_path = os.environ.get("PEAK_EQUITY_STATE_PATH")
    if env_path:
        return Path(env_path)
    return _DEFAULT_STATE_DIR / _PEAK_EQUITY_FILE


def _load_persisted_peak_equity() -> Optional[Decimal]:
    """Load persisted peak equity from disk. Returns None if missing/corrupt."""
    path = _peak_equity_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        value = Decimal(str(data["peak_equity"]))
        if value > 0:
            logger.info(
                "Loaded persisted peak equity",
                peak_equity=str(value),
                updated_at=data.get("updated_at", "unknown"),
            )
            return value
        return None
    except (json.JSONDecodeError, ValueError, TypeError, KeyError, OSError) as e:
        logger.warning("Failed to load persisted peak equity", error=str(e))
        return None


def _save_persisted_peak_equity(peak_equity: Decimal) -> None:
    """Persist peak equity to disk."""
    try:
        path = _peak_equity_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "peak_equity": str(peak_equity),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        path.write_text(json.dumps(data, indent=2))
    except OSError as e:
        logger.warning("Failed to save persisted peak equity", error=str(e))


class SystemState(str, Enum):
    """System operational state.
    
    ACTIVE: Normal operations, all trading allowed
    DEGRADED: Some limits breached, reduce exposure, no new entries
    HALTED: Trading stopped, only position management allowed
    EMERGENCY: Flatten all positions immediately
    """
    ACTIVE = "active"
    DEGRADED = "degraded"
    HALTED = "halted"
    EMERGENCY = "emergency"


@dataclass
class InvariantViolation:
    """Record of an invariant violation."""
    invariant: str
    threshold: str
    actual: str
    severity: str  # "WARNING" or "CRITICAL"
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    def __str__(self) -> str:
        return f"{self.severity}: {self.invariant} - threshold={self.threshold}, actual={self.actual}"


@dataclass
class SystemInvariants:
    """Hard limits that trigger immediate system halt.
    
    These are the absolute limits that should NEVER be exceeded.
    Designed to prevent catastrophic losses.
    """
    
    # ===== EQUITY-BASED =====
    # Maximum drawdown from peak equity before halting
    max_equity_drawdown_pct: Decimal = Decimal("0.15")  # 15% - CRITICAL
    
    # Optional absolute minimum equity floor (USD)
    min_equity_floor_usd: Optional[Decimal] = None
    
    # ===== EXPOSURE-BASED =====
    # Maximum total notional exposure (USD)
    max_open_notional_usd: Decimal = Decimal("500000")
    
    # Maximum concurrent open positions
    # Must be >= auction_max_positions to avoid false HALTs.
    max_concurrent_positions: int = 27
    
    # Maximum margin utilization percentage
    # NOTE: Must be > auction_max_margin_util (0.90) to avoid premature HALT
    max_margin_utilization_pct: Decimal = Decimal("0.92")  # 92%
    
    # Maximum single position as % of equity
    max_single_position_pct_equity: Decimal = Decimal("0.25")  # 25%
    
    # ===== OPERATIONAL =====
    # Maximum rejected orders per cycle (indicates execution issues)
    max_rejected_orders_per_cycle: int = 5
    
    # Maximum API errors per minute (indicates connectivity issues)
    max_api_errors_per_minute: int = 10
    
    # Maximum acceptable API latency (ms)
    max_latency_ms: int = 5000
    
    # ===== DEGRADED MODE THRESHOLDS =====
    # These trigger DEGRADED state (warnings) before HALTED
    degraded_equity_drawdown_pct: Decimal = Decimal("0.10")  # 10% - WARNING
    degraded_margin_utilization_pct: Decimal = Decimal("0.85")  # 85%
    degraded_concurrent_positions: int = 22


class InvariantMonitor:
    """
    Central invariant enforcement.
    
    CRITICAL: This is the single source of truth for system health.
    All trading operations MUST check this before proceeding.
    
    Usage:
        monitor = get_invariant_monitor()
        if not monitor.is_trading_allowed():
            return  # Don't open new positions
    """
    
    def __init__(
        self,
        invariants: Optional[SystemInvariants] = None,
        kill_switch: Optional[KillSwitch] = None,
    ):
        """
        Initialize invariant monitor.
        
        Args:
            invariants: System invariant thresholds (uses defaults if None)
            kill_switch: Kill switch instance for emergency halt
        """
        self.invariants = invariants or SystemInvariants()
        self.kill_switch = kill_switch
        self.state = SystemState.ACTIVE
        self.last_check = datetime.min.replace(tzinfo=timezone.utc)
        self.violations: List[InvariantViolation] = []
        
        # Rolling counters
        self._rejected_orders_this_cycle = 0
        self._api_errors: List[datetime] = []  # Timestamps of recent errors
        self._last_state_change: datetime = datetime.now(timezone.utc)
        
        # Peak equity: load from persisted state to survive restarts.
        # Without persistence, every restart resets the high-water mark and
        # allows another full drawdown cycle — cumulative losses compound.
        self._peak_equity: Optional[Decimal] = _load_persisted_peak_equity()
        
        # History for debugging
        self._violation_history: List[InvariantViolation] = []
        self._max_history_size = 100
        
        logger.info(
            "InvariantMonitor initialized",
            max_drawdown_pct=str(self.invariants.max_equity_drawdown_pct),
            max_notional=str(self.invariants.max_open_notional_usd),
            max_positions=self.invariants.max_concurrent_positions,
            persisted_peak_equity=str(self._peak_equity) if self._peak_equity else "none",
        )
    
    def set_kill_switch(self, kill_switch: KillSwitch):
        """Set kill switch for emergency halt."""
        self.kill_switch = kill_switch
    
    async def check_all(
        self,
        current_equity: Decimal,
        open_positions: List[Any],  # List of Position objects
        margin_utilization: Decimal,
        available_margin: Decimal,
    ) -> SystemState:
        """
        Check all invariants and update system state.
        
        This should be called at the START of every trading cycle.
        
        Args:
            current_equity: Current account equity in USD
            open_positions: List of open Position objects
            margin_utilization: Current margin usage as decimal (0.0 - 1.0)
            available_margin: Available margin in USD
            
        Returns:
            Current system state after checks
        """
        now = datetime.now(timezone.utc)
        violations: List[InvariantViolation] = []
        
        # ===== 1. EQUITY DRAWDOWN CHECK =====
        # Peak equity tracks the high-water mark for drawdown calculation.
        # Persisted to disk so restarts don't "forgive" drawdown.
        # Epsilon guard: only update when equity exceeds peak by > $0.01
        # to avoid float noise from mark-to-market jitter.
        if self._peak_equity is None:
            self._peak_equity = current_equity
            _save_persisted_peak_equity(current_equity)
        elif current_equity > self._peak_equity + _PEAK_EQUITY_EPSILON:
            self._peak_equity = current_equity
            _save_persisted_peak_equity(current_equity)
        
        if self._peak_equity > 0:
            drawdown_pct = (self._peak_equity - current_equity) / self._peak_equity
            
            # Critical level
            if drawdown_pct > self.invariants.max_equity_drawdown_pct:
                violations.append(InvariantViolation(
                    invariant="max_equity_drawdown_pct",
                    threshold=f"{self.invariants.max_equity_drawdown_pct:.1%}",
                    actual=f"{drawdown_pct:.1%}",
                    severity="CRITICAL",
                ))
            # Degraded level
            elif drawdown_pct > self.invariants.degraded_equity_drawdown_pct:
                violations.append(InvariantViolation(
                    invariant="degraded_equity_drawdown_pct",
                    threshold=f"{self.invariants.degraded_equity_drawdown_pct:.1%}",
                    actual=f"{drawdown_pct:.1%}",
                    severity="WARNING",
                ))
        
        # ===== 2. EQUITY FLOOR CHECK =====
        if self.invariants.min_equity_floor_usd:
            if current_equity < self.invariants.min_equity_floor_usd:
                violations.append(InvariantViolation(
                    invariant="min_equity_floor_usd",
                    threshold=str(self.invariants.min_equity_floor_usd),
                    actual=str(current_equity),
                    severity="CRITICAL",
                ))
        
        # ===== 3. TOTAL NOTIONAL CHECK =====
        total_notional = sum(
            getattr(p, 'size_notional', Decimal("0")) or Decimal("0")
            for p in open_positions
        )
        if total_notional > self.invariants.max_open_notional_usd:
            violations.append(InvariantViolation(
                invariant="max_open_notional_usd",
                threshold=str(self.invariants.max_open_notional_usd),
                actual=str(total_notional),
                severity="CRITICAL",
            ))
        
        # ===== 4. CONCURRENT POSITIONS CHECK =====
        position_count = len(open_positions)
        if position_count > self.invariants.max_concurrent_positions:
            violations.append(InvariantViolation(
                invariant="max_concurrent_positions",
                threshold=str(self.invariants.max_concurrent_positions),
                actual=str(position_count),
                severity="CRITICAL",
            ))
        elif position_count > self.invariants.degraded_concurrent_positions:
            violations.append(InvariantViolation(
                invariant="degraded_concurrent_positions",
                threshold=str(self.invariants.degraded_concurrent_positions),
                actual=str(position_count),
                severity="WARNING",
            ))
        
        # ===== 5. MARGIN UTILIZATION CHECK =====
        if margin_utilization > self.invariants.max_margin_utilization_pct:
            violations.append(InvariantViolation(
                invariant="max_margin_utilization_pct",
                threshold=f"{self.invariants.max_margin_utilization_pct:.1%}",
                actual=f"{margin_utilization:.1%}",
                severity="CRITICAL",
            ))
        elif margin_utilization > self.invariants.degraded_margin_utilization_pct:
            violations.append(InvariantViolation(
                invariant="degraded_margin_utilization_pct",
                threshold=f"{self.invariants.degraded_margin_utilization_pct:.1%}",
                actual=f"{margin_utilization:.1%}",
                severity="WARNING",
            ))
        
        # ===== 6. SINGLE POSITION SIZE CHECK =====
        if current_equity > 0:
            for pos in open_positions:
                pos_notional = getattr(pos, 'size_notional', Decimal("0")) or Decimal("0")
                pos_pct = pos_notional / current_equity
                if pos_pct > self.invariants.max_single_position_pct_equity:
                    violations.append(InvariantViolation(
                        invariant="max_single_position_pct_equity",
                        threshold=f"{self.invariants.max_single_position_pct_equity:.1%}",
                        actual=f"{pos_pct:.1%} ({getattr(pos, 'symbol', 'unknown')})",
                        severity="WARNING",
                    ))
        
        # ===== 7. REJECTED ORDERS CHECK =====
        if self._rejected_orders_this_cycle > self.invariants.max_rejected_orders_per_cycle:
            violations.append(InvariantViolation(
                invariant="max_rejected_orders_per_cycle",
                threshold=str(self.invariants.max_rejected_orders_per_cycle),
                actual=str(self._rejected_orders_this_cycle),
                severity="WARNING",
            ))
        
        # ===== 8. API ERRORS CHECK =====
        # Clean old errors (older than 1 minute)
        one_minute_ago = now - timedelta(minutes=1)
        self._api_errors = [t for t in self._api_errors if t > one_minute_ago]
        
        if len(self._api_errors) > self.invariants.max_api_errors_per_minute:
            violations.append(InvariantViolation(
                invariant="max_api_errors_per_minute",
                threshold=str(self.invariants.max_api_errors_per_minute),
                actual=str(len(self._api_errors)),
                severity="CRITICAL",
            ))
        
        # ===== DETERMINE NEW STATE =====
        self.violations = violations
        self._update_violation_history(violations)
        
        critical_count = sum(1 for v in violations if v.severity == "CRITICAL")
        warning_count = sum(1 for v in violations if v.severity == "WARNING")
        
        old_state = self.state
        
        if critical_count >= 2:
            # Multiple critical violations = EMERGENCY
            self.state = SystemState.EMERGENCY
            if self.kill_switch:
                await self.kill_switch.activate(
                    KillSwitchReason.MARGIN_CRITICAL,
                    emergency=True
                )
            logger.critical(
                "SYSTEM_EMERGENCY",
                violations=[str(v) for v in violations],
                critical_count=critical_count,
                action="EMERGENCY_FLATTEN",
            )
        elif critical_count == 1:
            # Single critical violation = HALTED
            self.state = SystemState.HALTED
            if self.kill_switch:
                await self.kill_switch.activate(
                    KillSwitchReason.MARGIN_CRITICAL,
                    emergency=False
                )
            logger.critical(
                "SYSTEM_HALTED",
                violations=[str(v) for v in violations],
                critical_count=critical_count,
                action="HALT_NEW_ENTRIES",
            )
        elif warning_count >= 2:
            # Multiple warnings = DEGRADED
            self.state = SystemState.DEGRADED
            logger.warning(
                "SYSTEM_DEGRADED",
                violations=[str(v) for v in violations],
                warning_count=warning_count,
                action="REDUCE_EXPOSURE",
            )
        else:
            # All clear or single warning = ACTIVE
            self.state = SystemState.ACTIVE
        
        if old_state != self.state:
            self._last_state_change = now
            logger.info(
                "SYSTEM_STATE_CHANGE",
                old_state=old_state.value,
                new_state=self.state.value,
            )
        
        self.last_check = now
        return self.state
    
    def record_order_rejection(self):
        """Record an order rejection for rate limiting."""
        self._rejected_orders_this_cycle += 1
        logger.debug("Order rejection recorded", count=self._rejected_orders_this_cycle)
    
    def record_api_error(self):
        """Record an API error for rate limiting."""
        self._api_errors.append(datetime.now(timezone.utc))
        logger.debug("API error recorded", count=len(self._api_errors))
    
    def reset_cycle_counters(self):
        """Reset per-cycle counters (call at end of each tick)."""
        self._rejected_orders_this_cycle = 0
    
    def is_trading_allowed(self) -> bool:
        """
        Check if new entries are allowed.
        
        Only returns True if system is ACTIVE.
        """
        return self.state == SystemState.ACTIVE
    
    def is_management_allowed(self) -> bool:
        """
        Check if position management is allowed.
        
        Returns True if system is ACTIVE or DEGRADED.
        Position management (SL/TP updates, trailing stops) is always allowed
        except in EMERGENCY state.
        """
        return self.state in (SystemState.ACTIVE, SystemState.DEGRADED, SystemState.HALTED)
    
    def get_status(self) -> Dict[str, Any]:
        """Get current monitor status for debugging/dashboard."""
        return {
            "state": self.state.value,
            "trading_allowed": self.is_trading_allowed(),
            "management_allowed": self.is_management_allowed(),
            "peak_equity": str(self._peak_equity) if self._peak_equity else None,
            "last_check": self.last_check.isoformat(),
            "last_state_change": self._last_state_change.isoformat(),
            "active_violations": [str(v) for v in self.violations],
            "rejected_orders_this_cycle": self._rejected_orders_this_cycle,
            "api_errors_last_minute": len(self._api_errors),
        }
    
    def _update_violation_history(self, violations: List[InvariantViolation]):
        """Update violation history for debugging."""
        self._violation_history.extend(violations)
        if len(self._violation_history) > self._max_history_size:
            self._violation_history = self._violation_history[-self._max_history_size:]
    
    def get_violation_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Get recent violation history."""
        return [
            {
                "invariant": v.invariant,
                "threshold": v.threshold,
                "actual": v.actual,
                "severity": v.severity,
                "timestamp": v.timestamp.isoformat(),
            }
            for v in self._violation_history[-limit:]
        ]
    
    def reset_peak_equity(self, new_peak: Optional[Decimal] = None):
        """
        Reset peak equity (e.g., after manual acknowledgment).
        
        Also persists the new peak so it survives restarts.
        
        Args:
            new_peak: New peak value, or None to clear entirely
        """
        self._peak_equity = new_peak
        if new_peak is not None:
            _save_persisted_peak_equity(new_peak)
        else:
            # Remove the persisted file when explicitly clearing
            try:
                path = _peak_equity_path()
                if path.exists():
                    path.unlink()
            except OSError as e:
                logger.warning("Failed to remove persisted peak equity file", error=str(e))
        logger.info("Peak equity reset", new_peak=str(new_peak))


# ===== GLOBAL SINGLETON =====
_invariant_monitor: Optional[InvariantMonitor] = None


def get_invariant_monitor() -> InvariantMonitor:
    """Get global invariant monitor instance."""
    global _invariant_monitor
    if _invariant_monitor is None:
        _invariant_monitor = InvariantMonitor()
    return _invariant_monitor


def init_invariant_monitor(
    invariants: Optional[SystemInvariants] = None,
    kill_switch: Optional[KillSwitch] = None,
) -> InvariantMonitor:
    """Initialize global invariant monitor with custom settings."""
    global _invariant_monitor
    _invariant_monitor = InvariantMonitor(invariants, kill_switch)
    return _invariant_monitor
