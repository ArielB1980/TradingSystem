"""
Order monitoring and timeout management.

Tracks submitted orders and automatically cancels unfilled orders after timeout.
"""
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Set
from src.domain.models import Order, OrderStatus
from src.monitoring.logger import get_logger

logger = get_logger(__name__)


@dataclass
class TrackedOrder:
    """Order with tracking metadata."""
    order: Order
    submitted_at: datetime
    timeout_seconds: int
    cancelled: bool = False
    
    @property
    def age_seconds(self) -> float:
        """Get order age in seconds."""
        return (datetime.now(timezone.utc) - self.submitted_at).total_seconds()
    
    @property
    def is_expired(self) -> bool:
        """Check if order has exceeded timeout."""
        return self.age_seconds > self.timeout_seconds
    
    @property
    def is_pending(self) -> bool:
        """Check if order is still pending (not filled or cancelled)."""
        return self.order.status in [OrderStatus.PENDING, OrderStatus.SUBMITTED]


class OrderMonitor:
    """
    Monitors submitted orders and handles timeouts.
    
    Responsibilities:
    - Track order lifecycle with timestamps
    - Detect timeout violations
    - Trigger automatic cancellation
    - Reconcile with exchange state
    - Emit monitoring events
    """
    
    def __init__(self, default_timeout_seconds: int = 30):
        """
        Initialize order monitor.
        
        Args:
            default_timeout_seconds: Default timeout for orders
        """
        self.default_timeout_seconds = default_timeout_seconds
        self.tracked_orders: Dict[str, TrackedOrder] = {}  # order_id -> TrackedOrder
        self.cancelled_order_ids: Set[str] = set()  # Track cancelled orders
        
        logger.info("OrderMonitor initialized", default_timeout=default_timeout_seconds)
    
    def track_order(
        self, 
        order: Order, 
        timeout_seconds: Optional[int] = None
    ) -> None:
        """
        Start tracking an order.
        
        Args:
            order: Order to track
            timeout_seconds: Custom timeout (uses default if None)
        """
        timeout = timeout_seconds or self.default_timeout_seconds
        
        tracked = TrackedOrder(
            order=order,
            submitted_at=datetime.now(timezone.utc),
            timeout_seconds=timeout
        )
        
        self.tracked_orders[order.order_id] = tracked
        
        logger.info(
            "Order tracking started",
            order_id=order.order_id,
            symbol=order.symbol,
            timeout=timeout
        )
    
    def update_order_status(self, order_id: str, status: OrderStatus) -> None:
        """
        Update order status from external source.
        
        Args:
            order_id: Order ID
            status: New status
        """
        if order_id in self.tracked_orders:
            self.tracked_orders[order_id].order.status = status
            
            # Remove from tracking if filled or cancelled
            if status in [OrderStatus.FILLED, OrderStatus.CANCELLED]:
                logger.info(
                    "Order completed, removing from tracking",
                    order_id=order_id,
                    status=status.value
                )
                del self.tracked_orders[order_id]
    
    def get_expired_orders(self) -> List[TrackedOrder]:
        """
        Get all orders that have exceeded timeout.
        
        Returns:
            List of expired TrackedOrder objects
        """
        expired = []
        
        for tracked in self.tracked_orders.values():
            if tracked.is_expired and tracked.is_pending and not tracked.cancelled:
                expired.append(tracked)
        
        return expired
    
    def mark_as_cancelled(self, order_id: str) -> None:
        """
        Mark order as cancelled (prevents re-cancellation).
        
        Args:
            order_id: Order ID
        """
        if order_id in self.tracked_orders:
            self.tracked_orders[order_id].cancelled = True
            self.cancelled_order_ids.add(order_id)
    
    def get_pending_orders(self) -> List[Order]:
        """
        Get all pending orders.
        
        Returns:
            List of pending Order objects
        """
        return [
            tracked.order 
            for tracked in self.tracked_orders.values() 
            if tracked.is_pending and not tracked.cancelled
        ]
    
    def reconcile_with_exchange(self, exchange_orders: List[Order]) -> Dict[str, str]:
        """
        Reconcile tracked orders with exchange state.
        
        Args:
            exchange_orders: Current orders from exchange
            
        Returns:
            Dict of discrepancies: {order_id: issue_description}
        """
        exchange_order_ids = {o.order_id for o in exchange_orders}
        tracked_order_ids = set(self.tracked_orders.keys())
        
        discrepancies = {}
        
        # Ghost orders: We think they exist but exchange doesn't have them
        ghost_orders = tracked_order_ids - exchange_order_ids - self.cancelled_order_ids
        for order_id in ghost_orders:
            tracked = self.tracked_orders[order_id]
            if tracked.is_pending:
                discrepancies[order_id] = "Ghost order: not found on exchange"
                logger.warning(
                    "Ghost order detected",
                    order_id=order_id,
                    symbol=tracked.order.symbol,
                    age=tracked.age_seconds
                )
                # Auto-remove ghost orders from tracking
                del self.tracked_orders[order_id]
        
        # Update status for orders found on exchange
        exchange_orders_map = {o.order_id: o for o in exchange_orders}
        for order_id in tracked_order_ids & exchange_order_ids:
            exchange_order = exchange_orders_map[order_id]
            if order_id in self.tracked_orders:
                self.update_order_status(order_id, exchange_order.status)
        
        return discrepancies
    
    def get_monitoring_stats(self) -> Dict:
        """
        Get monitoring statistics.
        
        Returns:
            Dict with monitoring metrics
        """
        pending = [t for t in self.tracked_orders.values() if t.is_pending]
        expired = self.get_expired_orders()
        
        return {
            "total_tracked": len(self.tracked_orders),
            "pending_count": len(pending),
            "expired_count": len(expired),
            "cancelled_count": len(self.cancelled_order_ids),
            "avg_age_seconds": sum(t.age_seconds for t in pending) / len(pending) if pending else 0,
            "oldest_order_age": max((t.age_seconds for t in pending), default=0)
        }
    
    def cleanup_old_records(self, max_age_hours: int = 24) -> int:
        """
        Remove old cancelled order IDs from tracking.
        
        Args:
            max_age_hours: Maximum age to keep records
            
        Returns:
            Number of records cleaned up
        """
        # For now, just clear the cancelled set periodically
        # In production, you'd want timestamp-based cleanup
        if len(self.cancelled_order_ids) > 1000:
            count = len(self.cancelled_order_ids)
            self.cancelled_order_ids.clear()
            logger.info("Cleaned up cancelled order records", count=count)
            return count
        return 0
