"""
Position Manager v2 - Production Grade.

State-machine-driven position management with:
1. All decisions go through PositionRegistry (single source of truth)
2. Order events drive state transitions (not intent)
3. Idempotent event handling
4. Shadow mode support for comparison
"""
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import List, Optional, Dict, Tuple
from datetime import datetime, timezone
from datetime import datetime, timezone
import uuid
import os

from src.execution.position_state_machine import (
    ManagedPosition,
    PositionState,
    PositionRegistry,
    ExitReason,
    OrderEvent,
    OrderEventType,
    get_position_registry,
    check_invariant
)
from src.domain.models import Side, OrderType, Signal, SignalType
from src.monitoring.logger import get_logger

logger = get_logger(__name__)


class ActionType(str, Enum):
    """Types of management actions."""
    # Entry Actions
    OPEN_POSITION = "open_position"
    CANCEL_ENTRY = "cancel_entry"
    
    # Exit Actions
    CLOSE_FULL = "close_full"
    CLOSE_PARTIAL = "close_partial"
    
    # Stop Management
    PLACE_STOP = "place_stop"
    UPDATE_STOP = "update_stop"
    CANCEL_STOP = "cancel_stop"
    
    # TP Management
    PLACE_TP = "place_tp"
    CANCEL_TP = "cancel_tp"
    
    # Reconciliation
    FLATTEN_ORPHAN = "flatten_orphan"
    SYNC_STOP = "sync_stop"
    
    # State Updates
    STATE_UPDATE = "state_update"
    
    # Rejections
    REJECT_ENTRY = "reject_entry"
    REJECT_ACTION = "reject_action"
    
    # No Action
    NO_ACTION = "no_action"


@dataclass
class ManagementAction:
    """
    Action to be executed by the execution layer.
    
    The PositionManager only DECIDES - it does not execute.
    The Execution Gateway executes and reports back via OrderEvents.
    """
    type: ActionType
    symbol: str
    reason: str
    
    # For entries/exits
    side: Optional[Side] = None
    size: Optional[Decimal] = None
    price: Optional[Decimal] = None
    order_type: OrderType = OrderType.MARKET
    
    # Order identification (for tracking)
    client_order_id: Optional[str] = None
    position_id: Optional[str] = None
    
    # Exit reason tracking
    exit_reason: Optional[ExitReason] = None
    
    # Priority (higher = execute first)
    priority: int = 0
    
    # Metadata for shadow mode
    decision_timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    def __post_init__(self):
        if self.client_order_id is None:
            self.client_order_id = f"psm-{uuid.uuid4().hex[:12]}"


@dataclass
class DecisionTick:
    """
    Record of a single decision tick for shadow mode comparison.
    """
    timestamp: datetime
    symbol: str
    current_price: Decimal
    position_state: Optional[str]
    position_id: Optional[str]
    remaining_qty: Optional[Decimal]
    current_stop: Optional[Decimal]
    actions: List[ManagementAction]
    reason_codes: List[str]
    
    def to_dict(self) -> Dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "symbol": self.symbol,
            "current_price": str(self.current_price),
            "position_state": self.position_state,
            "position_id": self.position_id,
            "remaining_qty": str(self.remaining_qty) if self.remaining_qty else None,
            "current_stop": str(self.current_stop) if self.current_stop else None,
            "actions": [{"type": a.type.value, "reason": a.reason} for a in self.actions],
            "reason_codes": self.reason_codes
        }


class PositionManagerV2:
    """
    State-Machine-Driven Position Manager.
    
    EXECUTION MODEL:
    1. Receive price update / order event
    2. Evaluate rules against current state
    3. Return prioritized list of actions
    4. Execution Gateway places orders with client_order_id linking to position_id
    5. Order events reported back via apply_order_event()
    6. State transitions are driven by acknowledged fills, not intent
    
    SHADOW MODE:
    - Records all decisions for comparison
    - Does not execute, only logs
    """
    
    def __init__(
        self,
        registry: Optional[PositionRegistry] = None,
        shadow_mode: bool = False
    ):
        """
        Initialize with optional custom registry.
        
        Args:
            registry: Position registry (uses singleton if not provided)
            shadow_mode: If True, only log decisions, don't emit actions
        """
        self.registry = registry or get_position_registry()
        self.shadow_mode = shadow_mode
        
        # Decision history for shadow mode
        self.decision_history: List[DecisionTick] = []
        self.max_history = 10000
        
        # Safety Components
        from src.execution.production_safety import (
            ExitTimeoutManager, 
            SafetyConfig, 
            PositionProtectionMonitor, 
            ProtectionEnforcer
        )
        self.safety_config = SafetyConfig()
        self.exit_timeout_manager = ExitTimeoutManager(self.safety_config)
        # Note: PositionProtectionMonitor requires client/gateway, usually run externally or injected.
        # We'll leave monitor for the outer loop, but manage timeouts here.
        
        # Configuration
        
        # Configuration
        self.tp1_partial_pct = Decimal("0.5")
        self.tp2_partial_pct = Decimal("0.25")
        self.trailing_atr_multiple = Decimal("1.5")
        
        # Metrics
        self.metrics = {
            "opens": 0,
            "closes": 0,
            "reversals_attempted": 0,
            "stop_moves": 0,
            "blocked_duplicates": 0,
            "errors": 0
        }
    
    # ========== ENTRY EVALUATION ==========
    
    def evaluate_entry(
        self,
        signal: Signal,
        entry_price: Decimal,
        stop_price: Decimal,
        tp1_price: Optional[Decimal],
        tp2_price: Optional[Decimal],
        final_target: Optional[Decimal],
        position_size: Decimal,
        trade_type: str = "tight_smc"
    ) -> Tuple[ManagementAction, Optional[ManagedPosition]]:
        """
        Evaluate whether a new position can be opened.
        
        Returns:
            (action, position) - Action to execute and the prepared position object
        """
        symbol = signal.symbol
        side = Side.LONG if signal.signal_type == SignalType.LONG else Side.SHORT
        
        # SAFETY CHECK: New entries enabled?
        if os.environ.get("TRADING_NEW_ENTRIES_ENABLED", "true").lower() != "true":
            return ManagementAction(
                type=ActionType.REJECT_ENTRY,
                symbol=symbol,
                reason="Global Switch: NEW_ENTRIES_ENABLED=False",
                side=side,
                priority=-1
            ), None
        
        # Check if position can be opened
        can_open, reason = self.registry.can_open_position(symbol, side)
        
        if not can_open:
            self.metrics["blocked_duplicates"] += 1
            logger.warning("Entry REJECTED", symbol=symbol, side=side.value, reason=reason)
            
            return ManagementAction(
                type=ActionType.REJECT_ENTRY,
                symbol=symbol,
                reason=reason,
                side=side,
                priority=-1
            ), None
        
        # Validate stop
        if stop_price is None:
            return ManagementAction(
                type=ActionType.REJECT_ENTRY,
                symbol=symbol,
                reason="NO STOP PRICE DEFINED",
                side=side,
                priority=-1
            ), None
        
        # Validate stop direction
        if side == Side.LONG and stop_price >= entry_price:
            return ManagementAction(
                type=ActionType.REJECT_ENTRY,
                symbol=symbol,
                reason=f"LONG stop ({stop_price}) must be below entry ({entry_price})",
                side=side,
                priority=-1
            ), None
        if side == Side.SHORT and stop_price <= entry_price:
            return ManagementAction(
                type=ActionType.REJECT_ENTRY,
                symbol=symbol,
                reason=f"SHORT stop ({stop_price}) must be above entry ({entry_price})",
                side=side,
                priority=-1
            ), None
        
        # Create position object (not registered until entry acknowledged)
        position_id = f"pos-{uuid.uuid4().hex[:12]}"
        client_order_id = f"entry-{position_id}"
        
        position = ManagedPosition(
            symbol=symbol,
            side=side,
            position_id=position_id,
            initial_size=position_size,
            initial_entry_price=entry_price,
            initial_stop_price=stop_price,
            initial_tp1_price=tp1_price,
            initial_tp2_price=tp2_price,
            initial_final_target=final_target,
            setup_type=signal.setup_type.value if hasattr(signal, 'setup_type') else None,
            regime=signal.regime if hasattr(signal, 'regime') else None,
            trade_type=trade_type
        )
        position.entry_order_id = client_order_id
        position.entry_client_order_id = client_order_id
        
        self.metrics["opens"] += 1
        logger.info(
            "Entry APPROVED",
            symbol=symbol,
            side=side.value,
            size=str(position_size),
            entry=str(entry_price),
            stop=str(stop_price),
            position_id=position_id
        )
        
        return ManagementAction(
            type=ActionType.OPEN_POSITION,
            symbol=symbol,
            reason="Entry criteria met",
            side=side,
            size=position_size,
            price=entry_price,
            client_order_id=client_order_id,
            position_id=position_id,
            priority=10
        ), position
    
    # ========== POSITION EVALUATION ==========
    
    def evaluate_position(
        self,
        symbol: str,
        current_price: Decimal,
        current_atr: Optional[Decimal] = None,
        premise_invalidated: bool = False
    ) -> List[ManagementAction]:
        """
        Evaluate all rules for an active position.
        
        RULE PRIORITY (highest to lowest):
        1. STOP HIT → Immediate close (ABSOLUTE)
        2. PREMISE INVALIDATION → Immediate close
        3. FINAL TARGET HIT → Full close
        4. TP2 HIT → Partial close
        5. TP1 HIT → Partial close + conditional BE
        6. TRAILING STOP UPDATE → Move stop toward profit
        7. NO ACTION
        
        Returns:
            Prioritized list of actions (execute in order)
        """
        actions: List[ManagementAction] = []
        reason_codes: List[str] = []
        
        position = self.registry.get_position(symbol)
        
        # No position or in-flight
        if position is None:
            return []
        
        if position.state in (PositionState.PENDING, PositionState.EXIT_PENDING, 
                              PositionState.CANCEL_PENDING):
            reason_codes.append(f"IN_FLIGHT:{position.state.value}")
            self._record_decision(symbol, current_price, position, actions, reason_codes)
            return []
        
        if position.is_terminal:
            return []
        
        # ========== RULE 2: STOP HIT (ABSOLUTE PRIORITY) ==========
        if position.check_stop_hit(current_price):
            reason_codes.append("STOP_HIT")
            exit_reason = ExitReason.TRAILING_STOP if position.trailing_active else ExitReason.STOP_LOSS
            
            logger.critical(
                "🛑 STOP HIT - IMMEDIATE EXIT",
                symbol=symbol,
                stop_price=str(position.current_stop_price),
                current_price=str(current_price)
            )
            
            client_order_id = f"exit-stop-{position.position_id}"
            
            actions.append(ManagementAction(
                type=ActionType.CLOSE_FULL,
                symbol=symbol,
                reason=f"Stop Hit ({position.current_stop_price})",
                side=position.side,
                size=position.remaining_qty,
                price=current_price,
                order_type=OrderType.MARKET,
                client_order_id=client_order_id,
                position_id=position.position_id,
                exit_reason=exit_reason,
                priority=100
            ))
            
            self._record_decision(symbol, current_price, position, actions, reason_codes)
            return actions
        
        # ========== RULE 3: PREMISE INVALIDATION ==========
        if premise_invalidated:
            reason_codes.append("PREMISE_INVALIDATED")
            client_order_id = f"exit-premise-{position.position_id}"
            
            actions.append(ManagementAction(
                type=ActionType.CLOSE_FULL,
                symbol=symbol,
                reason="Premise Invalidated",
                side=position.side,
                size=position.remaining_qty,
                order_type=OrderType.MARKET,
                client_order_id=client_order_id,
                position_id=position.position_id,
                exit_reason=ExitReason.PREMISE_INVALIDATION,
                priority=90
            ))
            
            self._record_decision(symbol, current_price, position, actions, reason_codes)
            return actions
        
        # ========== RULE 11: FINAL TARGET HIT ==========
        if position.check_final_target_hit(current_price):
            reason_codes.append("FINAL_TARGET_HIT")
            client_order_id = f"exit-final-{position.position_id}"
            
            actions.append(ManagementAction(
                type=ActionType.CLOSE_FULL,
                symbol=symbol,
                reason=f"Final Target Hit ({position.initial_final_target})",
                side=position.side,
                size=position.remaining_qty,
                order_type=OrderType.MARKET,
                client_order_id=client_order_id,
                position_id=position.position_id,
                exit_reason=ExitReason.TAKE_PROFIT_FINAL,
                priority=80
            ))
            
            self._record_decision(symbol, current_price, position, actions, reason_codes)
            return actions
        
        # ========== RULE 10: TP2 HIT ==========
        if position.check_tp2_hit(current_price):
            if os.environ.get("TRADING_PARTIALS_ENABLED", "true").lower() != "true":
                reason_codes.append("TP2_HIT_IGNORED")
            else:
                reason_codes.append("TP2_HIT")
                partial_size = position.remaining_qty * self.tp2_partial_pct
                client_order_id = f"exit-tp2-{position.position_id}"
                
                actions.append(ManagementAction(
                    type=ActionType.CLOSE_PARTIAL,
                    symbol=symbol,
                    reason=f"TP2 Hit ({position.initial_tp2_price})",
                    side=position.side,
                    size=partial_size,
                    order_type=OrderType.MARKET,
                    client_order_id=client_order_id,
                    position_id=position.position_id,
                    exit_reason=ExitReason.TAKE_PROFIT_2,
                    priority=70
                ))
        
        # ========== RULE 5: TP1 HIT ==========
        if position.check_tp1_hit(current_price):
            if os.environ.get("TRADING_PARTIALS_ENABLED", "true").lower() != "true":
                reason_codes.append("TP1_HIT_IGNORED")
            else:
                reason_codes.append("TP1_HIT")
                partial_size = position.remaining_qty * self.tp1_partial_pct
                client_order_id = f"exit-tp1-{position.position_id}"
                
                actions.append(ManagementAction(
                    type=ActionType.CLOSE_PARTIAL,
                    symbol=symbol,
                    reason=f"TP1 Hit ({position.initial_tp1_price})",
                    side=position.side,
                    size=partial_size,
                    order_type=OrderType.MARKET,
                    client_order_id=client_order_id,
                    position_id=position.position_id,
                    exit_reason=ExitReason.TAKE_PROFIT_1,
                    priority=60
                ))
            
                # CONDITIONAL BE (after TP1 fill confirmed by event, not here)
                reason_codes.append("TP1_PARTIAL_QUEUED")
        
        # ========== RULE 9: TRAILING STOP ==========
        if position.break_even_triggered and current_atr:
            if os.environ.get("TRADING_TRAILING_ENABLED", "true").lower() == "true":
                new_trail = self._calculate_trailing_stop(position, current_price, current_atr)
                
                if new_trail and position._validate_stop_move(new_trail):
                    pct_move = abs(new_trail - position.current_stop_price) / position.current_stop_price
                    if pct_move > Decimal("0.001"):  # 0.1% min move
                        reason_codes.append("TRAILING_UPDATE")
                        client_order_id = f"stop-trail-{position.position_id}"
                        
                        actions.append(ManagementAction(
                            type=ActionType.UPDATE_STOP,
                            symbol=symbol,
                            reason=f"Trailing Stop Update",
                            side=position.side,
                            price=new_trail,
                            client_order_id=client_order_id,
                            position_id=position.position_id,
                            priority=20
                        ))
                        self.metrics["stop_moves"] += 1
        
        # Sort by priority
        actions.sort(key=lambda a: a.priority, reverse=True)
        
        if not reason_codes:
            reason_codes.append("NO_ACTION")
        
        self._record_decision(symbol, current_price, position, actions, reason_codes)
        
        return actions
    
    def _calculate_trailing_stop(
        self,
        position: ManagedPosition,
        current_price: Decimal,
        current_atr: Decimal
    ) -> Optional[Decimal]:
        """Calculate trailing stop using ATR."""
        trail_distance = current_atr * self.trailing_atr_multiple
        
        if position.side == Side.LONG:
            new_stop = current_price - trail_distance
            if new_stop <= position.current_stop_price:
                return None
            if position.break_even_triggered and position.avg_entry_price:
                if new_stop < position.avg_entry_price:
                    return None
            return new_stop
        else:
            new_stop = current_price + trail_distance
            if new_stop >= position.current_stop_price:
                return None
            if position.break_even_triggered and position.avg_entry_price:
                if new_stop > position.avg_entry_price:
                    return None
            return new_stop
    
    # ========== ORDER EVENT HANDLING ==========
    
    def handle_order_event(self, symbol: str, event: OrderEvent) -> List[ManagementAction]:
        """
        Handle order event and potentially trigger follow-up actions.
        
        This is the feedback loop from Execution Gateway.
        State transitions are driven by events, not intent.
        """
        result = self.registry.apply_order_event(symbol, event)
        if not result:
            return []  # Duplicate or N/A
        
        position = self.registry.get_position(symbol)
        if position is None:
            return []
        
        follow_up_actions: List[ManagementAction] = []
        
        # Handle entry acknowledgement → place stop
        if event.event_type == OrderEventType.ACKNOWLEDGED:
            if event.order_id == position.entry_order_id:
                # Entry ack → stop placement will happen after fill
                pass
        
        # Handle entry fill → place stop order
        if event.event_type in (OrderEventType.FILLED, OrderEventType.PARTIAL_FILL):
            if event.order_id == position.entry_order_id:
                # Entry filled (or partial) → ensure stop is placed
                if not position.stop_order_id:
                    client_order_id = f"stop-initial-{position.position_id}"
                    follow_up_actions.append(ManagementAction(
                        type=ActionType.PLACE_STOP,
                        symbol=symbol,
                        reason="Initial stop after entry fill",
                        side=position.side,
                        price=position.current_stop_price,
                        size=position.remaining_qty,
                        client_order_id=client_order_id,
                        position_id=position.position_id,
                        priority=100
                    ))
        
        # Handle exit fill → check for BE trigger
        if event.event_type in (OrderEventType.FILLED, OrderEventType.PARTIAL_FILL):
            if event.order_id != position.entry_order_id:
                # Exit fill → check conditional BE
                if position.tp1_filled and position.should_trigger_break_even():
                    if position.trigger_break_even():
                        client_order_id = f"stop-be-{position.position_id}"
                        follow_up_actions.append(ManagementAction(
                            type=ActionType.UPDATE_STOP,
                            symbol=symbol,
                            reason="Break-even after TP1 fill (conditional)",
                            side=position.side,
                            price=position.avg_entry_price,
                            client_order_id=client_order_id,
                            position_id=position.position_id,
                            priority=90
                        ))
                        self.metrics["stop_moves"] += 1
        
        # Handle full close
        if position.is_terminal:
            self.metrics["closes"] += 1
            # Cancel any remaining TP orders if position closed
            if position.tp1_order_id and not position.tp1_filled:
                follow_up_actions.append(ManagementAction(
                    type=ActionType.CANCEL_TP,
                    symbol=symbol,
                    reason="Position closed, cancel TP",
                    client_order_id=position.tp1_order_id,
                    position_id=position.position_id,
                    priority=50
                ))
        
        return follow_up_actions
    
    # ========== REVERSAL HANDLING ==========
    
    def request_reversal(
        self,
        symbol: str,
        new_side: Side,
        current_price: Decimal
    ) -> List[ManagementAction]:
        """
        Request position close for direction reversal.
        """
        position = self.registry.get_position(symbol)
        if position is None:
            return []
        
        if position.side == new_side:
            return []  # Not a reversal
            
        # SAFETY CHECK: Reversals enabled?
        if os.environ.get("TRADING_REVERSALS_ENABLED", "true").lower() != "true":
            logger.warning("Reversal BLOCKED by Global Switch", symbol=symbol)
            return []
        
        # Register reversal intent
        self.registry.request_reversal(symbol, new_side)
        self.metrics["reversals_attempted"] += 1
        
        client_order_id = f"exit-reversal-{position.position_id}"
        
        return [ManagementAction(
            type=ActionType.CLOSE_FULL,
            symbol=symbol,
            reason=f"Direction reversal: {position.side.value} → {new_side.value}",
            side=position.side,
            size=position.remaining_qty,
            price=current_price,
            order_type=OrderType.MARKET,
            client_order_id=client_order_id,
            position_id=position.position_id,
            exit_reason=ExitReason.DIRECTION_REVERSAL,
            priority=95
        )]
    
    # ========== RECONCILIATION ==========
    
    def reconcile(
        self,
        exchange_positions: Dict[str, Dict],
        exchange_orders: List[Dict]
    ) -> List[ManagementAction]:
        """
        Reconcile with exchange and return corrective actions.
        """
        issues = self.registry.reconcile_with_exchange(exchange_positions, exchange_orders)
        actions: List[ManagementAction] = []
        
        for symbol, issue in issues:
            if "ORPHANED" in issue:
                # Registry thinks position exists, exchange doesn't
                # This is dangerous - position might have been liquidated
                pos = self.registry.get_position(symbol)
                if pos:
                    pos.mark_orphaned()
                    actions.append(ManagementAction(
                        type=ActionType.NO_ACTION,
                        symbol=symbol,
                        reason=f"ORPHANED: {issue}",
                        priority=0
                    ))
                    self.metrics["errors"] += 1
            
            elif "PHANTOM" in issue:
                # Exchange has position we don't know about - FLATTEN
                actions.append(ManagementAction(
                    type=ActionType.FLATTEN_ORPHAN,
                    symbol=symbol,
                    reason=f"PHANTOM position on exchange",
                    order_type=OrderType.MARKET,
                    exit_reason=ExitReason.ORPHAN_FLATTEN,
                    priority=100
                ))
                self.metrics["errors"] += 1
            
            elif "QTY_MISMATCH" in issue:
                # Qty mismatch - log but don't auto-correct
                logger.error(f"QTY MISMATCH: {symbol} - {issue}")
                self.metrics["errors"] += 1
        
        return actions
    
    # ========== SHADOW MODE ==========
    
    def _record_decision(
        self,
        symbol: str,
        current_price: Decimal,
        position: Optional[ManagedPosition],
        actions: List[ManagementAction],
        reason_codes: List[str]
    ) -> None:
        """Record decision tick for shadow mode comparison."""
        tick = DecisionTick(
            timestamp=datetime.now(timezone.utc),
            symbol=symbol,
            current_price=current_price,
            position_state=position.state.value if position else None,
            position_id=position.position_id if position else None,
            remaining_qty=position.remaining_qty if position else None,
            current_stop=position.current_stop_price if position else None,
            actions=actions,
            reason_codes=reason_codes
        )
        
        self.decision_history.append(tick)
        
        # Trim history
        if len(self.decision_history) > self.max_history:
            self.decision_history = self.decision_history[-self.max_history:]
        
        if self.shadow_mode and actions:
            logger.info(
                "[SHADOW] Would execute",
                symbol=symbol,
                actions=[a.type.value for a in actions],
                reasons=reason_codes
            )
    
    def get_shadow_metrics(self) -> Dict:
        """Get metrics from shadow mode for comparison."""
        return {
            "total_decisions": len(self.decision_history),
            "metrics": self.metrics.copy(),
            "action_counts": self._count_actions(),
            "state_distribution": self._state_distribution()
        }
    
    def _count_actions(self) -> Dict[str, int]:
        """Count actions by type in history."""
        counts: Dict[str, int] = {}
        for tick in self.decision_history:
            for action in tick.actions:
                counts[action.type.value] = counts.get(action.type.value, 0) + 1
        return counts
    
    def _state_distribution(self) -> Dict[str, int]:
        """Count state occurrences in history."""
        states: Dict[str, int] = {}
        for tick in self.decision_history:
            if tick.position_state:
                states[tick.position_state] = states.get(tick.position_state, 0) + 1
        return states
    
    def export_decision_history(self, limit: int = 1000) -> List[Dict]:
        """Export decision history for analysis."""
        return [t.to_dict() for t in self.decision_history[-limit:]]

    # ========== SAFETY & MAINTENANCE ==========
    
    def check_safety(self) -> List[ManagementAction]:
        """
        Run periodic safety checks.
        
        1. Exit Timeouts & Escalation
        """
        from src.execution.production_safety import ExitEscalationLevel
        
        actions: List[ManagementAction] = []
        
        # 1. Update Exit Timeout States
        # Ensure we are tracking all pending exits
        for pos in self.registry.get_all_active():
            if pos.state == PositionState.EXIT_PENDING:
                self.exit_timeout_manager.start_exit_tracking(pos)
        
        # 2. Check Timeouts
        escalations = self.exit_timeout_manager.check_timeouts()
        
        for state in escalations:
            new_level = self.exit_timeout_manager.escalate(state.symbol)
            
            if new_level in (ExitEscalationLevel.AGGRESSIVE, ExitEscalationLevel.EMERGENCY):
                # Fetch position to get side (needed for ManagementAction, though simple close shouldn't need it if robust)
                pos = self.registry.get_position(state.symbol)
                side = pos.side if pos else Side.LONG # Fallback
                
                # Escalate to Market Close
                actions.append(ManagementAction(
                    type=ActionType.CLOSE_FULL,
                    symbol=state.symbol,
                    reason=f"Exit Timeout: Escalating to {new_level.value}",
                    side=side, 
                    order_type=OrderType.MARKET,
                    priority=200 # Higher than signal exits
                ))
                
            elif new_level == ExitEscalationLevel.QUARANTINE:
                logger.critical("QUARANTINING SYMBOL due to Exit Timeout", symbol=state.symbol)
                # Could emit a quarantine action if supported
        
        return actions
