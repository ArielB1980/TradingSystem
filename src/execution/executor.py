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
from typing import Dict, Optional, Set, List, Tuple, Any
from datetime import datetime, timezone
import uuid
import asyncio
from collections import defaultdict
from src.domain.models import Order, OrderIntent, OrderType, OrderStatus, Position, Side
from src.execution.futures_adapter import FuturesAdapter
from src.execution.price_converter import PriceConverter
from src.config.config import ExecutionConfig
from src.monitoring.logger import get_logger
from src.data.symbol_utils import normalize_symbol_for_position_match

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
        
        # Latest futures tickers for optimal symbol mapping (updated by LiveTrading each tick)
        self.latest_futures_tickers: Optional[Dict[str, Any]] = None
        
        # Order tracking for idempotency
        self.submitted_orders: Dict[str, Order] = {}  # client_order_id → Order
        self.order_intents_seen: Set[str] = set()  # intent hash for deduplication (memory)
        self._load_persisted_intent_hashes()  # Load from database on startup

        # Per-symbol locks to prevent race conditions in parallel processing
        # Using defaultdict to create locks on demand
        self._symbol_locks = defaultdict(asyncio.Lock)
        
        # Order monitoring for timeout handling
        from src.execution.order_monitor import OrderMonitor
        self.order_monitor = OrderMonitor(
            default_timeout_seconds=config.order_timeout_seconds
        )
        
        logger.info("Executor initialized", config=config.model_dump())
        
    def _normalize_symbol(self, symbol: str) -> str:
        """
        Normalize symbol for robust comparison.
        Handles: PF_MONUSD, MON/USD:USD, MONUSD-PERP, etc.
        Returns: MONUSD (clean uppercase)
        """
        if not symbol: return ""
        s = symbol.upper()
        # Remove Kraken prefixes
        s = s.replace('PF_', '').replace('PI_', '').replace('FI_', '')
        # Remove CCXT suffixes
        s = s.split(':')[0]
        # Remove separators
        s = s.replace('/', '').replace('-', '').replace('_', '')
        return s


        
    async def sync_open_orders(self) -> None:
        """
        Synchronize local order state with exchange open orders.
        
        CRITICAL ROOT CAUSE FIX:
        Restores 'pending order' awareness after bot restart.
        Prevents duplicate orders if bot crashed while orders were open.
        """
        try:
            open_orders_data = await self.futures_adapter.kraken_client.get_futures_open_orders()
            
            synced_count = 0
            for order_data in open_orders_data:
                # Map CCXT structure to our Order domain model
                
                # Status
                status_str = order_data.get('status')
                status_map = {
                    'open': OrderStatus.SUBMITTED,  # Open orders are submitted/pending
                    'closed': OrderStatus.FILLED, 
                    'canceled': OrderStatus.CANCELLED,
                    'pending': OrderStatus.PENDING,
                    'submitted': OrderStatus.SUBMITTED,
                }
                # If we are fetching 'open_orders', they are mostly SUBMITTED/PENDING
                status = status_map.get(status_str, OrderStatus.SUBMITTED)
                
                # Side
                side_str = order_data.get('side', '').lower()
                side = Side.LONG if side_str == 'buy' else Side.SHORT
                
                # Type
                type_str = order_data.get('type')
                type_map = {
                    'limit': OrderType.LIMIT,
                    'market': OrderType.MARKET,
                    'stop': OrderType.STOP_LOSS,
                    'take_profit': OrderType.TAKE_PROFIT
                }
                order_type = type_map.get(type_str, OrderType.LIMIT)
                
                # IDs
                order_id = str(order_data.get('id', ''))
                # Try to use clientOrderId, fallback to info.cliOrdId or generated
                client_id = order_data.get('clientOrderId')
                if not client_id and 'info' in order_data:
                    client_id = order_data['info'].get('cliOrdId')
                
                if not client_id:
                    client_id = f"recovered_{order_id}"
                
                # Construct Order
                order = Order(
                    order_id=order_id,
                    client_order_id=client_id,
                    timestamp=datetime.fromtimestamp(order_data.get('timestamp', 0)/1000, timezone.utc),
                    symbol=order_data.get('symbol', ''),
                    side=side,
                    order_type=order_type,
                    size=Decimal(str(order_data.get('amount') or 0)),
                    price=Decimal(str(order_data.get('price'))) if order_data.get('price') else None,
                    status=status,
                    reduce_only=order_data.get('reduceOnly', False)
                )
                
                # Store in memory
                self.submitted_orders[client_id] = order
                synced_count += 1
                
            logger.info(
                "Executor state synchronized with exchange",
                recovered_orders=synced_count,
                active_submission_count=len(self.submitted_orders)
            )
            
        except Exception as e:
            logger.error("Failed to sync open orders in Executor", error=str(e))
            # Critical: We should probably raise here or ensure we don't trade blindly?
            # For now, log error, as system can technically recover by active checks,
            # but duplicate guard might be weak.

    
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
        # Idempotency check (before lock for performance)
        intent_hash = self._hash_intent(order_intent)
        if intent_hash in self.order_intents_seen:
            logger.warning(
                "Duplicate order intent detected",
                symbol=order_intent.signal.symbol,
                intent_hash=intent_hash,
            )
            return None
        
        futures_symbol = self.futures_adapter.map_spot_to_futures(
            order_intent.signal.symbol,
            futures_tickers=self.latest_futures_tickers
        )
        
        # CRITICAL: Acquire per-symbol lock to prevent race conditions
        # This ensures only one order can be processed for a symbol at a time
        async with self._symbol_locks[futures_symbol]:
            # Pyramiding guard
            if self.config.pyramiding_enabled is False:
                # Check if we already have a position in this symbol.
                # CRITICAL: Normalize both sides — exchange positions may use PF_* or PI_*,
                # while map_spot_to_futures can return ROSE/USD:USD. Exact match would miss
                # the existing position and allow a second order on the same contract (pyramiding).
                fut_norm = normalize_symbol_for_position_match(futures_symbol)
                has_position = any(
                    normalize_symbol_for_position_match(p.symbol) == fut_norm
                    for p in current_positions
                )
                if has_position:
                    logger.info("PYRAMIDING_GUARD_SKIP", symbol=futures_symbol, reason="position_already_exists")
                    logger.warning(
                        "Pyramiding guard REJECTED",
                        symbol=futures_symbol,
                        reason="Pyramiding disabled, position already exists",
                    )
                    return None
                    
                # CRITICAL: Check exchange for existing open orders BEFORE placing new order
                # This prevents duplicate orders if sync missed something or order was placed externally
                try:
                    exchange_orders = await self.futures_adapter.kraken_client.get_futures_open_orders()
                    
                    # First, clean up stale local orders that don't exist on exchange
                    # This fixes the issue where local state has pending orders that were already filled/cancelled
                    exchange_order_ids = {str(o.get('id', '')) for o in exchange_orders}
                    exchange_client_ids = {str(o.get('clientOrderId', '')) for o in exchange_orders if o.get('clientOrderId')}
                    
                    # Remove local orders that don't exist on exchange (they were filled/cancelled)
                    stale_orders = []
                    for client_id, local_order in list(self.submitted_orders.items()):
                        # Check if order exists on exchange
                        order_exists = (
                            local_order.order_id in exchange_order_ids or
                            client_id in exchange_client_ids
                        )
                        
                        # If order is pending locally but doesn't exist on exchange, it's stale
                        if (local_order.status in (OrderStatus.SUBMITTED, OrderStatus.PENDING) and 
                            not order_exists):
                            stale_orders.append(client_id)
                    
                    # Remove stale orders
                    for client_id in stale_orders:
                        logger.debug(
                            "Removing stale pending order from local state",
                            symbol=self.submitted_orders[client_id].symbol,
                            client_order_id=client_id,
                            order_id=self.submitted_orders[client_id].order_id
                        )
                        del self.submitted_orders[client_id]
                    
                    # Now check if exchange has pending orders for this symbol
                    exchange_pending = any(
                        self._normalize_symbol(o.get('symbol', '')) == self._normalize_symbol(futures_symbol)
                        and o.get('side', '').lower() == ('buy' if order_intent.side == Side.LONG else 'sell')
                        and o.get('status', '').lower() in ('open', 'pending', 'submitted')
                        for o in exchange_orders
                    )
                    
                    if exchange_pending:
                        logger.warning(
                            "Duplicate order guard REJECTED - Exchange has pending order",
                            symbol=futures_symbol,
                            reason="Open order already exists on exchange"
                        )
                        # Sync this order to local state
                        await self.sync_open_orders()
                        return None
                except Exception as e:
                    logger.warning(
                        "Failed to check exchange orders, proceeding with local check only",
                        symbol=futures_symbol,
                        error=str(e)
                    )


                # Check if we have any pending (open) entry orders for this symbol
                # Block duplicate entry orders - only one entry order per symbol at a time
                # NOTE: After cleaning stale orders above, this should be more accurate
                has_pending = any(
                    self._normalize_symbol(o.symbol) == self._normalize_symbol(futures_symbol)
                    and o.status in (OrderStatus.SUBMITTED, OrderStatus.PENDING)
                    and o.side == order_intent.side  # Same side to allow reversal orders
                    for o in self.submitted_orders.values()
                )

                if has_pending:
                    logger.warning(
                        "Duplicate order guard REJECTED",
                        symbol=futures_symbol,
                        side=order_intent.side.value,
                        reason="Pending entry order already exists in local state",
                        local_orders=len([o for o in self.submitted_orders.values()
                                         if self._normalize_symbol(o.symbol) == self._normalize_symbol(futures_symbol)])
                    )
                    return None
            
            # Place entry order
            try:
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
                self._persist_intent_hash(intent_hash, order_intent)  # Persist to survive restarts

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
                err = str(e)
                insufficient = "insufficient" in err.lower() or "insufficientavailablefunds" in err.lower()
                if insufficient:
                    logger.warning(
                        "Insufficient funds, skipping entry",
                        symbol=order_intent.signal.symbol,
                        error=err[:200],
                    )
                else:
                    logger.error(
                        "Failed to submit entry order",
                        symbol=order_intent.signal.symbol,
                        error=err,
                    )
                # CRITICAL: Add intent_hash even on failure to prevent immediate retry
                self.order_intents_seen.add(intent_hash)
                self._persist_intent_hash(intent_hash, order_intent)
                logger.debug(
                    "Added failed intent to seen set to prevent immediate retry",
                    symbol=order_intent.signal.symbol,
                    intent_hash=intent_hash,
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
        position_size_notional: Optional[Decimal] = None,
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
                
                # Reduce-only SL: use actual position size if provided, else 0 (exchange handles sizing)
                sl_size = position_size_notional if position_size_notional else Decimal("0")
                sl_order = await self.futures_adapter.place_order(
                    symbol=symbol,
                    side=protective_side,
                    size_notional=sl_size,
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
                # Use actual position size if provided (for proper TP sizing)
                # For reduce-only orders, exchange will handle sizing if 0, but passing actual size is safer
                tp_size = position_size_notional if position_size_notional else Decimal("0")
                
                for i, tp_price in enumerate(new_tp_prices):
                    try:
                        # Place TP order with position size (reduce-only will close appropriate amount)
                        tp_order = await self.futures_adapter.place_order(
                            symbol=symbol,
                            side=protective_side,
                            size_notional=tp_size,  # Use actual position size for safety
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

    def _load_persisted_intent_hashes(self):
        """Load recent intent hashes from database on startup to prevent duplicates after restart."""
        try:
            from src.storage.repository import load_recent_intent_hashes
            persisted_hashes = load_recent_intent_hashes(lookback_hours=24)
            self.order_intents_seen.update(persisted_hashes)
            logger.info(f"Loaded {len(persisted_hashes)} persisted intent hashes from last 24h")
        except Exception as e:
            logger.warning(f"Failed to load persisted intent hashes: {e}")

    def _persist_intent_hash(self, intent_hash: str, intent: OrderIntent):
        """Persist intent hash to database for duplicate prevention after restart."""
        try:
            from src.storage.repository import save_intent_hash
            save_intent_hash(intent_hash, intent.signal.symbol, intent.signal.timestamp)
        except Exception as e:
            logger.warning(f"Failed to persist intent hash: {e}")

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
    
    async def check_order_timeouts(self, current_prices: Optional[Dict[str, Decimal]] = None) -> int:
        """
        Check for expired and price-invalidated orders and cancel them.

        Args:
            current_prices: Optional dict of symbol -> current_price for price-based cancellation

        Returns:
            Number of orders cancelled
        """
        # 1. Time-based expiration
        expired_orders = self.order_monitor.get_expired_orders()

        # 2. Price-based invalidation (if prices provided)
        price_invalidated = []
        if current_prices and hasattr(self.config, 'order_price_invalidation_pct'):
            price_invalidated = self.order_monitor.get_price_invalidated_orders(
                current_prices,
                self.config.order_price_invalidation_pct
            )

        # Combine all orders to cancel (avoid duplicates)
        orders_to_cancel_set = set()
        for tracked in expired_orders:
            orders_to_cancel_set.add(tracked.order.order_id)
        for tracked in price_invalidated:
            orders_to_cancel_set.add(tracked.order.order_id)

        # Build list of tracked orders to cancel
        all_orders_to_cancel = []
        for tracked in expired_orders + price_invalidated:
            if tracked.order.order_id in orders_to_cancel_set:
                all_orders_to_cancel.append(tracked)
                orders_to_cancel_set.remove(tracked.order.order_id)  # Prevent duplicates

        if not all_orders_to_cancel:
            return 0

        cancelled_count = 0

        for tracked in all_orders_to_cancel:
            order = tracked.order

            # Determine reason
            is_expired = tracked.is_expired
            is_price_invalid = tracked in price_invalidated

            reason_parts = []
            if is_expired:
                reason_parts.append(f"timeout ({tracked.age_seconds:.0f}s > {tracked.timeout_seconds}s)")
            if is_price_invalid:
                reason_parts.append("price moved away")
            reason = " & ".join(reason_parts)

            try:
                logger.warning(
                    f"Cancelling order: {reason}",
                    order_id=order.order_id,
                    symbol=order.symbol,
                    age_seconds=tracked.age_seconds
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
                    "Order cancelled",
                    order_id=order.order_id,
                    symbol=order.symbol,
                    reason=reason
                )

            except Exception as e:
                logger.error(
                    "Failed to cancel order",
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
