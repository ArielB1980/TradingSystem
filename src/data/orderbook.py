"""
Futures mark price and best bid/ask tracking.

CRITICAL: Mark price MUST be sourced from Kraken Futures mark/index feed,
not computed from bid/ask. This module tracks but does NOT compute mark price.
"""
from decimal import Decimal
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional
from src.monitoring.logger import get_logger

logger = get_logger(__name__)


@dataclass
class OrderBookSnapshot:
    """
    Minimal order book data for futures market.
    
    NOTE: This is NOT a full depth-of-book engine. Only tracks best bid/ask
    and mark price from exchange feed.
    """
    symbol: str  # Futures symbol (e.g., "BTCUSD-PERP")
    mark_price: Decimal  # From exchange mark/index feed (authoritative)
    best_bid: Optional[Decimal]  # Best bid price
    best_ask: Optional[Decimal]  # Best ask price
    timestamp: datetime
    
    def spread_pct(self) -> Optional[Decimal]:
        """Calculate bid-ask spread as percentage."""
        if self.best_bid and self.best_ask:
            return (self.best_ask - self.best_bid) / self.mark_price
        return None


class OrderBook:
    """
    Tracks futures mark price and best bid/ask from exchange feed.
    
    Design locks enforced:
    - Mark price is from exchange feed (never computed)
    - No strategy logic (tracking only)
    - No microstructure analysis
    """
    
    def __init__(self, symbol: str):
        """
        Initialize order book tracker.
        
        Args:
            symbol: Futures symbol (e.g., "BTCUSD-PERP")
        """
        self.symbol = symbol
        self.current: Optional[OrderBookSnapshot] = None
        
        logger.info("OrderBook initialized", symbol=symbol)
    
    def update_mark_price(self, mark_price: Decimal, timestamp: Optional[datetime] = None):
        """
        Update mark price from exchange feed.
        
        Args:
            mark_price: Mark price from Kraken Futures mark/index feed
            timestamp: Timestamp of update (defaults to now)
        """
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)
        
        if self.current:
            self.current = OrderBookSnapshot(
                symbol=self.symbol,
                mark_price=mark_price,
                best_bid=self.current.best_bid,
                best_ask=self.current.best_ask,
                timestamp=timestamp,
            )
        else:
            self.current = OrderBookSnapshot(
                symbol=self.symbol,
                mark_price=mark_price,
                best_bid=None,
                best_ask=None,
                timestamp=timestamp,
            )
        
        logger.debug(
            "Mark price updated",
            symbol=self.symbol,
            mark_price=str(mark_price),
        )
    
    def update_best_bid_ask(
        self,
        best_bid: Decimal,
        best_ask: Decimal,
        timestamp: Optional[datetime] = None,
    ):
        """
        Update best bid/ask from exchange feed.
        
        Args:
            best_bid: Best bid price
            best_ask: Best ask price
            timestamp: Timestamp of update (defaults to now)
        """
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)
        
        if self.current:
            self.current = OrderBookSnapshot(
                symbol=self.symbol,
                mark_price=self.current.mark_price,
                best_bid=best_bid,
                best_ask=best_ask,
                timestamp=timestamp,
            )
        else:
            # No mark price yet - log warning
            logger.warning(
                "Best bid/ask updated before mark price",
                symbol=self.symbol,
            )
    
    def get_mark_price(self) -> Optional[Decimal]:
        """
        Get current mark price.
        
        Returns:
            Mark price or None if not yet available
        """
        return self.current.mark_price if self.current else None
    
    def get_snapshot(self) -> Optional[OrderBookSnapshot]:
        """Get current order book snapshot."""
        return self.current
