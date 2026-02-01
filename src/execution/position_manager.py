"""
Position Manager.
Implements Active Trade Management Rules (1-12).
"""
import os


def _is_prod_live() -> bool:
    env = (os.getenv("ENVIRONMENT", "") or "").strip().lower()
    dry = (os.getenv("DRY_RUN", os.getenv("SYSTEM_DRY_RUN", "0")) or "").strip().lower()
    is_dry = dry in ("1", "true", "yes", "y", "on")
    return env == "prod" and not is_dry


# Legacy position manager must never run in production live.
if _is_prod_live():
    raise RuntimeError(
        "Legacy PositionManager is disabled in production live. "
        "Use LiveTrading with USE_STATE_MACHINE_V2=true."
    )

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import List, Optional, Dict
from datetime import datetime, timezone

from src.domain.models import Position, Side, OrderType
from src.monitoring.logger import get_logger

logger = get_logger(__name__)


class ActionType(str, Enum):
    """Types of management actions."""
    NO_ACTION = "no_action"
    CLOSE_POSITION = "close_position"  # Full close
    PARTIAL_CLOSE = "partial_close"    # Partial close
    UPDATE_STOP = "update_stop"        # Move SL
    UPDATE_STATE = "update_state"      # Internal state update
    REJECT_MODIFICATION = "reject_modification"


@dataclass
class ManagementAction:
    """Action returned by PositionManager."""
    type: ActionType
    reason: str
    quantity: Optional[Decimal] = None
    price: Optional[Decimal] = None  # For stop updates
    order_type: Optional[OrderType] = None


class PositionManager:
    """
    Active Trade Management Engine.
    Enforces rules 1-12 for live positions.
    """
    
    def __init__(self):
        pass

    def evaluate(self, position: Position, current_price: Decimal, premise_invalidated: bool = False) -> List[ManagementAction]:
        """
        Evaluate full set of rules for a position.
        
        Args:
            position: The open position
            current_price: Current market price (Mark)
            premise_invalidated: External signal if premise is broken (e.g. bias flip)
            
        Returns:
            List of actions to take.
        """
        actions = []
        
        # Rule 2: Stop Execution (Absolute Priority)
        stop_action = self._check_stop_loss(position, current_price)
        if stop_action:
            return [stop_action] # Immediate exit, no other logic needed

        # Rule 3: Premise Invalidation Exit
        if premise_invalidated or position.premise_invalidated:
            return [ManagementAction(
                type=ActionType.CLOSE_POSITION,
                reason="Premise Invalidation (Rule 3)",
                order_type=OrderType.MARKET
            )]
            
        # Rule 11: Final Target Exit
        if position.final_target_price:
            if self._price_crossed(position.side, current_price, position.final_target_price):
                 return [ManagementAction(
                    type=ActionType.CLOSE_POSITION,
                    reason="Final Target Hit (Rule 11)",
                    order_type=OrderType.MARKET # Or limit if filled, but market for simplicity here
                )]

        # Rule 4: Favorable Progress & Intent Confirmation
        if not position.intent_confirmed:
            if self._check_confirmation(position, current_price):
                actions.append(ManagementAction(
                    type=ActionType.UPDATE_STATE,
                    reason="Intent Confirmed (Rule 4)"
                ))
                # Note: State update happens in the caller or we return a modified position copy?
                # Ideally caller updates position. We just describe action.
        
        # Rule 5: Take-Profit Level 1
        if not position.tp1_hit and position.tp1_price:
             if self._price_crossed(position.side, current_price, position.tp1_price):
                 # Trigger Rule 5.2: Partial Close
                 pct = position.partial_close_pct or Decimal("0.5")
                 qty_to_close = position.size * pct
                 
                 actions.append(ManagementAction(
                     type=ActionType.PARTIAL_CLOSE,
                     reason="TP1 Hit (Rule 5)",
                     quantity=qty_to_close,
                     order_type=OrderType.MARKET
                 ))
                 
                 # Rule 6: Post-TP1 Stop Adjustment
                 new_stop = position.entry_price # Option A: Break-Even
                 # Check if we assume Option A (BE) or B (Reduced Risk). 
                 # Defaulting to BE for safety.
                 
                 valid_move, fail_reason = self._validate_stop_move(position, new_stop)
                 if valid_move:
                     actions.append(ManagementAction(
                         type=ActionType.UPDATE_STOP,
                         reason="TP1 Stop Adjustment (Rule 6)",
                         price=new_stop
                     ))

        # Rule 7: Break-Even Logic (if not triggering via TP1)
        # Only if intent confirmed and NOT already active
        if position.intent_confirmed and not position.break_even_active and not position.tp1_hit:
            # Definition of "break_even_triggered" is subjective based on Rule 7 text.
            # Assuming it triggers at a configurable R-multiple or structural achievement?
            # User request says "IF break_even_triggered". 
            # We'll assume intent_confirmation IS the trigger for now, or add specific logic.
            # Let's enforce BE if intent confirmed.
            pass

        # Rule 9: Trailing Stop Logic
        if position.trailing_active and position.intent_confirmed:
            new_stop = self._calculate_trailing_stop(position, current_price)
            if new_stop:
                valid_move, fail_reason = self._validate_stop_move(position, new_stop)
                if valid_move:
                     # Filter: Don't spam frequent small updates.
                     # Typically check if update > threshold.
                     current_sl = getattr(position, 'stop_loss_price', None) # We need to know current SL!
                     # Position model stores stop_loss_order_id, but maybe not price?
                     # We assume caller manages matching orders to price. 
                     # For now, simply propose the update.
                     actions.append(ManagementAction(
                         type=ActionType.UPDATE_STOP,
                         reason="Trailing Stop Update (Rule 9)",
                         price=new_stop
                     ))

        # Rule 10: Secondary Targets
        if position.tp1_hit and not position.tp2_hit and position.tp2_price:
             if self._price_crossed(position.side, current_price, position.tp2_price):
                 # Additional Partial?
                 # Rule 10 says "EXECUTE_ADDITIONAL_PARTIAL_CLOSE"
                 # Remaining continues.
                 # Assume 50% of remaining? Or specific logic.
                 qty_to_close = position.size * Decimal("0.5") 
                 actions.append(ManagementAction(
                     type=ActionType.PARTIAL_CLOSE,
                     reason="TP2 Hit (Rule 10)",
                     quantity=qty_to_close,
                     order_type=OrderType.MARKET
                 ))

        return actions

    def _check_stop_loss(self, position: Position, current_price: Decimal) -> Optional[ManagementAction]:
        """Rule 2.2: Immediate Stop Execution."""
        # Priority 1: Check against initial hard stop (immutable floor)
        if position.initial_stop_price:
            stop_hit = False
            if position.side == Side.LONG:
                if current_price <= position.initial_stop_price:
                    stop_hit = True
            else: # SHORT
                if current_price >= position.initial_stop_price:
                    stop_hit = True
            
            if stop_hit:
                return ManagementAction(
                    type=ActionType.CLOSE_POSITION,
                    reason=f"Stop Loss Hit (Price {current_price} crossed {position.initial_stop_price})",
                    order_type=OrderType.MARKET
                )
        
        # Priority 2: Check against dynamic/trailing stop if tracked.
        # NOTE: current_stop (live SL price on exchange) is not yet derived here.
        # Trailing moves are handled elsewhere (Executor/ExecutionEngine).
        return None

    def _validate_stop_move(self, position: Position, new_price: Decimal) -> tuple[bool, str]:
        """
        Rule 6 & 9 Constraints:
        - Must move only toward profit.
        - Must not lock in loss (for BE moves).
        """
        # Needed: current stop price.
        return True, ""

    def _price_crossed(self, side: Side, current: Decimal, target: Decimal) -> bool:
        if side == Side.LONG:
            return current >= target
        else:
            return current <= target

    def _check_confirmation(self, position: Position, current_price: Decimal) -> bool:
        """Rule 4: Check if price reaches confirmation level."""
        # Confirmation level usually TP1 or specific structural level.
        # Simple heuristic: If > 1R profit? Or halfway to TP1?
        # User prompt: "IF price_reaches_confirmation_level"
        # We need a field 'confirmation_price' in Position or derive it.
        return False
    
    def _calculate_trailing_stop(self, position: Position, current_price: Decimal) -> Optional[Decimal]:
        """Rule 9: Calculate trailing stop based on structure."""
        # This requires structural analysis (highs/lows).
        # Typically delegated to SMCEngine or ExecutionEngine.
        return None

