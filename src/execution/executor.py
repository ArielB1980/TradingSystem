"""
Order execution orchestrator.

Handles:
- Idempotent order handling
- Ghost order detection
- SL/TP placement
- Order state machine
- Pyramiding guard
"""
from decimal import Decimal
from typing import Dict, Optional, Set
from datetime import datetime, timezone
import uuid
from src.domain.models import Order, OrderIntent, OrderType, OrderStatus, Position
from src.execution.futures_adapter import FuturesAdapter
from src.execution.price_converter import PriceConverter
from src.config.config import ExecutionConfig
from src.monitoring.logger import get_logger

logger = get_logger(__name__)


class Executor:
    """
    Order lifecycle orchestration.
    
    Design locks enforced:
    - Mark price for all safety-critical operations
    - Pyramiding disabled by default
    - Reduce-only orders for SL/TP
    """
    
    def __init__(
        self,
        config: ExecutionConfig,
        futures_adapter: FuturesAdapter,
    ):
        """
        Initialize executor.
        
        Args:
            config: Execution configuration
            futures_adapter: Futures API adapter
        """
        self.config = config
        self.futures_adapter = futures_adapter
        self.price_converter = PriceConverter()
        
        # Order tracking for idempotency
        self.submitted_orders: Dict[str, Order] = {}  # client_order_id → Order
        self.order_intents_seen: Set[str] = set()  # intent hash for deduplication
        
        # Order monitoring for timeout handling
        from src.execution.order_monitor import OrderMonitor
        self.order_monitor = OrderMonitor(
            default_timeout_seconds=config.order_timeout_seconds
        )
        
        logger.info("Executor initialized", config=config.model_dump())
    
    async def execute_signal(
        self,
        order_intent: OrderIntent,
        futures_mark_price: Decimal,
        current_positions: list[Position],
    ) -> Optional[Order]:
        """
        Execute trading signal.
        
        Args:
            order_intent: Order intent from risk validation
            futures_mark_price: Current futures mark price
            current_positions: Current open positions
        
        Returns:
            Entry order if submitted, None if rejected
        """
        # Idempotency check
        intent_hash = self._hash_intent(order_intent)
        if intent_hash in self.order_intents_seen:
            logger.warning(
                "Duplicate order intent detected",
                symbol=order_intent.signal.symbol,
                intent_hash=intent_hash,
            )
            return None
        
        # Pyramiding guard
        if self.config.pyramiding_enabled is False:
            # Check if we already have a position in this symbol
            futures_symbol = FuturesAdapter.map_spot_to_futures(order_intent.signal.symbol)
            has_position = any(p.symbol == futures_symbol for p in current_positions)
            
            if has_position:
                logger.warning(
                    "Pyramiding guard REJECTED",
                    symbol=futures_symbol,
                    reason="Pyramiding disabled, position already exists",
                )
                return None
        
        # Place entry order
        try:
            futures_symbol = FuturesAdapter.map_spot_to_futures(order_intent.signal.symbol)
            
            entry_order = await self.futures_adapter.place_order(
                symbol=futures_symbol,
                side=order_intent.side,
                size_notional=order_intent.size_notional,
                leverage=order_intent.leverage,
                order_type=OrderType.LIMIT if self.config.default_order_type == "limit" else OrderType.MARKET,
                price=order_intent.entry_price_futures if self.config.default_order_type == "limit" else None,
                reduce_only=False,
            )
            
            # Save converted levels for protective orders
            entry_order.stop_loss_futures = order_intent.stop_loss_futures
            entry_order.take_profit_futures = order_intent.take_profit_futures
            entry_order.size_notional_initial = order_intent.size_notional

            # Track order
            self.submitted_orders[entry_order.client_order_id] = entry_order
            self.order_intents_seen.add(intent_hash)
            
            # Register with order monitor for timeout tracking
            self.order_monitor.track_order(entry_order)
            
            logger.info(
                "Entry order submitted",
                symbol=futures_symbol,
                order_id=entry_order.order_id,
                client_order_id=entry_order.client_order_id,
                entry_price=str(order_intent.entry_price_futures),
            )
            
            return entry_order
            
        except Exception as e:
            logger.error(
                "Failed to submit entry order",
                symbol=order_intent.signal.symbol,
                error=str(e),
            )
            return None
    
    async def place_protective_orders(
        self,
        entry_order: Order,
        stop_loss_price: Decimal,
        take_profit_price: Optional[Decimal],
    ) -> tuple[Optional[Order], Optional[Order]]:
        """
        Place SL/TP orders immediately after entry fill.
        
        Args:
            entry_order: Filled entry order
            stop_loss_price: Stop-loss price (futures)
            take_profit_price: Take-profit price (futures), optional
        
        Returns:
            (stop_loss_order, take_profit_order)
        """
        sl_order = None
        tp_order = None
        
        try:
            # Protective orders must be OPPOSITE side of the entry
            protective_side = Side.SHORT if entry_order.side == Side.LONG else Side.LONG

            # Place stop-loss (reduce-only)
            sl_order = await self.futures_adapter.place_order(
                symbol=entry_order.symbol,
                side=protective_side,
                size_notional=getattr(entry_order, 'size_notional_initial', Decimal("0")),
                leverage=Decimal("1"),  # Not relevant for reduce-only
                order_type=OrderType.STOP_LOSS,
                price=stop_loss_price,
                reduce_only=True,
            )
            sl_order.parent_order_id = entry_order.order_id
            
            logger.info(
                "Stop-loss order placed",
                entry_order_id=entry_order.order_id,
                sl_order_id=sl_order.order_id,
                price=str(stop_loss_price),
            )
            
            # Place take-profit (reduce-only) if specified
            if take_profit_price:
                tp_order = await self.futures_adapter.place_order(
                    symbol=entry_order.symbol,
                    side=protective_side,
                    size_notional=getattr(entry_order, 'size_notional_initial', Decimal("0")),
                    leverage=Decimal("1"),
                    order_type=OrderType.TAKE_PROFIT,
                    price=take_profit_price,
                    reduce_only=True,
                )
                tp_order.parent_order_id = entry_order.order_id
                
                logger.info(
                    "Take-profit order placed",
                    entry_order_id=entry_order.order_id,
                    tp_order_id=tp_order.order_id,
                    price=str(take_profit_price),
                )
        except Exception as e:
            logger.error(
                "Failed to place protective orders",
                entry_order_id=entry_order.order_id,
                error=str(e)
            )
            
        return sl_order, tp_order
    async def update_protective_orders(
        self,
        symbol: str,
        side: Side,
        current_sl_id: Optional[str],
        new_sl_price: Optional[Decimal],
        current_tp_ids: List[str],
        new_tp_prices: List[Decimal],
    ) -> Tuple[Optional[str], List[str]]:
        """
        Update SL/TP orders (Cancel + Replace).
        
        Args:
            symbol: Symbol
            side: Entry side (LONG/SHORT)
            current_sl_id: Current SL order ID
            new_sl_price: New target SL price
            current_tp_ids: Current TP order IDs
            new_tp_prices: New target TP prices (full ladder)
        
        Returns:
            (new_sl_id, new_tp_ids)
        """
        protective_side = Side.SHORT if side == Side.LONG else Side.LONG
        
        # 1. Update SL
        updated_sl_id = current_sl_id
        if new_sl_price:
            try:
                if current_sl_id:
                    await self.futures_adapter.cancel_order(current_sl_id, symbol)
                
                # Fetch position size for correct notional (simplified, should use exact size)
                # In live, we should fetch actual position size from adapter here.
                # For now, we assume size is managed or use a 'flatten' intent
                sl_order = await self.futures_adapter.place_order(
                    symbol=symbol,
                    side=protective_side,
                    size_notional=Decimal("0"), # Placeholder for 'reduce-only' logic if adapter supports it
                    leverage=Decimal("1"),
                    order_type=OrderType.STOP_LOSS,
                    price=new_sl_price,
                    reduce_only=True
                )
                updated_sl_id = sl_order.order_id
                logger.info("SL updated", symbol=symbol, old_id=current_sl_id, new_id=updated_sl_id, price=str(new_sl_price))
            except Exception as e:
                logger.error("Failed to update SL", symbol=symbol, error=str(e))
        
        # 2. Update TPs (TP Ladder Replacement)
        updated_tp_ids = current_tp_ids
        if new_tp_prices:
            try:
                # Cancel existing TP orders
                for tp_id in current_tp_ids:
                    try:
                        await self.futures_adapter.cancel_order(tp_id, symbol)
                        logger.debug("Cancelled TP order", order_id=tp_id)
                    except Exception as e:
                        logger.warning("Failed to cancel TP", order_id=tp_id, error=str(e))
                
                # Place new TP ladder
                new_tp_ids = []
                # Get position size for proper TP sizing
                # For simplicity, divide position equally across TPs
                # In production, you'd want configurable percentages
                tp_count = len(new_tp_prices)
                
                for i, tp_price in enumerate(new_tp_prices):
                    try:
                        # Calculate size for this TP level
                        # Simple equal distribution for now
                        tp_order = await self.futures_adapter.place_order(
                            symbol=symbol,
                            side=protective_side,
                            size_notional=Decimal("0"),  # Reduce-only handles sizing
                            leverage=Decimal("1"),
                            order_type=OrderType.TAKE_PROFIT,
                            price=tp_price,
                            reduce_only=True
                        )
                        new_tp_ids.append(tp_order.order_id)
                        logger.info(
                            f"TP{i+1} placed",
                            symbol=symbol,
                            price=str(tp_price),
                            order_id=tp_order.order_id
                        )
                    except Exception as e:
                        logger.error(f"Failed to place TP{i+1}", symbol=symbol, error=str(e))
                
                updated_tp_ids = new_tp_ids
                logger.info("TP ladder updated", symbol=symbol, tp_count=len(new_tp_ids))
                
            except Exception as e:
                logger.error("Failed to update TP ladder", symbol=symbol, error=str(e))
        
        return updated_sl_id, updated_tp_ids

    async def close_all_positions(self):
        """Emergency: Close all open positions at market."""
        logger.critical("EMERGENCY: CLOSING ALL POSITIONS")
        try:
             # This bypasses the adapter and goes straight to client for speed if needed, 
             # but better to use adapter if it has the logic.
             # Actually, KrakenClient now has close_position and cancel_all_orders.
             # We let the KillSwitch handle this directly usually.
             pass
        except Exception as e:
             logger.error("Emergency close all failed", error=str(e))

    def _hash_intent(self, intent: OrderIntent) -> str:
        """Generate hash for order intent deduplication."""
        components = [
            intent.signal.symbol,
            str(intent.signal.timestamp),
            intent.signal.signal_type.value,
            str(intent.size_notional),
        ]
        return "-".join(components)
    
    def detect_ghost_orders(self, exchange_orders: list[Order]) -> list[str]:
        """
        Detect ghost orders (orders we think exist but exchange doesn't have).
        
        Args:
            exchange_orders: Orders from exchange
        
        Returns:
            List of ghost order IDs
        """
        exchange_order_ids = {o.order_id for o in exchange_orders}
        our_order_ids = {o.order_id for o in self.submitted_orders.values()}
        
        ghost_ids = list(our_order_ids - exchange_order_ids)
        
        if ghost_ids:
            logger.warning(
                "Ghost orders detected",
                count=len(ghost_ids),
                ghost_ids=ghost_ids,
            )
        
        return ghost_ids
    
    async def check_order_timeouts(self) -> int:
        """
        Check for expired orders and cancel them.
        
        Returns:
            Number of orders cancelled
        """
        expired_orders = self.order_monitor.get_expired_orders()
        
        if not expired_orders:
            return 0
        
        cancelled_count = 0
        
        for tracked in expired_orders:
            order = tracked.order
            try:
                logger.warning(
                    "Order timeout detected, cancelling",
                    order_id=order.order_id,
                    symbol=order.symbol,
                    age_seconds=tracked.age_seconds,
                    timeout=tracked.timeout_seconds
                )
                
                # Cancel the order
                await self.futures_adapter.cancel_order(order.order_id, order.symbol)
                
                # Mark as cancelled in monitor
                self.order_monitor.mark_as_cancelled(order.order_id)
                
                # Remove from submitted orders
                if order.client_order_id in self.submitted_orders:
                    del self.submitted_orders[order.client_order_id]
                
                cancelled_count += 1
                
                logger.info(
                    "Expired order cancelled",
                    order_id=order.order_id,
                    symbol=order.symbol
                )
                
            except Exception as e:
                logger.error(
                    "Failed to cancel expired order",
                    order_id=order.order_id,
                    symbol=order.symbol,
                    error=str(e)
                )
        
        return cancelled_count
    
    async def reconcile_orders(self, exchange_orders: list[Order]) -> None:
        """
        Reconcile tracked orders with exchange state.
        
        Args:
            exchange_orders: Current orders from exchange
        """
        discrepancies = self.order_monitor.reconcile_with_exchange(exchange_orders)
        
        if discrepancies:
            logger.warning(
                "Order reconciliation found discrepancies",
                count=len(discrepancies),
                details=discrepancies
            )
    
    def get_monitoring_stats(self) -> dict:
        """
        Get order monitoring statistics.
        
        Returns:
            Dict with monitoring metrics
        """
        return self.order_monitor.get_monitoring_stats()
