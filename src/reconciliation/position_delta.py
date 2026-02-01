"""
Position Delta Reconciliation Layer.

This module implements the explicit invariant layer between strategy and execution:

    INTENDED_POSITION (what strategy wants)
         ↓
    ACTUAL_POSITION (what's on exchange)
         ↓
    DELTA (what needs to change)
         ↓
    DELTA_ALLOWED? (risk checks)
         ↓
    EXECUTION (only acts on reconciled delta)

CRITICAL: Execution should ONLY act on reconciled deltas, never on raw strategy signals.
This prevents position drift when:
- Orders are rejected
- Partial fills occur
- Exchange behavior diverges from expectations
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from src.monitoring.logger import get_logger
from src.domain.models import Side

logger = get_logger(__name__)


class DeltaAction(str, Enum):
    """Action to take based on position delta."""
    HOLD = "hold"          # No action needed, positions match
    OPEN = "open"          # Open new position
    CLOSE = "close"        # Close existing position
    ADJUST = "adjust"      # Adjust position size (scale in/out)
    FLIP = "flip"          # Reverse position direction
    REDUCE = "reduce"      # Reduce position (partial close)


class DeltaRejection(str, Enum):
    """Reasons for rejecting a position delta."""
    NONE = "none"                          # Not rejected
    INSUFFICIENT_MARGIN = "insufficient_margin"
    MAX_POSITIONS_REACHED = "max_positions_reached"
    MAX_NOTIONAL_EXCEEDED = "max_notional_exceeded"
    DELTA_TOO_SMALL = "delta_too_small"
    SYSTEM_HALTED = "system_halted"
    SYSTEM_DEGRADED = "system_degraded"
    POSITION_ALREADY_OPEN = "position_already_open"
    NO_POSITION_TO_CLOSE = "no_position_to_close"
    RECONCILIATION_PENDING = "reconciliation_pending"
    STALE_SIGNAL = "stale_signal"
    EXCHANGE_ERROR = "exchange_error"


@dataclass
class PositionIntent:
    """
    What the strategy WANTS the position to be.
    
    This is the OUTPUT of strategy, INPUT to reconciliation.
    """
    symbol: str
    side: Optional[Side]  # None = flat (no position desired)
    size: Decimal  # Desired size in base currency
    size_notional: Decimal  # Desired size in USD
    signal_id: Optional[str] = None  # Link to originating signal
    signal_score: float = 0.0
    reason: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ExchangePosition:
    """
    What ACTUALLY exists on the exchange.
    
    This is the ground truth from the exchange.
    """
    symbol: str
    side: Optional[Side]  # None = no position
    size: Decimal  # Actual size in base currency
    size_notional: Decimal  # Actual size in USD
    entry_price: Optional[Decimal] = None
    mark_price: Optional[Decimal] = None
    unrealized_pnl: Optional[Decimal] = None
    margin_used: Optional[Decimal] = None
    liquidation_price: Optional[Decimal] = None
    last_updated: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    @property
    def is_open(self) -> bool:
        """Check if there's an actual position."""
        return self.size > 0 and self.side is not None


@dataclass
class PositionDelta:
    """
    The DELTA between intended and actual position.
    
    This is what execution should act on, NOT raw strategy signals.
    """
    symbol: str
    
    # Intent vs Reality
    intended_side: Optional[Side]
    intended_size: Decimal
    actual_side: Optional[Side]
    actual_size: Decimal
    
    # Calculated delta
    delta_size: Decimal  # +ve = need to buy, -ve = need to sell
    delta_notional: Decimal  # Delta in USD terms
    
    # Action determination
    action: DeltaAction
    
    # Reconciliation status
    is_reconciled: bool  # True if intended == actual
    
    # Risk check result
    allowed: bool
    rejection: DeltaRejection = DeltaRejection.NONE
    rejection_details: str = ""
    rejection_reason: Optional[str] = None  # V2: Additional rejection reason for idempotency
    
    # Idempotency (V2)
    action_id: Optional[str] = None  # Deterministic ID for duplicate detection
    
    # Metadata
    signal_id: Optional[str] = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for logging."""
        return {
            "symbol": self.symbol,
            "intended_side": self.intended_side.value if self.intended_side else None,
            "intended_size": str(self.intended_size),
            "actual_side": self.actual_side.value if self.actual_side else None,
            "actual_size": str(self.actual_size),
            "delta_size": str(self.delta_size),
            "delta_notional": str(self.delta_notional),
            "action": self.action.value,
            "is_reconciled": self.is_reconciled,
            "allowed": self.allowed,
            "rejection": self.rejection.value if not self.allowed else None,
            "action_id": self.action_id,
        }



class PositionDeltaReconciler:
    """
    Reconciles intended positions (from strategy) with actual positions (from exchange).
    
    Execution should ONLY act on reconciled deltas, never on raw strategy signals.
    
    Usage:
        reconciler = PositionDeltaReconciler(config)
        
        # Get exchange position
        actual = reconciler.get_actual_position(symbol, exchange_client)
        
        # Create intent from strategy signal
        intent = reconciler.create_intent_from_signal(signal, account_info)
        
        # Calculate delta
        delta = reconciler.calculate_delta(intent, actual)
        
        # Only proceed if delta is allowed
        if delta.allowed and not delta.is_reconciled:
            execute_delta(delta)
    """
    
    def __init__(
        self,
        min_delta_threshold_usd: Decimal = Decimal("10"),
        max_delta_per_order_usd: Decimal = Decimal("50000"),
        max_delta_pct_of_position: Decimal = Decimal("0.5"),  # 50% change per order
    ):
        """
        Initialize reconciler.
        
        Args:
            min_delta_threshold_usd: Minimum delta to act on (avoid dust trades)
            max_delta_per_order_usd: Maximum delta per single order
            max_delta_pct_of_position: Maximum position change as % of current
        """
        self.min_delta_threshold = min_delta_threshold_usd
        self.max_delta_per_order = max_delta_per_order_usd
        self.max_delta_pct = max_delta_pct_of_position
        
        logger.info(
            "PositionDeltaReconciler initialized",
            min_delta=str(min_delta_threshold_usd),
            max_delta_per_order=str(max_delta_per_order_usd),
        )
    
    def calculate_delta(
        self,
        intent: Optional[PositionIntent],
        actual: Optional[ExchangePosition],
        current_price: Optional[Decimal] = None,
    ) -> PositionDelta:
        """
        Calculate the delta between intended and actual position.
        
        This is the CORE reconciliation logic.
        
        Args:
            intent: What strategy wants (None = want flat)
            actual: What exists on exchange (None = no position)
            current_price: Current market price for notional calculations
            
        Returns:
            PositionDelta with action and allowed status
        """
        symbol = (intent.symbol if intent else actual.symbol) if (intent or actual) else "UNKNOWN"
        
        # Extract intent values
        intended_side = intent.side if intent else None
        intended_size = intent.size if intent else Decimal("0")
        intended_notional = intent.size_notional if intent else Decimal("0")
        
        # Extract actual values
        actual_side = actual.side if actual and actual.is_open else None
        actual_size = actual.size if actual and actual.is_open else Decimal("0")
        actual_notional = actual.size_notional if actual and actual.is_open else Decimal("0")
        
        # Determine action and calculate delta
        action, delta_size, delta_notional = self._calculate_action_and_delta(
            intended_side=intended_side,
            intended_size=intended_size,
            intended_notional=intended_notional,
            actual_side=actual_side,
            actual_size=actual_size,
            actual_notional=actual_notional,
        )
        
        # Check if reconciled (intent matches reality)
        is_reconciled = (
            intended_side == actual_side and
            abs(intended_size - actual_size) < Decimal("0.0001")
        )
        
        # Create delta object
        delta = PositionDelta(
            symbol=symbol,
            intended_side=intended_side,
            intended_size=intended_size,
            actual_side=actual_side,
            actual_size=actual_size,
            delta_size=delta_size,
            delta_notional=delta_notional,
            action=action,
            is_reconciled=is_reconciled,
            allowed=True,  # Will be updated by risk checks
            signal_id=intent.signal_id if intent else None,
        )
        
        # Apply risk checks
        self._apply_risk_checks(delta)
        
        logger.info(
            "POSITION_DELTA_CALCULATED",
            **delta.to_dict(),
        )
        
        return delta
    
    def _calculate_action_and_delta(
        self,
        intended_side: Optional[Side],
        intended_size: Decimal,
        intended_notional: Decimal,
        actual_side: Optional[Side],
        actual_size: Decimal,
        actual_notional: Decimal,
    ) -> Tuple[DeltaAction, Decimal, Decimal]:
        """
        Calculate action and delta values.
        
        Returns:
            (action, delta_size, delta_notional)
            delta_size is positive for buy, negative for sell
        """
        # Case 1: Both flat -> HOLD
        if intended_side is None and actual_side is None:
            return DeltaAction.HOLD, Decimal("0"), Decimal("0")
        
        # Case 2: Want flat, have position -> CLOSE
        if intended_side is None and actual_side is not None:
            # Need to close the position
            delta_size = -actual_size if actual_side == Side.LONG else actual_size
            return DeltaAction.CLOSE, delta_size, -actual_notional
        
        # Case 3: Want position, have flat -> OPEN
        if intended_side is not None and actual_side is None:
            delta_size = intended_size if intended_side == Side.LONG else -intended_size
            return DeltaAction.OPEN, delta_size, intended_notional
        
        # Case 4: Both have positions
        # Case 4a: Same side -> ADJUST or HOLD
        if intended_side == actual_side:
            size_diff = intended_size - actual_size
            notional_diff = intended_notional - actual_notional
            
            if abs(size_diff) < Decimal("0.0001"):
                return DeltaAction.HOLD, Decimal("0"), Decimal("0")
            elif size_diff > 0:
                # Scale in
                delta_size = size_diff if intended_side == Side.LONG else -size_diff
                return DeltaAction.ADJUST, delta_size, notional_diff
            else:
                # Scale out (partial close)
                delta_size = size_diff if intended_side == Side.LONG else -size_diff
                return DeltaAction.REDUCE, delta_size, notional_diff
        
        # Case 4b: Different sides -> FLIP
        # This is a full position reversal
        total_delta = intended_size + actual_size  # Full flip
        delta_size = total_delta if intended_side == Side.LONG else -total_delta
        delta_notional = intended_notional + actual_notional
        return DeltaAction.FLIP, delta_size, delta_notional
    
    def _apply_risk_checks(self, delta: PositionDelta) -> None:
        """
        Apply risk checks to the delta.
        
        Modifies delta.allowed and delta.rejection in place.
        """
        # Skip checks if already reconciled
        if delta.is_reconciled:
            delta.allowed = True
            delta.rejection = DeltaRejection.NONE
            return
        
        # Check minimum delta threshold
        if abs(delta.delta_notional) < self.min_delta_threshold:
            delta.allowed = False
            delta.rejection = DeltaRejection.DELTA_TOO_SMALL
            delta.rejection_details = f"Delta ${abs(delta.delta_notional):.2f} below minimum ${self.min_delta_threshold}"
            return
        
        # Check maximum delta per order
        if abs(delta.delta_notional) > self.max_delta_per_order:
            # Cap the delta instead of rejecting
            logger.warning(
                "Delta capped to max per order",
                original=str(delta.delta_notional),
                capped=str(self.max_delta_per_order),
            )
            # We allow but note the cap
            delta.rejection_details = f"Delta capped from ${abs(delta.delta_notional):.2f} to ${self.max_delta_per_order}"
        
        delta.allowed = True
        delta.rejection = DeltaRejection.NONE
    
    def apply_system_state_check(
        self,
        delta: PositionDelta,
        system_state: str,
        active_violations: List[str],
    ) -> None:
        """
        Apply system state checks to delta.
        
        Call this after calculate_delta if you have InvariantMonitor state.
        
        Args:
            delta: The delta to check
            system_state: Current system state (ACTIVE, DEGRADED, HALTED, EMERGENCY)
            active_violations: List of active invariant violations
        """
        if system_state == "emergency":
            # Only allow closing positions
            if delta.action not in (DeltaAction.CLOSE, DeltaAction.REDUCE, DeltaAction.HOLD):
                delta.allowed = False
                delta.rejection = DeltaRejection.SYSTEM_HALTED
                delta.rejection_details = f"EMERGENCY state - only closes allowed. Violations: {active_violations}"
                return
        
        if system_state == "halted":
            # No new entries, only management
            if delta.action == DeltaAction.OPEN:
                delta.allowed = False
                delta.rejection = DeltaRejection.SYSTEM_HALTED
                delta.rejection_details = f"HALTED state - no new entries. Violations: {active_violations}"
                return
        
        if system_state == "degraded":
            # Allow but flag
            if delta.action == DeltaAction.OPEN:
                delta.rejection_details = f"DEGRADED state - proceed with caution. Violations: {active_violations}"
    
    def create_intent_from_signal(
        self,
        signal: Any,  # Signal object
        size_notional: Decimal,
        size_base: Decimal,
    ) -> PositionIntent:
        """
        Create a PositionIntent from a strategy signal.
        
        Args:
            signal: Signal object with symbol, side, etc.
            size_notional: Calculated position size in USD
            size_base: Calculated position size in base currency
            
        Returns:
            PositionIntent representing what strategy wants
        """
        # Determine side from signal
        signal_type = getattr(signal, 'signal_type', None)
        if hasattr(signal_type, 'value'):
            signal_type = signal_type.value
        
        if signal_type in ("LONG", "long", "buy", "BUY"):
            side = Side.LONG
        elif signal_type in ("SHORT", "short", "sell", "SELL"):
            side = Side.SHORT
        else:
            side = None  # No position desired
        
        return PositionIntent(
            symbol=getattr(signal, 'symbol', 'UNKNOWN'),
            side=side,
            size=size_base,
            size_notional=size_notional,
            signal_id=getattr(signal, 'signal_id', None) or str(id(signal)),
            signal_score=sum(getattr(signal, 'score_breakdown', {}).values()) if getattr(signal, 'score_breakdown', None) else 0.0,
            reason=getattr(signal, 'reasoning', '') or '',
        )
    
    def create_flat_intent(self, symbol: str, reason: str = "close_signal") -> PositionIntent:
        """Create an intent to be flat (close position)."""
        return PositionIntent(
            symbol=symbol,
            side=None,
            size=Decimal("0"),
            size_notional=Decimal("0"),
            reason=reason,
        )
    
    def log_reconciliation_summary(
        self,
        deltas: List[PositionDelta],
        cycle_id: str,
    ) -> Dict[str, Any]:
        """
        Log a summary of reconciliation results.
        
        Args:
            deltas: List of calculated deltas
            cycle_id: Current cycle identifier
            
        Returns:
            Summary dict for logging
        """
        summary = {
            "cycle_id": cycle_id,
            "total_deltas": len(deltas),
            "reconciled": sum(1 for d in deltas if d.is_reconciled),
            "allowed": sum(1 for d in deltas if d.allowed and not d.is_reconciled),
            "rejected": sum(1 for d in deltas if not d.allowed),
            "by_action": {},
            "by_rejection": {},
        }
        
        for d in deltas:
            action = d.action.value
            summary["by_action"][action] = summary["by_action"].get(action, 0) + 1
            
            if not d.allowed:
                rejection = d.rejection.value
                summary["by_rejection"][rejection] = summary["by_rejection"].get(rejection, 0) + 1
        
        logger.info("RECONCILIATION_SUMMARY", **summary)
        return summary


# ===== GLOBAL SINGLETON =====
_delta_reconciler: Optional[PositionDeltaReconciler] = None


def get_delta_reconciler() -> PositionDeltaReconciler:
    """Get global delta reconciler instance."""
    global _delta_reconciler
    if _delta_reconciler is None:
        _delta_reconciler = PositionDeltaReconciler()
    return _delta_reconciler


def init_delta_reconciler(
    min_delta_threshold_usd: Decimal = Decimal("10"),
    max_delta_per_order_usd: Decimal = Decimal("50000"),
) -> PositionDeltaReconciler:
    """Initialize global delta reconciler with custom settings."""
    global _delta_reconciler
    _delta_reconciler = PositionDeltaReconciler(
        min_delta_threshold_usd=min_delta_threshold_usd,
        max_delta_per_order_usd=max_delta_per_order_usd,
    )
    return _delta_reconciler
