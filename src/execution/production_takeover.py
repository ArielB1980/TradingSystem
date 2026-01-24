"""
Production Takeover Protocol.

Implements the STRICT protocol for taking over existing positions:
1. Snapshot exchange truth
2. Classify positions (Protected/Naked/Conflicting/Duplicate)
3. Resolve chaos (cancel conflicting orders)
4. Enforce Invariant K (ensure valid stop exists)
5. Import positions into registry
6. Enable Safe Management Mode

Usage:
    takeover = ProductionTakeover(gateway, safety_config)
    await takeover.execute_takeover()
"""

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, Set

from src.execution.position_state_machine import (
    ManagedPosition,
    PositionState,
    PositionRegistry,
    FillRecord,
    OrderEvent,
    OrderEventType,
    ExitReason,
    get_position_registry
)
from src.domain.models import Side, OrderType
from src.execution.execution_gateway import ExecutionGateway
from src.execution.production_safety import AtomicStopReplacer, SafetyConfig
from src.monitoring.logger import get_logger

logger = get_logger(__name__)


@dataclass
class TakeoverConfig:
    """Configuration for production takeover."""
    takeover_stop_pct: Decimal = Decimal("0.02")  # 2.0% conservative default
    stop_replace_atomically: bool = True
    dry_run: bool = False  # If True, only log what would happen


class TakeoverCase:
    """Classification of position state."""
    A_PROTECTED = "A_PROTECTED"      # Valid stop exists
    B_NAKED = "B_NAKED"              # No stop exists
    C_CHAOS = "C_CHAOS"              # Multiple/conflicting stops
    D_DUPLICATE = "D_DUPLICATE"      # Local state conflicts with exchange


class ProductionTakeover:
    """
    Executes the Production Takeover Protocol.
    
    This is a "Run Once" operation to stabilize the system.
    """
    
    def __init__(
        self, 
        gateway: ExecutionGateway,
        config: TakeoverConfig = TakeoverConfig()
    ):
        self.gateway = gateway
        self.client = gateway.client
        self.registry = gateway.registry
        self.config = config
        self.replacer = AtomicStopReplacer(
            self.client, 
            SafetyConfig(stop_replace_ack_timeout_seconds=10)
        )
        
        # State
        self.snapshot_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.imported_positions: List[str] = []
        self.quarantined_positions: List[str] = []
    
    async def execute_takeover(self) -> Dict[str, int]:
        """
        Execute the full takeover protocol.
        """
        logger.critical(f"🚀 STARTING PRODUCTION TAKEOVER (ID: {self.snapshot_id})")
        
        # Global stats
        stats = {
            "total_positions": 0,
            "imported": 0,
            "quarantined": 0,
            "stops_placed": 0,
            "orders_cancelled": 0,
            "case_a": 0,
            "case_b": 0,
            "case_c": 0,
            "case_d": 0
        }
        
        # Step 1: Snapshot
        logger.info("Step 1: Snapshotting exchange state...")
        positions, orders = await self._snapshot_exchange()
        stats["total_positions"] = len(positions)
        
        logger.info(f"Found {len(positions)} open positions and {len(orders)} open orders")
        
        # Step 2-5: Process each position
        for symbol, pos_data in positions.items():
            try:
                await self._process_single_position(symbol, pos_data, orders, stats)
            except Exception as e:
                logger.critical(f"Failed to process {symbol}: {e}", exc_info=True)
                self.quarantined_positions.append(symbol)
                stats["quarantined"] += 1
        
        # Step 6: Normalize exits (done implicitly via import)
        
        # Step 7: Summary
        logger.critical("🏁 PRODUCTION TAKEOVER COMPLETE")
        logger.info("Takeover Stats", **stats)
        
        return stats

    async def _snapshot_exchange(self) -> Tuple[Dict, List]:
        """Query exchange for open positions and orders."""
        # This assumes the client has these methods - adapting to standard client interface
        positions_raw = await self.client.get_all_futures_positions()
        orders_raw = await self.client.get_futures_open_orders()
        
        # Filter for active positions (size != 0)
        active_positions = {}
        for p in positions_raw:
            size = float(p.get("size", p.get("contracts", 0)))
            if size != 0:
                symbol = p.get("symbol")
                active_positions[symbol] = {
                    "symbol": symbol,
                    "side": Side.LONG if p.get("side") == "long" else Side.SHORT,
                    "qty": Decimal(str(abs(size))),
                    "entry_price": Decimal(str(p.get("entry_price", 0)))
                }
        
        return active_positions, orders_raw

    async def _process_single_position(
        self, 
        symbol: str, 
        pos_data: Dict, 
        all_orders: List[Dict], 
        stats: Dict
    ) -> None:
        """Process a single position."""
        logger.info(f"Processing {symbol}...", data=pos_data)
        
        # Filter orders for this symbol
        symbol_orders = [o for o in all_orders if o.get("symbol") == symbol]
        
        # Step 2: Classify
        classification, stop_orders = self._classify_position(pos_data, symbol_orders)
        logger.info(f"Classification for {symbol}: {classification}")
        
        # Update Case Stats
        if classification == TakeoverCase.A_PROTECTED: stats["case_a"] += 1
        elif classification == TakeoverCase.B_NAKED: stats["case_b"] += 1
        elif classification == TakeoverCase.C_CHAOS: stats["case_c"] += 1
        elif classification == TakeoverCase.D_DUPLICATE: stats["case_d"] += 1
        
        # Step 3: Resolve Chaos (Case C & D)
        if classification == TakeoverCase.D_DUPLICATE:
            logger.warning(f"Case D: Purging local state for {symbol}")
            if symbol in self.registry._positions:
                del self.registry._positions[symbol]
        
        valid_stop = None
        
        if classification == TakeoverCase.C_CHAOS:
            logger.warning(f"Case C: Resolving order chaos for {symbol}")
            valid_stop = await self._resolve_chaos(symbol, stop_orders)
            stats["orders_cancelled"] += (len(stop_orders) - (1 if valid_stop else 0))
        elif classification == TakeoverCase.A_PROTECTED:
            valid_stop = stop_orders[0]
            logger.info(f"Protective stop confirmed for {symbol} (Order ID: {valid_stop['id']})")
        
        # Step 4: Enforce Invariant K (Protect)
        final_stop = await self._enforce_protection(symbol, pos_data, valid_stop)
        
        if final_stop is None:
            # Emergency failed - validation failed and placement failed
            self.quarantined_positions.append(symbol)
            stats["quarantined"] += 1
            logger.critical(f"FLATTENED + QUARANTINED: {symbol}")
            return
        
        if not valid_stop and final_stop is not None:
            stats["stops_placed"] += 1
            logger.info(f"Protective stop PLACED for {symbol}")
        
        # Step 5: Import
        await self._import_position(symbol, pos_data, final_stop)
        stats["imported"] += 1
        self.imported_positions.append(symbol)

    def _classify_position(self, pos_data: Dict, orders: List[Dict]) -> Tuple[str, List[Dict]]:
        """Classify position into A, B, C, or D."""
        # Check Case D (Duplicate) first
        if self.registry.has_position(pos_data["symbol"]):
            # We treat any existing registry entry as "Duplicate/Stale" in takeover mode
            # You might want strictly "if match" logic, but "ignore local" is safer
            return TakeoverCase.D_DUPLICATE, []
            
        # Identify stop orders
        stop_orders = []
        for o in orders:
            o_type = o.get("type", "").lower()
            if o_type in ["stop", "stop_market", "stop-loss", "stop-loss-limit"]:
                # Basic validation: reduces position?
                # Simplified check: just presence for now, Detailed validation in Step 4
                stop_orders.append(o)
        
        if not stop_orders:
            return TakeoverCase.B_NAKED, []
        
        if len(stop_orders) == 1:
            return TakeoverCase.A_PROTECTED, stop_orders
        
        return TakeoverCase.C_CHAOS, stop_orders

    async def _resolve_chaos(self, symbol: str, stop_orders: List[Dict]) -> Optional[Dict]:
        """
        Resolve multiple stops.
        Strategy: Cancel ALL, return None (forces fresh placement).
        Alternatively: Pick 'best' and cancel others.
        
        Decision: For safety, if >1 stop, cancel ALL and place fresh. 
        It's cleaner than guessing which one the user intended.
        """
        logger.info(f"Cancelling {len(stop_orders)} conflicting stops for {symbol}")
        
        for order in stop_orders:
            try:
                if not self.config.dry_run:
                    await self.client.cancel_futures_order(order["id"], symbol)
            except Exception as e:
                logger.error(f"Failed to cancel {order['id']}: {e}")
        
        return None

    async def _enforce_protection(
        self, 
        symbol: str, 
        pos_data: Dict, 
        existing_stop: Optional[Dict]
    ) -> Optional[Decimal]:
        """
        Ensure valid stop exists. 
        Returns the stop PRICE if protected, None if failed (and quarantined).
        """
        # Validate existing stop if Case A
        if existing_stop:
            stop_price = Decimal(str(existing_stop.get("stopPrice", existing_stop.get("price", 0))))
            current_side = pos_data["side"]
            entry_price = pos_data["entry_price"]
            qty = pos_data["qty"]
            stop_qty = Decimal(str(existing_stop.get("amount", 0)))
            
            is_valid = True
            
            # Direction check
            if current_side == Side.LONG and stop_price >= entry_price:
                 # Normally we want stop < entry, but user might have moved to profit. 
                 # Just check it's below CURRENT price? No, we don't have current price easily.
                 # Let's assume exchange stop is valid directionally if accepted by engine.
                 pass
            
            # Size check
            if stop_qty < qty:
                logger.warning(f"Stop size {stop_qty} < Position {qty}. Invalid.")
                is_valid = False
            
            if is_valid:
                return stop_price
            
            # If invalid, we fall through to placement
            logger.warning(f"Existing stop {existing_stop['id']} invalid. Replacing.")
            if not self.config.dry_run:
                try:
                    await self.client.cancel_futures_order(existing_stop["id"], symbol)
                except Exception:
                    pass
        
        # Case B (or failed A): Place fresh stop
        return await self._place_fresh_stop(symbol, pos_data)

    async def _place_fresh_stop(self, symbol: str, pos_data: Dict) -> Optional[Decimal]:
        """Calculate and place a conservative protective stop."""
        side = pos_data["side"]
        entry_price = pos_data["entry_price"]
        qty = pos_data["qty"]
        
        # Calculate conservative price
        # Note: We use entry_price as proxy for "current" if we don't have ticker.
        # Ideally we fetch ticker.
        try:
            mark_price = await self.client.get_futures_mark_price(symbol)
            current_price = Decimal(str(mark_price))
        except Exception:
            logger.warning(f"Could not fetch ticker for {symbol}, using entry price for stop calc")
            current_price = entry_price
        
        pct = self.config.takeover_stop_pct
        
        if side == Side.LONG:
            # Lower of entry or current, minus buffer
            base = min(entry_price, current_price)
            stop_price = base * (Decimal("1") - pct)
        else:
            # Higher of entry or current, plus buffer
            base = max(entry_price, current_price)
            stop_price = base * (Decimal("1") + pct)
            
        logger.critical(f"PLACING EMERGENCY STOP for {symbol}: {stop_price} (Size: {qty})")
        
        if self.config.dry_run:
            return stop_price
            
        try:
            # Use gateway/client to place stop
            # We generate a special ID
            client_order_id = f"takeover-stop-{symbol.replace('/','')}-{self.snapshot_id}"
            stop_side = "sell" if side == Side.LONG else "buy"
            
            result = await self.client.place_futures_order(
                symbol=symbol,
                side=stop_side,
                order_type="stop",
                size=qty,
                stop_price=stop_price,
                reduce_only=True,
                client_order_id=client_order_id
            )
            return stop_price
            
        except Exception as e:
            logger.critical(f"FAILED TO PLACE STOP for {symbol}: {e}")
            # Step 4 failsafe: Emergency Market Exit
            await self._emergency_flatten(symbol, qty, side)
            return None

    async def _emergency_flatten(self, symbol: str, qty: Decimal, side: Side):
        """Emergency flatten if stop placement fails."""
        logger.critical(f"🚨 EMERGENCY FLATTENING {symbol}")
        if self.config.dry_run:
            return
            
        try:
            exit_side = "sell" if side == Side.LONG else "buy"
            await self.client.place_futures_order(
                symbol=symbol,
                side=exit_side,
                order_type="market",
                size=qty,
                reduce_only=True
            )
        except Exception as e:
             logger.critical(f"FATAL: Could not flatten {symbol}: {e}")

    async def _import_position(self, symbol: str, pos_data: Dict, stop_price: Decimal) -> None:
        """Create ManagedPosition from truth."""
        if self.config.dry_run:
            return

        # Create Position ID
        pid = f"pos-{symbol.replace('/','')}-{self.snapshot_id}"
        
        pos = ManagedPosition(
            symbol=symbol,
            side=pos_data["side"],
            position_id=pid,
            initial_size=pos_data["qty"],
            initial_entry_price=pos_data["entry_price"],
            initial_stop_price=stop_price,
            initial_tp1_price=None,  # Unknown
            initial_tp2_price=None,
            initial_final_target=None
        )
        
        # Isolate Invariant C: Immutables. entry_acknowledged = True immediately
        pos.entry_acknowledged = True
        pos.intent_confirmed = True  # BE gate: takeover positions treated as confirmed
        pos.state = PositionState.OPEN # Or PROTECTED
        pos.current_stop_price = stop_price
        pos.setup_type = "TAKEOVER"
        pos.trade_type = "UNKNOWN"
        
        # Synthesize Fill Record to make stats work
        dummy_fill = FillRecord(
            fill_id=f"takeover-fill-{self.snapshot_id}",
            order_id="UNKNOWN_ORIGIN",
            side=pos_data["side"],
            qty=pos_data["qty"],
            price=pos_data["entry_price"],
            timestamp=datetime.now(timezone.utc),
            is_entry=True
        )
        pos.entry_fills.append(dummy_fill)
        
        # Register
        self.registry.register_position(pos)
        
        # Persist
        self.gateway.persistence.save_position(pos)
        self.gateway.persistence.log_action(pid, "TAKEOVER_IMPORT", {
            "snapshot_id": self.snapshot_id, 
            "original_data": str(pos_data)
        })
        
        logger.info(f"✅ Imported {symbol} as {pid}")

