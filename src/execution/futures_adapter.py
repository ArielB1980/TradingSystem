"""
Kraken Futures adapter for order execution.

Handles:
- Spot-to-futures ticker mapping
- Leverage setting
- Reduce-only orders
- Order submission
"""
from decimal import Decimal
from typing import Optional
from datetime import datetime, timezone
from src.domain.models import Order, OrderType, OrderStatus, Side
from src.data.kraken_client import KrakenClient
from src.monitoring.logger import get_logger
import uuid

logger = get_logger(__name__)


class FuturesAdapter:
    """
    Kraken Futures order execution adapter.
    
    Maps spot tickers to futures contracts and handles order placement.
    """
    
    # Spot → Futures mapping (Kraken uses PF_ prefix for perpetuals)
    TICKER_MAP = {
        "BTC/USD": "PF_XBTUSD",  # Kraken Futures BTC perpetual
        "ETH/USD": "PF_ETHUSD",   # Kraken Futures ETH perpetual
        "SOL/USD": "PF_SOLUSD",
        "LINK/USD": "PF_LINKUSD",
        "AVAX/USD": "PF_AVAXUSD",
        "MATIC/USD": "PF_MATICUSD",
    }
    
    def __init__(self, kraken_client: KrakenClient, max_leverage: float = 10.0):
        """
        Initialize futures adapter.
        
        Args:
            kraken_client: Kraken client for API calls
            max_leverage: Maximum leverage cap (hard limit)
        """
        self.kraken_client = kraken_client
        self.max_leverage = max_leverage
        
        logger.info("Futures Adapter initialized", max_leverage=max_leverage)
    
    @staticmethod
    def map_spot_to_futures(spot_symbol: str) -> str:
        """
        Map spot symbol to futures symbol.
        
        Args:
            spot_symbol: Spot symbol (e.g., "BTC/USD")
        
        Returns:
            Futures symbol (e.g., "BTCUSD-PERP")
        
        Raises:
            ValueError: If symbol not supported
        """
        futures_symbol = FuturesAdapter.TICKER_MAP.get(spot_symbol)
        
        if futures_symbol:
            return futures_symbol
            
        # Dynamic Fallback: PF_{BASE}USD
        # Most tickers follow this pattern (PF_ETHUSD, PF_SOLUSD, etc.)
        try:
            base = spot_symbol.split('/')[0]
            # Handle special cases if any (BTC is handled in TICKER_MAP)
            dynamic_symbol = f"PF_{base}USD"
            return dynamic_symbol
        except IndexError:
            raise ValueError(f"Invalid spot symbol format: {spot_symbol}")
        
        return futures_symbol
    
    async def place_order(
        self,
        symbol: str,
        side: Side,
        size_notional: Decimal,
        leverage: Decimal,
        order_type: OrderType = OrderType.LIMIT,
        price: Optional[Decimal] = None,
        reduce_only: bool = False,
    ) -> Order:
        """
        Place order on Kraken Futures.
        
        Args:
            symbol: Futures symbol (e.g., "BTCUSD-PERP")
            side: Order side (LONG/SHORT)
            size_notional: Position size in USD notional
            leverage: Leverage to use (capped at max_leverage)
            order_type: Order type
            price: Limit price (required for limit orders)
            reduce_only: Whether order is reduce-only (for SL/TP)
        
        Returns:
            Order object
        """
        # Cap leverage
        if leverage > Decimal(str(self.max_leverage)):
            logger.warning(
                "Leverage capped",
                requested=str(leverage),
                max=self.max_leverage,
            )
            leverage = Decimal(str(self.max_leverage))
        
        # Generate client order ID
        client_order_id = f"order_{uuid.uuid4().hex[:16]}"
        
        # Map order type to Kraken format
        kraken_order_type_map = {
            OrderType.LIMIT: "lmt",
            OrderType.MARKET: "mkt",
            OrderType.STOP_LOSS: "stp",
            OrderType.TAKE_PROFIT: "take_profit",
        }
        kraken_order_type = kraken_order_type_map.get(order_type, "lmt")
        
        # Map side to Kraken format
        kraken_side = "buy" if side == Side.LONG else "sell"
        
        # 1. Fetch instrument metadata to get contract size
        instruments = await self.kraken_client.get_futures_instruments()
        instr = next((i for i in instruments if i['symbol'].upper() == symbol.upper()), None)
        
        if not instr:
            raise ValueError(f"Instrument specs for {symbol} not found")
        
        contract_size = Decimal(str(instr.get('contractSize', 1)))
        
        # 2. Convert USD notional to contract count
        # Formula: size_contracts = Position Notional / (Entry Price * Contract Multiplier)
        size_contracts = (size_notional / (price * contract_size)).quantize(
            Decimal("0.0001"), rounding="ROUND_DOWN"
        )
        
        logger.info(
            "Converting notional to contracts",
            symbol=symbol,
            notional=float(size_notional),
            price=float(price),
            multiplier=float(contract_size),
            contracts=float(size_contracts)
        )
        
        try:
            # Place order via Kraken Futures API
            response = await self.kraken_client.place_futures_order(
                symbol=symbol,
                side=kraken_side,
                order_type=kraken_order_type,
                size=float(size_contracts),
                price=price,
                stop_price=price if order_type in [OrderType.STOP_LOSS, OrderType.TAKE_PROFIT] else None,
                reduce_only=reduce_only,
                leverage=leverage,
                client_order_id=client_order_id,
            )
            
            # Extract order details from response
            send_status = response.get("sendStatus", {})
            order_id = send_status.get("order_id", f"unknown_{uuid.uuid4().hex[:16]}")
            status_str = send_status.get("status", "placed")
            
            # Map status to our OrderStatus enum
            status_map = {
                "placed": OrderStatus.SUBMITTED,
                "cancelled": OrderStatus.CANCELLED,
                "filled": OrderStatus.FILLED,
            }
            status = status_map.get(status_str, OrderStatus.SUBMITTED)
            
            # Create Order object
            order = Order(
                order_id=order_id,
                client_order_id=client_order_id,
                timestamp=datetime.now(timezone.utc),
                symbol=symbol,
                side=side,
                order_type=order_type,
                size=contract_size,
                price=price,
                status=status,
                reduce_only=reduce_only,
            )
            
            logger.info(
                "Futures order placed successfully",
                symbol=symbol,
                order_id=order_id,
                side=side.value,
                size=str(contract_size),
                leverage=str(leverage),
            )
            
            return order
            
        except Exception as e:
            logger.error(
                "Failed to place futures order",
                symbol=symbol,
                error=str(e),
            )
            raise

    async def cancel_order(self, order_id: str, symbol: str) -> None:
        """
        Cancel a futures order.
        
        Args:
            order_id: Order ID to cancel
            symbol: Futures symbol
        """
        try:
            await self.kraken_client.cancel_futures_order(order_id, symbol)
            logger.info("Order cancelled via adapter", order_id=order_id, symbol=symbol)
        except Exception as e:
            logger.error("Failed to cancel order via adapter", order_id=order_id, symbol=symbol, error=str(e))
            raise

