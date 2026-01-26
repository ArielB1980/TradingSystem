"""
Kraken Futures adapter for order execution.

Handles:
- Spot-to-futures ticker mapping
- Leverage setting
- Reduce-only orders
- Order submission
"""
from decimal import Decimal
from typing import Dict, Optional
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
    
    # Spot → Futures mapping (Kraken uses PF_ prefix for perpetuals, e.g. PF_XBTUSD).
    # Covers common pairs; market discovery supplies spot_to_futures_override for the full universe.
    TICKER_MAP = {
        "BTC/USD": "PF_XBTUSD",
        "ETH/USD": "PF_ETHUSD",
        "SOL/USD": "PF_SOLUSD",
        "LINK/USD": "PF_LINKUSD",
        "AVAX/USD": "PF_AVAXUSD",
        "MATIC/USD": "PF_MATICUSD",
        "XRP/USD": "PF_XRPUSD",
        "DOGE/USD": "PF_DOGEUSD",
        "ADA/USD": "PF_ADAUSD",
        "DOT/USD": "PF_DOTUSD",
        "UNI/USD": "PF_UNIUSD",
        "ATOM/USD": "PF_ATOMUSD",
        "LTC/USD": "PF_LTCUSD",
        "BCH/USD": "PF_BCHUSD",
        "ETC/USD": "PF_ETCUSD",
        "XLM/USD": "PF_XLMUSD",
        "ALGO/USD": "PF_ALGOUSD",
        "FIL/USD": "PF_FILUSD",
        "TRX/USD": "PF_TRXUSD",
        "APT/USD": "PF_APTUSD",
        "ARB/USD": "PF_ARBUSD",
        "OP/USD": "PF_OPUSD",
        "SUI/USD": "PF_SUIUSD",
        "SEI/USD": "PF_SEIUSD",
        "NEAR/USD": "PF_NEARUSD",
        "INJ/USD": "PF_INJUSD",
        "PEPE/USD": "PF_PEPEUSD",
    }

    def __init__(
        self,
        kraken_client: KrakenClient,
        max_leverage: float = 10.0,
        spot_to_futures_override: Optional[Dict[str, str]] = None,
        position_size_is_notional: bool = False,
    ):
        """
        Initialize futures adapter.

        Args:
            kraken_client: Kraken client for API calls
            max_leverage: Maximum leverage cap (hard limit)
            spot_to_futures_override: Optional mapping from market discovery (spot -> futures). Used first.
            position_size_is_notional: If True, exchange returns size as notional USD. If False, returns contracts.
        """
        self.kraken_client = kraken_client
        self.max_leverage = max_leverage
        self.spot_to_futures_override = spot_to_futures_override or {}
        self.position_size_is_notional = position_size_is_notional
        logger.info(
            "Futures Adapter initialized",
            max_leverage=max_leverage,
            override_size=len(self.spot_to_futures_override),
        )

    def set_spot_to_futures_override(self, mapping: Dict[str, str]) -> None:
        """Update mapping from market discovery (spot -> futures)."""
        self.spot_to_futures_override = mapping or {}

    def _find_best_executable_symbol(
        self, spot_symbol: str, futures_tickers: Optional[Dict[str, any]] = None
    ) -> Optional[str]:
        """
        Find the best executable futures symbol for a spot symbol using ticker lookup.
        
        Priority:
        1. Discovery override (usually CCXT unified "BASE/USD:USD")
        2. Check futures_tickers for derived keys (prefer CCXT unified, then PF_, then raw)
        3. TICKER_MAP lookup
        4. Fallback: PF_{BASE}USD
        
        Args:
            spot_symbol: Spot symbol (e.g., "THETA/USD")
            futures_tickers: Optional dict of futures tickers (from get_futures_tickers_bulk)
        
        Returns:
            Best executable futures symbol, or None if not found
        """
        # Priority 1: Discovery override
        override = self.spot_to_futures_override.get(spot_symbol)
        if override:
            # If tickers provided, verify override exists
            if futures_tickers is None or override in futures_tickers:
                return override
        
        # Priority 2: Check futures_tickers for derived keys
        if futures_tickers:
            base = spot_symbol.split("/")[0]
            if base == "XBT":
                base = "BTC"
            
            # Try CCXT unified first (preferred for execution)
            ccxt_unified = f"{base}/USD:USD"
            if ccxt_unified in futures_tickers:
                return ccxt_unified
            
            # Try PF_ format
            pf_key = f"PF_{base}USD"
            if pf_key in futures_tickers:
                return pf_key
            
            # Try raw formats (PI_, PF_, FI_)
            for prefix in ["PI_", "PF_", "FI_"]:
                raw_key = f"{prefix}{base}USD"
                if raw_key in futures_tickers:
                    return raw_key
        
        # Priority 3: TICKER_MAP
        mapped = FuturesAdapter.TICKER_MAP.get(spot_symbol)
        if mapped:
            # If tickers provided, verify mapped symbol exists
            if futures_tickers is None or mapped in futures_tickers:
                return mapped
        
        # Priority 4: Fallback
        try:
            base = spot_symbol.split("/")[0]
            if base == "XBT":
                base = "BTC"
            return f"PF_{base}USD"
        except IndexError:
            return None
    
    def map_spot_to_futures(
        self, spot_symbol: str, futures_tickers: Optional[Dict[str, any]] = None
    ) -> str:
        """
        Map spot symbol to futures symbol.
        
        Uses override (e.g. from market discovery) first, then checks futures_tickers
        for best executable symbol, then TICKER_MAP, then PF_{BASE}USD.
        
        Args:
            spot_symbol: Spot symbol (e.g., "THETA/USD")
            futures_tickers: Optional dict of futures tickers (from get_futures_tickers_bulk)
                            If provided, will find best executable symbol that exists in tickers.
        
        Returns:
            Futures symbol (e.g., "THETA/USD:USD" or "PF_THETAUSD")
        """
        result = self._find_best_executable_symbol(spot_symbol, futures_tickers)
        if result:
            return result
        
        # Final fallback if _find_best_executable_symbol returns None
        try:
            base = spot_symbol.split("/")[0]
            if base == "XBT":
                base = "BTC"
            return f"PF_{base}USD"
        except IndexError:
            raise ValueError(f"Invalid spot symbol format: {spot_symbol}")
    
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
            symbol: Futures symbol (e.g., "PF_XBTUSD" on Kraken)
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
        
        # Try to find instrument by symbol - instruments API may return different formats
        # Try: PF_AUDUSD, AUDUSD, AUD/USD:USD, etc.
        instr = None
        symbol_upper = symbol.upper()
        
        # First try exact match
        instr = next((i for i in instruments if i.get('symbol', '').upper() == symbol_upper), None)
        
        if not instr:
            # Try without PF_ prefix
            symbol_no_prefix = symbol_upper.replace('PF_', '')
            instr = next((i for i in instruments if i.get('symbol', '').upper() == symbol_no_prefix), None)
        
        if not instr:
            # Try with /USD:USD format (CCXT unified)
            base = symbol_upper.replace('PF_', '').replace('USD', '')
            if base:
                unified_format = f"{base}/USD:USD"
                instr = next((i for i in instruments if i.get('symbol', '').upper() == unified_format), None)
        
        if not instr:
            # Log available symbols for debugging (first 20 that contain similar base)
            base_part = symbol_upper.replace('PF_', '').replace('USD', '').replace('/', '')[:3]
            similar = [i.get('symbol', '') for i in instruments if base_part in i.get('symbol', '').upper()][:20]
            logger.error(
                "Instrument specs not found",
                requested_symbol=symbol,
                similar_symbols=similar,
                total_instruments=len(instruments),
            )
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
    
    async def position_size_notional(
        self, symbol: str, pos_data: Dict, current_price: Decimal
    ) -> Optional[Decimal]:
        """
        Convert position size from exchange format to USD notional.
        
        CRITICAL: Centralizes the conversion logic to handle different exchange formats.
        Uses config flag to determine if exchange returns size as notional or contracts.
        
        Args:
            symbol: Futures symbol
            pos_data: Position data dict from exchange API (must have 'size' key)
            current_price: Current mark price for conversion (only used if size is in contracts)
        
        Returns:
            Position size in USD notional, or None if size is 0/missing
        """
        size_raw = pos_data.get('size', 0)
        if not size_raw or Decimal(str(size_raw)) == 0:
            return None
        
        size_value = Decimal(str(size_raw))
        
        if self.position_size_is_notional:
            # Exchange already returns size as notional USD - use directly
            size_notional = size_value
            logger.debug(
                "Position size already in notional (from exchange)",
                symbol=symbol,
                size_notional=float(size_notional)
            )
        else:
            # Exchange returns size in contracts/base units - convert to notional
            # Formula: notional = size_contracts * current_price
            # NOTE: For perpetuals, this is typically correct. For inverse contracts,
            # the formula may differ, but Kraken perpetuals use linear contracts.
            size_notional = size_value * current_price
            logger.debug(
                "Converted position size to notional",
                symbol=symbol,
                size_contracts=float(size_value),
                current_price=float(current_price),
                size_notional=float(size_notional)
            )
        
        return size_notional

