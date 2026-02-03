"""
Execution Gateway - Single Order Flow Point.

CRITICAL: All order placement MUST flow through this gateway.

This ensures:
1. Every order has client_order_id linking to position_id
2. Order events are routed back to state machine
3. No bypass paths for order placement
4. Audit trail for all execution
"""
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Optional, Dict, List, Callable, Awaitable
from datetime import datetime, timezone
import asyncio

from src.execution.position_state_machine import (
    ManagedPosition,
    PositionRegistry,
    PositionState,
    OrderEvent,
    OrderEventType,
    ExitReason,
    get_position_registry
)
from src.execution.position_manager_v2 import (
    PositionManagerV2,
    ManagementAction,
    ActionType
)
from src.execution.position_persistence import PositionPersistence
from src.execution.production_safety import (
    SafetyConfig,
    AtomicStopReplacer,
    WriteAheadIntentLog,
    EventOrderingEnforcer,
    ActionIntent,
    ActionIntentStatus,
)
from src.domain.models import Side, OrderType
from src.monitoring.logger import get_logger

logger = get_logger(__name__)


class OrderPurpose(str, Enum):
    """Purpose of an order for tracking."""
    ENTRY = "entry"
    EXIT_STOP = "exit_stop"
    EXIT_TP = "exit_tp"
    EXIT_MARKET = "exit_market"
    EXIT_REVERSAL = "exit_reversal"
    STOP_INITIAL = "stop_initial"
    STOP_UPDATE = "stop_update"


@dataclass
class PendingOrder:
    """Tracking record for a pending order."""
    client_order_id: str
    position_id: str
    symbol: str
    purpose: OrderPurpose
    side: Side
    size: Decimal
    price: Optional[Decimal]
    order_type: OrderType
    submitted_at: datetime
    exchange_order_id: Optional[str] = None
    status: str = "pending"
    last_event_seq: int = 0
    exchange_symbol: Optional[str] = None  # Futures symbol for fetch_order (e.g. X/USD:USD)
    

@dataclass
class ExecutionResult:
    """Result of an execution attempt."""
    success: bool
    client_order_id: str
    exchange_order_id: Optional[str] = None
    error: Optional[str] = None
    filled_qty: Decimal = Decimal("0")
    filled_price: Optional[Decimal] = None


class ExecutionGateway:
    """
    Single point of order flow.
    
    ALL order placement must go through this gateway.
    This is enforced by architecture - no direct exchange calls allowed.
    
    Responsibilities:
    1. Submit orders to exchange
    2. Track pending orders
    3. Route order events to state machine
    4. Persist state changes
    5. Emit follow-up actions
    """
    
    def __init__(
        self,
        exchange_client,  # The actual exchange client (KrakenFuturesClient)
        registry: Optional[PositionRegistry] = None,
        position_manager: Optional[PositionManagerV2] = None,
        persistence: Optional[PositionPersistence] = None,
        safety_config: Optional[SafetyConfig] = None,
        use_safety: bool = True,
    ):
        """
        Initialize the execution gateway.
        
        Args:
            exchange_client: Exchange API client
            registry: Position registry (uses singleton if not provided)
            position_manager: Position manager (creates new if not provided)
            persistence: Persistence layer (optional, creates default if not provided)
            safety_config: Config for AtomicStopReplacer, etc. (default SafetyConfig())
            use_safety: If True, wire AtomicStopReplacer, WAL, EventOrderingEnforcer
        """
        self.client = exchange_client
        self.registry = registry or get_position_registry()
        self.position_manager = position_manager or PositionManagerV2(self.registry)
        self.persistence = persistence or PositionPersistence()
        self._safety_config = safety_config or SafetyConfig()
        self._use_safety = use_safety

        self._stop_replacer: Optional[AtomicStopReplacer] = None
        self._wal: Optional[WriteAheadIntentLog] = None
        self._event_enforcer: Optional[EventOrderingEnforcer] = None
        if use_safety:
            self._stop_replacer = AtomicStopReplacer(exchange_client, self._safety_config)
            self._wal = WriteAheadIntentLog(self.persistence)
            self._event_enforcer = EventOrderingEnforcer()
        
        # Pending order tracking
        self._pending_orders: Dict[str, PendingOrder] = {}  # client_order_id -> PendingOrder
        self._order_id_map: Dict[str, str] = {}  # exchange_order_id -> client_order_id
        
        # Metrics
        self.metrics = {
            "orders_submitted": 0,
            "orders_filled": 0,
            "orders_cancelled": 0,
            "orders_rejected": 0,
            "events_processed": 0,
            "errors": 0
        }

    def _wal_record_intent(
        self,
        action: ManagementAction,
        action_type: str,
        size: Optional[Decimal] = None,
        price: Optional[Decimal] = None,
    ) -> None:
        """Record write-ahead intent before exchange call. No-op if no WAL."""
        if not self._wal:
            return
        intent = ActionIntent(
            intent_id=action.client_order_id,
            position_id=action.position_id or "",
            action_type=action_type,
            symbol=action.symbol,
            side=action.side.value,
            size=str(size if size is not None else action.size),
            price=str(price) if price is not None else (str(action.price) if action.price else None),
            created_at=datetime.now(timezone.utc),
        )
        self._wal.record_intent(intent)

    def _wal_mark_sent(self, intent_id: str, exchange_order_id: str) -> None:
        if self._wal:
            self._wal.mark_sent(intent_id, exchange_order_id)

    def _wal_mark_completed(self, intent_id: str) -> None:
        if self._wal:
            self._wal.mark_completed(intent_id)

    def _wal_mark_failed(self, intent_id: str, error: str) -> None:
        if self._wal:
            self._wal.mark_failed(intent_id, error)
    
    # ========== ORDER SUBMISSION ==========
    
    async def execute_action(
        self, action: ManagementAction, order_symbol: Optional[str] = None
    ) -> ExecutionResult:
        """
        Execute a management action.

        order_symbol: optional futures symbol (e.g. X/USD:USD) for exchange orders.
        When provided for OPEN_POSITION, used instead of action.symbol (spot) so
        Kraken Futures receives the correct unified symbol.
        """
        try:
            if action.type == ActionType.OPEN_POSITION:
                return await self._execute_entry(action, order_symbol=order_symbol)
            
            elif action.type == ActionType.CLOSE_FULL:
                return await self._execute_close(action)
            
            elif action.type == ActionType.CLOSE_PARTIAL:
                return await self._execute_partial_close(action)
            
            elif action.type == ActionType.PLACE_STOP:
                return await self._execute_place_stop(action)
            
            elif action.type == ActionType.UPDATE_STOP:
                return await self._execute_update_stop(action)
            
            elif action.type == ActionType.CANCEL_STOP:
                return await self._execute_cancel_stop(action)
            
            elif action.type == ActionType.FLATTEN_ORPHAN:
                return await self._execute_flatten_orphan(action)
            
            else:
                logger.warning(f"Unhandled action type: {action.type}")
                return ExecutionResult(
                    success=False,
                    client_order_id=action.client_order_id,
                    error=f"Unhandled action type: {action.type}"
                )
                
        except Exception as e:
            self.metrics["errors"] += 1
            logger.error(
                "Execution failed",
                action_type=action.type.value,
                symbol=action.symbol,
                error=str(e)
            )
            return ExecutionResult(
                success=False,
                client_order_id=action.client_order_id,
                error=str(e)
            )
    
    async def _execute_entry(
        self, action: ManagementAction, order_symbol: Optional[str] = None
    ) -> ExecutionResult:
        """Execute entry order. order_symbol: futures symbol for exchange (e.g. X/USD:USD)."""
        self.metrics["orders_submitted"] += 1
        exchange_symbol = order_symbol if order_symbol is not None else action.symbol

        # Track pending order (store exchange_symbol for order-status polling)
        pending = PendingOrder(
            client_order_id=action.client_order_id,
            position_id=action.position_id,
            symbol=action.symbol,
            purpose=OrderPurpose.ENTRY,
            side=action.side,
            size=action.size,
            price=action.price,
            order_type=action.order_type,
            submitted_at=datetime.now(timezone.utc),
            exchange_symbol=exchange_symbol,
        )
        self._pending_orders[action.client_order_id] = pending

        self._wal_record_intent(action, "open")

        # Submit to exchange (use futures symbol; action.symbol is spot)
        try:
            order_side = "buy" if action.side == Side.LONG else "sell"

            result = await self.client.create_order(
                symbol=exchange_symbol,
                type=action.order_type.value,
                side=order_side,
                amount=float(action.size),
                price=float(action.price) if action.price and action.order_type == OrderType.LIMIT else None,
                params={"clientOrderId": action.client_order_id}
            )
            
            exchange_order_id = result.get("id")
            pending.exchange_order_id = exchange_order_id
            pending.status = "submitted"
            self._order_id_map[exchange_order_id] = action.client_order_id
            
            logger.info(
                "Entry order submitted",
                symbol=exchange_symbol,
                client_order_id=action.client_order_id,
                exchange_order_id=exchange_order_id
            )
            self._wal_mark_sent(action.client_order_id, exchange_order_id)
            
            # Log action to persistence
            self.persistence.log_action(
                action.position_id,
                "entry_submitted",
                {"client_order_id": action.client_order_id, "exchange_order_id": exchange_order_id}
            )
            
            return ExecutionResult(
                success=True,
                client_order_id=action.client_order_id,
                exchange_order_id=exchange_order_id
            )
            
        except Exception as e:
            pending.status = "failed"
            self.metrics["orders_rejected"] += 1
            self._wal_mark_failed(action.client_order_id, str(e))
            logger.error(f"Entry order failed: {e}")
            return ExecutionResult(
                success=False,
                client_order_id=action.client_order_id,
                error=str(e)
            )
    
    async def _execute_close(self, action: ManagementAction) -> ExecutionResult:
        """Execute full position close."""
        self.metrics["orders_submitted"] += 1
        
        # Track pending order
        pending = PendingOrder(
            client_order_id=action.client_order_id,
            position_id=action.position_id,
            symbol=action.symbol,
            purpose=OrderPurpose.EXIT_MARKET if action.exit_reason != ExitReason.DIRECTION_REVERSAL else OrderPurpose.EXIT_REVERSAL,
            side=Side.SHORT if action.side == Side.LONG else Side.LONG,  # Opposite side to close
            size=action.size,
            price=None,
            order_type=OrderType.MARKET,
            submitted_at=datetime.now(timezone.utc)
        )
        self._pending_orders[action.client_order_id] = pending
        
        # Update position state to EXIT_PENDING
        position = self.registry.get_position(action.symbol)
        if position:
            position.initiate_exit(action.exit_reason, action.client_order_id)
            self.persistence.save_position(position)
        exchange_symbol = (getattr(position, "futures_symbol", None) if position else None) or action.symbol
        
        self._wal_record_intent(action, "close")
        try:
            # Close via reduce-only market order (reduceOnly=True required: rounds up, no dust)
            close_side = "sell" if action.side == Side.LONG else "buy"
            
            result = await self.client.create_order(
                symbol=exchange_symbol,
                type="market",
                side=close_side,
                amount=float(action.size),
                params={
                    "clientOrderId": action.client_order_id,
                    "reduceOnly": True,
                }
            )
            
            exchange_order_id = result.get("id")
            pending.exchange_order_id = exchange_order_id
            pending.status = "submitted"
            self._order_id_map[exchange_order_id] = action.client_order_id
            self._wal_mark_sent(action.client_order_id, exchange_order_id)
            
            logger.info(
                "Close order submitted",
                symbol=exchange_symbol,
                reason=action.exit_reason.value if action.exit_reason else "unknown"
            )
            
            return ExecutionResult(
                success=True,
                client_order_id=action.client_order_id,
                exchange_order_id=exchange_order_id
            )
            
        except Exception as e:
            pending.status = "failed"
            self._wal_mark_failed(action.client_order_id, str(e))
            logger.error(f"Close order failed: {e}")
            return ExecutionResult(
                success=False,
                client_order_id=action.client_order_id,
                error=str(e)
            )
    
    async def _execute_partial_close(self, action: ManagementAction) -> ExecutionResult:
        """Execute partial position close. Use position.futures_symbol for exchange when set."""
        self.metrics["orders_submitted"] += 1
        position = self.registry.get_position(action.symbol)
        exchange_symbol = (getattr(position, "futures_symbol", None) if position else None) or action.symbol
        
        pending = PendingOrder(
            client_order_id=action.client_order_id,
            position_id=action.position_id,
            symbol=action.symbol,
            purpose=OrderPurpose.EXIT_TP,
            side=Side.SHORT if action.side == Side.LONG else Side.LONG,
            size=action.size,
            price=None,
            order_type=OrderType.MARKET,
            submitted_at=datetime.now(timezone.utc)
        )
        self._pending_orders[action.client_order_id] = pending
        
        self._wal_record_intent(action, "partial_close")
        try:
            # reduceOnly=True required for exits: size rounds up, no dust
            close_side = "sell" if action.side == Side.LONG else "buy"
            
            result = await self.client.create_order(
                symbol=exchange_symbol,
                type="market",
                side=close_side,
                amount=float(action.size),
                params={
                    "clientOrderId": action.client_order_id,
                    "reduceOnly": True,
                }
            )
            
            exchange_order_id = result.get("id")
            pending.exchange_order_id = exchange_order_id
            self._order_id_map[exchange_order_id] = action.client_order_id
            self._wal_mark_sent(action.client_order_id, exchange_order_id)
            
            return ExecutionResult(
                success=True,
                client_order_id=action.client_order_id,
                exchange_order_id=exchange_order_id
            )
            
        except Exception as e:
            self._wal_mark_failed(action.client_order_id, str(e))
            logger.error(f"Partial close failed: {e}")
            return ExecutionResult(
                success=False,
                client_order_id=action.client_order_id,
                error=str(e)
            )
    
    async def _execute_place_stop(self, action: ManagementAction) -> ExecutionResult:
        """Place stop loss order. Use position.futures_symbol for exchange when set."""
        self.metrics["orders_submitted"] += 1
        position = self.registry.get_position(action.symbol)
        exchange_symbol = (getattr(position, "futures_symbol", None) if position else None) or action.symbol
        
        pending = PendingOrder(
            client_order_id=action.client_order_id,
            position_id=action.position_id,
            symbol=action.symbol,
            purpose=OrderPurpose.STOP_INITIAL,
            side=Side.SHORT if action.side == Side.LONG else Side.LONG,  # Opposite to close
            size=action.size,
            price=action.price,
            order_type=OrderType.STOP_LOSS,
            submitted_at=datetime.now(timezone.utc)
        )
        self._pending_orders[action.client_order_id] = pending
        
        self._wal_record_intent(action, "place_stop", price=action.price)
        try:
            # reduceOnly=True required for protective exits: rounds up, no dust
            stop_side = "sell" if action.side == Side.LONG else "buy"
            
            result = await self.client.create_order(
                symbol=exchange_symbol,
                type="stop",
                side=stop_side,
                amount=float(action.size),
                price=float(action.price),
                params={
                    "clientOrderId": action.client_order_id,
                    "reduceOnly": True,
                    "stopPrice": float(action.price),
                }
            )
            
            exchange_order_id = result.get("id")
            pending.exchange_order_id = exchange_order_id
            self._order_id_map[exchange_order_id] = action.client_order_id
            self._wal_mark_sent(action.client_order_id, exchange_order_id)
            
            # Update position with stop order ID
            position = self.registry.get_position(action.symbol)
            if position:
                position.stop_order_id = exchange_order_id
                self.persistence.save_position(position)
            
            logger.info(
                "Stop order placed",
                symbol=exchange_symbol,
                price=str(action.price)
            )
            
            return ExecutionResult(
                success=True,
                client_order_id=action.client_order_id,
                exchange_order_id=exchange_order_id
            )
            
        except Exception as e:
            self._wal_mark_failed(action.client_order_id, str(e))
            logger.error(f"Stop placement failed: {e}")
            return ExecutionResult(
                success=False,
                client_order_id=action.client_order_id,
                error=str(e)
            )
    
    async def _execute_update_stop(self, action: ManagementAction) -> ExecutionResult:
        """Update stop loss order. Uses AtomicStopReplacer (new-first then cancel old) when use_safety."""
        position = self.registry.get_position(action.symbol)
        if not position:
            return ExecutionResult(
                success=False,
                client_order_id=action.client_order_id,
                error="Position not found"
            )
        if not action.price:
            return ExecutionResult(
                success=False,
                client_order_id=action.client_order_id,
                error="Update stop requires price"
            )

        if self._stop_replacer:
            self._wal_record_intent(action, "update_stop", price=action.price)
            def _gen_cid(_pid: str, _: str) -> str:
                return action.client_order_id
            ctx = await self._stop_replacer.replace_stop(
                position, action.price, _gen_cid
            )
            if ctx.failed:
                self.metrics["errors"] += 1
                self._wal_mark_failed(action.client_order_id, ctx.error or "Atomic stop replace failed")
                return ExecutionResult(
                    success=False,
                    client_order_id=action.client_order_id,
                    error=ctx.error or "Atomic stop replace failed"
                )
            if not position.update_stop(action.price):
                return ExecutionResult(
                    success=False,
                    client_order_id=action.client_order_id,
                    error="Stop update rejected by state machine"
                )
            if ctx.new_stop_order_id:
                position.stop_order_id = ctx.new_stop_order_id
            self.persistence.save_position(position)
            self.metrics["orders_submitted"] += 1
            if ctx.old_stop_cancelled:
                self.metrics["orders_cancelled"] += 1
            self._wal_mark_sent(action.client_order_id, ctx.new_stop_order_id or "")
            return ExecutionResult(
                success=True,
                client_order_id=action.client_order_id,
                exchange_order_id=ctx.new_stop_order_id
            )
        else:
            # Legacy: cancel then place when use_safety=False
            if position.stop_order_id:
                try:
                    await self.client.cancel_order(position.stop_order_id, action.symbol)
                    self.metrics["orders_cancelled"] += 1
                except Exception as e:
                    logger.warning(f"Failed to cancel old stop: {e}")
            if not position.update_stop(action.price):
                return ExecutionResult(
                    success=False,
                    client_order_id=action.client_order_id,
                    error="Stop update rejected by state machine"
                )
            action.size = position.remaining_qty
            return await self._execute_place_stop(action)
    
    async def _execute_cancel_stop(self, action: ManagementAction) -> ExecutionResult:
        """Cancel stop loss order."""
        position = self.registry.get_position(action.symbol)
        if not position or not position.stop_order_id:
            return ExecutionResult(
                success=True,
                client_order_id=action.client_order_id,
                error="No stop to cancel"
            )
        self._wal_record_intent(action, "cancel_stop", size=position.remaining_qty)
        try:
            await self.client.cancel_order(position.stop_order_id, action.symbol)
            self.metrics["orders_cancelled"] += 1
            self._wal_mark_completed(action.client_order_id)
            return ExecutionResult(
                success=True,
                client_order_id=action.client_order_id
            )
        except Exception as e:
            self._wal_mark_failed(action.client_order_id, str(e))
            return ExecutionResult(
                success=False,
                client_order_id=action.client_order_id,
                error=str(e)
            )
    
    async def _execute_flatten_orphan(self, action: ManagementAction) -> ExecutionResult:
        """Flatten orphan position on exchange."""
        logger.critical(f"FLATTENING ORPHAN POSITION: {action.symbol}")
        self._wal_record_intent(action, "flatten_orphan")
        try:
            # Use exchange's close_position command
            result = await self.client.close_position(action.symbol)
            oid = (result or {}).get("id") if isinstance(result, dict) else None
            if oid:
                self._wal_mark_sent(action.client_order_id, str(oid))
            else:
                self._wal_mark_completed(action.client_order_id)
            self.persistence.log_action(
                action.position_id or "unknown",
                "orphan_flattened",
                {"symbol": action.symbol, "result": str(result)}
            )
            return ExecutionResult(
                success=True,
                client_order_id=action.client_order_id
            )
        except Exception as e:
            self._wal_mark_failed(action.client_order_id, str(e))
            logger.error(f"Failed to flatten orphan: {e}")
            return ExecutionResult(
                success=False,
                client_order_id=action.client_order_id,
                error=str(e)
            )
    
    # ========== ORDER EVENT HANDLING ==========
    
    async def process_order_update(self, order_data: Dict) -> List[ManagementAction]:
        """
        Process order update from exchange.
        
        This routes events to the state machine and returns follow-up actions.
        """
        self.metrics["events_processed"] += 1
        
        exchange_order_id = order_data.get("id")
        client_order_id = order_data.get("clientOrderId") or self._order_id_map.get(exchange_order_id)
        
        if not client_order_id:
            logger.warning(f"Unknown order update: {exchange_order_id}")
            return []
        
        pending = self._pending_orders.get(client_order_id)
        if not pending:
            logger.warning(f"No pending order for client_order_id: {client_order_id}")
            return []
        
        # Determine event type
        status = order_data.get("status", "").lower()
        filled = Decimal(str(order_data.get("filled", 0)))
        remaining = Decimal(str(order_data.get("remaining", 0)))
        
        if status == "closed" and filled > 0:
            event_type = OrderEventType.FILLED
        elif filled > 0 and remaining > 0:
            event_type = OrderEventType.PARTIAL_FILL
        elif status == "canceled":
            event_type = OrderEventType.CANCELLED
        elif status == "rejected":
            event_type = OrderEventType.REJECTED
        elif status == "open":
            event_type = OrderEventType.ACKNOWLEDGED
        else:
            return []  # No meaningful event
        
        next_seq = pending.last_event_seq + 1
        trades = order_data.get("trades") or []
        raw_fill_id = trades[0].get("id") if trades and isinstance(trades[0], dict) else None
        fill_id = str(raw_fill_id) if raw_fill_id is not None else None

        if self._event_enforcer and not self._event_enforcer.should_process_event(
            exchange_order_id, next_seq, fill_id
        ):
            return []

        event = OrderEvent(
            order_id=exchange_order_id,
            client_order_id=client_order_id,
            event_type=event_type,
            event_seq=next_seq,
            timestamp=datetime.now(timezone.utc),
            fill_qty=filled if event_type in (OrderEventType.FILLED, OrderEventType.PARTIAL_FILL) else None,
            fill_price=Decimal(str(order_data.get("average", 0))) if filled > 0 else None,
            fill_id=fill_id,
        )
        
        follow_up = self.position_manager.handle_order_event(pending.symbol, event)
        
        if self._event_enforcer:
            self._event_enforcer.mark_processed(exchange_order_id, next_seq, fill_id)
        pending.last_event_seq = next_seq
        
        if event_type == OrderEventType.FILLED:
            pending.status = "filled"
            self.metrics["orders_filled"] += 1
            self._wal_mark_completed(client_order_id)
        elif event_type == OrderEventType.CANCELLED:
            pending.status = "cancelled"
            self.metrics["orders_cancelled"] += 1
        elif event_type == OrderEventType.REJECTED:
            pending.status = "rejected"
            self.metrics["orders_rejected"] += 1
        
        position = self.registry.get_position(pending.symbol)
        if position:
            self.persistence.save_position(position)
        
        for action in follow_up:
            await self.execute_action(action)
        
        return follow_up

    async def poll_and_process_order_updates(self) -> int:
        """
        Poll order status for pending entry orders, process fills, and trigger PLACE_STOP.
        Returns number of orders processed (fill/cancel/reject).
        """
        fetch_order = getattr(self.client, "fetch_order", None)
        if not fetch_order:
            return 0
        processed = 0
        for pending in list(self._pending_orders.values()):
            if pending.purpose != OrderPurpose.ENTRY:
                continue
            if pending.status not in ("pending", "submitted"):
                continue
            oid = pending.exchange_order_id
            if not oid:
                continue
            sym = pending.exchange_symbol or pending.symbol
            try:
                order_data = await fetch_order(oid, sym)
            except Exception as e:
                logger.debug("poll order fetch failed", order_id=oid, symbol=sym, error=str(e))
                continue
            if not order_data:
                continue
            if not order_data.get("clientOrderId"):
                order_data["clientOrderId"] = pending.client_order_id
            try:
                follow_up = await self.process_order_update(order_data)
                if follow_up:
                    processed += 1
            except Exception as e:
                logger.warning("process_order_update failed", order_id=oid, error=str(e))
        return processed
    
    # ========== BATCH OPERATIONS ==========
    
    async def execute_actions(self, actions: List[ManagementAction]) -> List[ExecutionResult]:
        """Execute multiple actions in priority order."""
        # Sort by priority
        actions.sort(key=lambda a: a.priority, reverse=True)
        
        results = []
        for action in actions:
            result = await self.execute_action(action)
            results.append(result)
        
        return results
    
    async def sync_with_exchange(self) -> Dict:
        """
        Sync state with exchange.
        
        Returns dict of issues found.
        """
        # Get exchange state
        positions = await self.client.get_all_futures_positions()
        orders = await self.client.get_futures_open_orders()
        
        # Convert to reconciliation format
        exchange_positions = {
            p['symbol']: {
                'side': p.get('side', 'unknown'),
                'qty': str(p.get('contracts', p.get('size', 0))),
                'entry_price': str(p.get('entryPrice', 0))
            }
            for p in positions
            if float(p.get('contracts', p.get('size', 0))) != 0
        }
        
        # Reconcile (this marks orphaned positions and moves them to closed)
        issues = self.registry.reconcile_with_exchange(exchange_positions, orders)
        
        # Persist any orphaned positions (now in closed history)
        # This ensures they're saved as ORPHANED and won't be reloaded as ACTIVE
        orphaned_count = 0
        for symbol, issue in issues:
            if "ORPHANED" in issue:
                # Find in closed positions (just moved there by reconcile)
                for pos in self.registry._closed_positions:
                    if pos.symbol == symbol and pos.state.value == "orphaned":
                        self.persistence.save_position(pos)
                        orphaned_count += 1
                        break
        
        if orphaned_count > 0:
            logger.info("Persisted orphaned positions", count=orphaned_count)
        
        # Get corrective actions
        actions = self.position_manager.reconcile(exchange_positions, orders)
        
        # Execute corrective actions
        for action in actions:
            await self.execute_action(action)
        
        return {
            "issues": issues,
            "actions_taken": len(actions),
            "exchange_positions": len(exchange_positions),
            "registry_positions": len(self.registry.get_all_active())
        }
    
    # ========== STARTUP / RECOVERY ==========
    
    async def startup(self) -> None:
        """
        Startup procedure.
        
        1. Load persisted state
        2. Sync with exchange
        3. Resolve discrepancies
        """
        logger.info("ExecutionGateway starting up...")
        
        # Load persisted registry
        persisted_registry = self.persistence.load_registry()
        
        # Merge into current registry
        for pos in persisted_registry.get_all():
            if pos.symbol not in self.registry._positions:
                self.registry._positions[pos.symbol] = pos
        
        # Sync with exchange
        sync_result = await self.sync_with_exchange()
        
        logger.info(
            "ExecutionGateway startup complete",
            positions=len(self.registry.get_all_active()),
            issues=len(sync_result.get("issues", []))
        )
    
    def get_metrics(self) -> Dict:
        """Get gateway metrics."""
        return {
            **self.metrics,
            "pending_orders": len(self._pending_orders),
            "active_positions": len(self.registry.get_all_active()),
            "manager_metrics": self.position_manager.metrics
        }
