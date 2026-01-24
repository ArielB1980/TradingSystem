"""
Execution Engine for converting SMC Spot Signals into Futures Orders.
Handles:
- Spot -> Futures Price Conversion
- Multi-Take Profit Generation (Structure + RR Fallback)
- Stop Loss Management (Reduce-only)
- Dynamic State Management (Break-even, Trailing Stops)
"""
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN
from typing import List, Optional, Tuple, Dict
from datetime import datetime, timezone

from src.config.config import Config
from src.domain.models import (
    Signal, SignalType, Order, OrderType, OrderStatus, 
    Side, Position
)
from src.monitoring.logger import get_logger

logger = get_logger(__name__)

class ExecutionEngine:
    def __init__(self, config: Config):
        self.config = config.execution
        self.strategy_config = config.strategy
        mtp = getattr(config, "multi_tp", None)

        if mtp and getattr(mtp, "enabled", False):
            self._tp_splits = [mtp.tp1_close_pct, mtp.tp2_close_pct, mtp.runner_pct]
            self._rr_fallback_multiples = [mtp.tp1_r_multiple, mtp.tp2_r_multiple, 3.0]
            logger.info("ExecutionEngine using multi_tp config", tp_splits=self._tp_splits, rr_multiples=self._rr_fallback_multiples)
        else:
            self._tp_splits = list(self.config.tp_splits)
            self._rr_fallback_multiples = list(self.config.rr_fallback_multiples)

        self.price_precision = Decimal("0.1")
        self.qty_precision = Decimal("0.001")
        
    def generate_entry_plan(
        self, 
        signal: Signal, 
        size_notional: Decimal,
        spot_price: Decimal,
        mark_price: Decimal,
        leverage: Decimal
    ) -> Dict[str, any]:
        """
        Generate complete order package: Entry + SL + TPs.
        Returns a dict of orders/intents.
        """
        # 1. Price Conversion (Spot -> Futures)
        # Calculate Spot percentages
        if signal.signal_type == SignalType.LONG:
            sl_pct = (signal.entry_price - signal.stop_loss) / signal.entry_price
        else:
            sl_pct = (signal.stop_loss - signal.entry_price) / signal.entry_price
            
        # Apply to Futures Mark Price ("Reference Price")
        # Entry assumed at current mark price for market orders
        fut_entry = mark_price
        
        if signal.signal_type == SignalType.LONG:
            fut_sl = fut_entry * (Decimal("1") - sl_pct)
        else:
            fut_sl = fut_entry * (Decimal("1") + sl_pct)
            
        # 2. Generate TP Ladder (Futures prices)
        tps = self._generate_tp_ladder(signal, fut_entry, fut_sl, signal.signal_type)
        
        # 3. Calculate Quantities
        size_qty = size_notional / fut_entry
        tp_quantities = self._split_quantities(size_qty, len(tps))
        
        # 4. Construct Orders (Intents only, ID generation happens at placement)
        # Entry
        entry_side = Side.LONG if signal.signal_type == SignalType.LONG else Side.SHORT
        entry_order = {
            "type": "entry",
            "side": entry_side,
            "order_type": self.config.default_order_type, # Market or Limit
            "qty": size_qty,
            "price": fut_entry if self.config.default_order_type == "limit" else None,
            "leverage": leverage
        }
        
        # Stop Loss
        sl_order = {
            "type": "stop_loss",
            "side": Side.SHORT if entry_side == Side.LONG else Side.LONG,
            "order_type": OrderType.STOP_LOSS,
            "qty": size_qty, # Full position size
            "price": fut_sl,
            "reduce_only": True,
            "trigger_price": fut_sl
        }
        
        # Take Profits
        tp_orders = []
        for i, (tp_price, tp_qty) in enumerate(zip(tps, tp_quantities)):
            tp_orders.append({
                "type": f"tp_{i+1}",
                "side": Side.SHORT if entry_side == Side.LONG else Side.LONG,
                "order_type": OrderType.TAKE_PROFIT,
                "qty": tp_qty,
                "price": tp_price,
                "reduce_only": True,
                "trigger_price": tp_price
            })
            
        return {
            "entry": entry_order,
            "stop_loss": sl_order,
            "take_profits": tp_orders,
            "metadata": {
                "fut_entry": fut_entry,
                "fut_sl": fut_sl,
                "sl_pct": sl_pct
            }
        }
        
    def check_break_even(
        self, 
        position: Position, 
        current_mark_price: Decimal
    ) -> Optional[Decimal]:
        """
        Check if BE should be triggered.
        Returns NEW Stop Loss price if update needed, else None.
        Default trigger: TP1 Fill (checked by caller).
        """
        if position.break_even_active:
            return None # Already moved to BE
            
        # Calculate new SL
        # Offset to cover fees
        tick_size = self.price_precision 
        offset = tick_size * Decimal(str(self.config.break_even_offset_ticks))
        
        new_sl = None
        if position.side == Side.LONG:
            new_sl = position.entry_price + offset
            # Validate: must be higher than current SL (tightening)
             # We need current SL price, assumed caller has it or we look it up.
             # For now, we return candidate. Caller validates "tightening".
        else:
            new_sl = position.entry_price - offset
            
        return new_sl

    def check_trailing_stop(
        self,
        position: Position,
        current_mark_price: Decimal,
        current_atr_spot: Decimal,
        current_spot_price: Decimal,
        current_sl_price: Decimal
    ) -> Optional[Decimal]:
        """
        Calculate Trailing Stop update.
        Uses ATR from Spot applied to Futures Mark Price.
        """
        if not position.trailing_active:
            return None
            
        # 1. Convert Spot ATR to %
        atr_pct = current_atr_spot / current_spot_price
        
        # 2. Apply to Futures Mark
        atr_fut = current_mark_price * atr_pct
        trail_dist = atr_fut * Decimal(str(self.config.trailing_atr_mult))
        
        # 3. Calculate Candidate SL
        candidate_sl = None
        
        if position.side == Side.LONG:
            # Trailing from Peak High
            peak = position.peak_price if position.peak_price else current_mark_price
            candidate_sl = peak - trail_dist
            
            # Update peak if simplified logic here (caller manages state usually, 
            # but let's assume peak passed in is valid).
            
            # Validate Tightening
            # Must be > current_sl + min_step
            min_step = self.price_precision * Decimal(str(self.config.trailing_update_min_ticks))
            
            if candidate_sl > (current_sl_price + min_step):
                # Ensure we don't move SL above current price (safety, unlikely if peak is high)
                if candidate_sl < current_mark_price:
                    return candidate_sl
                    
        else: # SHORT
            # Trailing from Valley Low
            valley = position.peak_price if position.peak_price else current_mark_price
            candidate_sl = valley + trail_dist
            
            min_step = self.price_precision * Decimal(str(self.config.trailing_update_min_ticks))
            
            if candidate_sl < (current_sl_price - min_step):
                if candidate_sl > current_mark_price:
                     return candidate_sl
                     
        return None

    def _generate_tp_ladder(
        self, 
        signal: Signal, 
        fut_entry: Decimal, 
        fut_sl: Decimal, 
        side: SignalType
    ) -> List[Decimal]:
        """
        Generate 3 TP levels.
        Priority: Structure (Signal.tp_candidates) > RR Fallback.
        """
        tps = []
        candidates = signal.tp_candidates if hasattr(signal, "tp_candidates") else []
        
        # Calculate Risk (R)
        risk = abs(fut_entry - fut_sl)
        if risk == 0: risk = Decimal("1") # Edge case protection or config error
        
        multiples = self._rr_fallback_multiples
        fallbacks = []
        for m in multiples:
            m = Decimal(str(m))
            if side == SignalType.LONG:
                fallbacks.append(fut_entry + (risk * m))
            else:
                fallbacks.append(fut_entry - (risk * m))
                
        # Merge: Use candidates where available, else fill with fallback
        # Logic: If we have 2 candidates, use them as TP1, TP2, use fallback for TP3.
        # But we need to ensure they are "progressive" (further away).
        
        final_tps = []
        
        # Simple merge strategy: Fill slots 1, 2, 3
        num_slots = 3
        
        structure_idx = 0
        fallback_idx = 0
        
        # Sort candidates just in case (Nearest first)
        if side == SignalType.LONG:
            candidates = sorted([c for c in candidates if c > fut_entry])
        else:
            candidates = sorted([c for c in candidates if c < fut_entry], reverse=True)
            
        for i in range(num_slots):
            # Prefer structural candidate
            if structure_idx < len(candidates):
                # We have a candidate, but we need to convert it to futures price?
                # The candidates in Signal are SPOT prices.
                # We need to convert them same way as SL.
                
                spot_tp = candidates[structure_idx]
                # Calculate % dist from spot entry
                if side == SignalType.LONG:
                     dist_pct = (spot_tp - signal.entry_price) / signal.entry_price
                     fut_tp_candidate = fut_entry * (Decimal("1") + dist_pct)
                else:
                     dist_pct = (signal.entry_price - spot_tp) / signal.entry_price
                     fut_tp_candidate = fut_entry * (Decimal("1") - dist_pct)
                
                final_tps.append(fut_tp_candidate)
                structure_idx += 1
            else:
                # Use fallback
                # Ensure specific index fallback corresponds to specific TP (e.g. TP3 uses 3R)
                # Current loop index i maps to fallback index i
                final_tps.append(fallbacks[i])
        
        # Final sort to ensure logical order (e.g. if fallback TP3 < structure TP2 somehow)
        if side == SignalType.LONG:
            final_tps.sort()
        else:
            final_tps.sort(reverse=True)
            
        return final_tps

    def _split_quantities(self, total_qty: Decimal, num_tps: int) -> List[Decimal]:
        """Split quantities according to config splits."""
        splits = self._tp_splits
        # Ensure splits match num_tps
        if len(splits) != num_tps:
            # Fallback
            splits = [Decimal("1") / Decimal(str(num_tps))] * num_tps
        
        qtys = []
        remaining = total_qty
        
        for i in range(num_tps - 1):
            split_pct = Decimal(str(splits[i]))
            qty = total_qty * split_pct
            # Rounding (simplified)
            qty = round(qty, 4) 
            qtys.append(qty)
            remaining -= qty
            
        qtys.append(remaining) # Last one gets remainder to ensure exact sum
        return qtys
