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
        shadow_mode: bool = False
    ):
        """
        Initialize the execution gateway.
        
        Args:
            exchange_client: Exchange API client
            registry: Position registry (uses singleton if not provided)
            position_manager: Position manager (creates new if not provided)
            persistence: Persistence layer (optional, creates default if not provided)
            shadow_mode: If True, log but don't actually execute
        """
        self.client = exchange_client
        self.registry = registry or get_position_registry()
        self.position_manager = position_manager or PositionManagerV2(self.registry, shadow_mode)
        self.persistence = persistence or PositionPersistence()
        self.shadow_mode = shadow_mode
        
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
    
    # ========== ORDER SUBMISSION ==========
    
    async def execute_action(self, action: ManagementAction) -> ExecutionResult:
        """
        Execute a management action.
        
        This is the ONLY way to place orders.
        """
        if self.shadow_mode:
            logger.info(
                "[SHADOW] Would execute action",
                type=action.type.value,
                symbol=action.symbol,
                reason=action.reason
            )
            return ExecutionResult(
                success=True,
                client_order_id=action.client_order_id,
                error="Shadow mode - not executed"
            )
        
        try:
            if action.type == ActionType.OPEN_POSITION:
                return await self._execute_entry(action)
            
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
    
    async def _execute_entry(self, action: ManagementAction) -> ExecutionResult:
        """Execute entry order."""
        self.metrics["orders_submitted"] += 1
        
        # Track pending order
        pending = PendingOrder(
            client_order_id=action.client_order_id,
            position_id=action.position_id,
            symbol=action.symbol,
            purpose=OrderPurpose.ENTRY,
            side=action.side,
            size=action.size,
            price=action.price,
            order_type=action.order_type,
            submitted_at=datetime.now(timezone.utc)
        )
        self._pending_orders[action.client_order_id] = pending
        
        # Submit to exchange
        try:
            order_side = "buy" if action.side == Side.LONG else "sell"
            
            result = await self.client.create_order(
                symbol=action.symbol,
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
                symbol=action.symbol,
                client_order_id=action.client_order_id,
                exchange_order_id=exchange_order_id
            )
            
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
        
        try:
            # Close via reduce-only market order
            close_side = "sell" if action.side == Side.LONG else "buy"
            
            result = await self.client.create_order(
                symbol=action.symbol,
                type="market",
                side=close_side,
                amount=float(action.size),
                params={
                    "clientOrderId": action.client_order_id,
                    "reduceOnly": True
                }
            )
            
            exchange_order_id = result.get("id")
            pending.exchange_order_id = exchange_order_id
            pending.status = "submitted"
            self._order_id_map[exchange_order_id] = action.client_order_id
            
            logger.info(
                "Close order submitted",
                symbol=action.symbol,
                reason=action.exit_reason.value if action.exit_reason else "unknown"
            )
            
            return ExecutionResult(
                success=True,
                client_order_id=action.client_order_id,
                exchange_order_id=exchange_order_id
            )
            
        except Exception as e:
            pending.status = "failed"
            logger.error(f"Close order failed: {e}")
            return ExecutionResult(
                success=False,
                client_order_id=action.client_order_id,
                error=str(e)
            )
    
    async def _execute_partial_close(self, action: ManagementAction) -> ExecutionResult:
        """Execute partial position close."""
        self.metrics["orders_submitted"] += 1
        
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
        
        try:
            close_side = "sell" if action.side == Side.LONG else "buy"
            
            result = await self.client.create_order(
                symbol=action.symbol,
                type="market",
                side=close_side,
                amount=float(action.size),
                params={
                    "clientOrderId": action.client_order_id,
                    "reduceOnly": True
                }
            )
            
            exchange_order_id = result.get("id")
            pending.exchange_order_id = exchange_order_id
            self._order_id_map[exchange_order_id] = action.client_order_id
            
            return ExecutionResult(
                success=True,
                client_order_id=action.client_order_id,
                exchange_order_id=exchange_order_id
            )
            
        except Exception as e:
            logger.error(f"Partial close failed: {e}")
            return ExecutionResult(
                success=False,
                client_order_id=action.client_order_id,
                error=str(e)
            )
    
    async def _execute_place_stop(self, action: ManagementAction) -> ExecutionResult:
        """Place stop loss order."""
        self.metrics["orders_submitted"] += 1
        
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
        
        try:
            stop_side = "sell" if action.side == Side.LONG else "buy"
            
            result = await self.client.create_order(
                symbol=action.symbol,
                type="stop",
                side=stop_side,
                amount=float(action.size),
                price=float(action.price),
                params={
                    "clientOrderId": action.client_order_id,
                    "reduceOnly": True,
                    "stopPrice": float(action.price)
                }
            )
            
            exchange_order_id = result.get("id")
            pending.exchange_order_id = exchange_order_id
            self._order_id_map[exchange_order_id] = action.client_order_id
            
            # Update position with stop order ID
            position = self.registry.get_position(action.symbol)
            if position:
                position.stop_order_id = exchange_order_id
                self.persistence.save_position(position)
            
            logger.info(
                "Stop order placed",
                symbol=action.symbol,
                price=str(action.price)
            )
            
            return ExecutionResult(
                success=True,
                client_order_id=action.client_order_id,
                exchange_order_id=exchange_order_id
            )
            
        except Exception as e:
            logger.error(f"Stop placement failed: {e}")
            return ExecutionResult(
                success=False,
                client_order_id=action.client_order_id,
                error=str(e)
            )
    
    async def _execute_update_stop(self, action: ManagementAction) -> ExecutionResult:
        """Update stop loss order (cancel + replace)."""
        position = self.registry.get_position(action.symbol)
        if not position:
            return ExecutionResult(
                success=False,
                client_order_id=action.client_order_id,
                error="Position not found"
            )
        
        # Cancel existing stop
        if position.stop_order_id:
            try:
                await self.client.cancel_order(position.stop_order_id, action.symbol)
                self.metrics["orders_cancelled"] += 1
            except Exception as e:
                logger.warning(f"Failed to cancel old stop: {e}")
        
        # Update position stop price
        if not position.update_stop(action.price):
            return ExecutionResult(
                success=False,
                client_order_id=action.client_order_id,
                error="Stop update rejected by state machine"
            )
        
        # Place new stop at updated price
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
        
        try:
            await self.client.cancel_order(position.stop_order_id, action.symbol)
            self.metrics["orders_cancelled"] += 1
            return ExecutionResult(
                success=True,
                client_order_id=action.client_order_id
            )
        except Exception as e:
            return ExecutionResult(
                success=False,
                client_order_id=action.client_order_id,
                error=str(e)
            )
    
    async def _execute_flatten_orphan(self, action: ManagementAction) -> ExecutionResult:
        """Flatten orphan position on exchange."""
        logger.critical(f"FLATTENING ORPHAN POSITION: {action.symbol}")
        
        try:
            # Use exchange's close_position command
            result = await self.client.close_position(action.symbol)
            
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
        
        # Create event
        pending.last_event_seq += 1
        event = OrderEvent(
            order_id=exchange_order_id,
            client_order_id=client_order_id,
            event_type=event_type,
            event_seq=pending.last_event_seq,
            timestamp=datetime.now(timezone.utc),
            fill_qty=filled if event_type in (OrderEventType.FILLED, OrderEventType.PARTIAL_FILL) else None,
            fill_price=Decimal(str(order_data.get("average", 0))) if filled > 0 else None,
            fill_id=order_data.get("trades", [{}])[0].get("id") if order_data.get("trades") else None
        )
        
        # Route to position manager
        follow_up = self.position_manager.handle_order_event(pending.symbol, event)
        
        # Update pending status
        if event_type == OrderEventType.FILLED:
            pending.status = "filled"
            self.metrics["orders_filled"] += 1
        elif event_type == OrderEventType.CANCELLED:
            pending.status = "cancelled"
            self.metrics["orders_cancelled"] += 1
        elif event_type == OrderEventType.REJECTED:
            pending.status = "rejected"
            self.metrics["orders_rejected"] += 1
        
        # Persist position state
        position = self.registry.get_position(pending.symbol)
        if position:
            self.persistence.save_position(position)
        
        # Execute follow-up actions
        for action in follow_up:
            await self.execute_action(action)
        
        return follow_up
    
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
        orders = await self.client.fetch_open_orders()
        
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
        
        # Reconcile
        issues = self.registry.reconcile_with_exchange(exchange_positions, orders)
        
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
