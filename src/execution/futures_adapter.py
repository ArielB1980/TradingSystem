"""
Kraken Futures adapter for order execution.

Handles:
- Spot-to-futures ticker mapping
- Leverage setting (flexible/fixed/unknown from InstrumentSpec)
- Size rounding via instrument specs
- Reduce-only orders
- Order submission
"""
from decimal import Decimal
from typing import Dict, Optional, Any
from datetime import datetime, timezone
from src.domain.models import Order, OrderType, OrderStatus, Side
from src.data.kraken_client import KrakenClient
from src.monitoring.logger import get_logger
from src.execution.instrument_specs import (
    InstrumentSpecRegistry,
    InstrumentSpec,
    compute_size_contracts,
    resolve_leverage,
)
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
        instrument_spec_registry: Optional[InstrumentSpecRegistry] = None,
    ):
        """
        Initialize futures adapter.

        Args:
            kraken_client: Kraken client for API calls
            max_leverage: Maximum leverage cap (hard limit)
            spot_to_futures_override: Optional mapping from market discovery (spot -> futures). Used first.
            position_size_is_notional: If True, exchange returns size as notional USD. If False, returns contracts.
            instrument_spec_registry: Optional registry for specs; when set, size/leverage use spec (min_size, step, leverage_mode).
        """
        self.kraken_client = kraken_client
        self.max_leverage = max_leverage
        self.spot_to_futures_override = spot_to_futures_override or {}
        self.position_size_is_notional = position_size_is_notional
        self.instrument_spec_registry = instrument_spec_registry
        self.cached_futures_tickers: Optional[Dict[str, Any]] = None
        logger.info(
            "Futures Adapter initialized",
            max_leverage=max_leverage,
            override_size=len(self.spot_to_futures_override),
            has_spec_registry=instrument_spec_registry is not None,
        )

    def set_spot_to_futures_override(self, mapping: Dict[str, str]) -> None:
        """Update mapping from market discovery (spot -> futures)."""
        self.spot_to_futures_override = mapping or {}
    
    def update_cached_futures_tickers(self, futures_tickers: Dict[str, any]) -> None:
        """Update cached futures tickers for use when futures_tickers not provided to map_spot_to_futures()."""
        self.cached_futures_tickers = futures_tickers

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
                            If None, will use cached_futures_tickers if available.
        
        Returns:
            Futures symbol (e.g., "THETA/USD:USD" or "PF_THETAUSD")
        """
        # Use provided tickers, or fall back to cached tickers
        tickers_to_use = futures_tickers or self.cached_futures_tickers
        
        result = self._find_best_executable_symbol(spot_symbol, tickers_to_use)
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
    
    def notional_to_contracts(
        self,
        notional_usd: Decimal,
        mark_price: Decimal,
    ) -> Decimal:
        """
        Convert USD notional to contracts.
        
        For Kraken perps, contracts = notional / mark_price (1 contract = 1 USD notional at mark price).
        This matches the logic used in ExecutionEngine.generate_entry_plan().
        
        Args:
            notional_usd: Position size in USD notional
            mark_price: Current mark price
        
        Returns:
            Number of contracts
        """
        if mark_price <= 0:
            raise ValueError(f"Invalid mark price for contract conversion: {mark_price}")
        return notional_usd / mark_price
    
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
        # Cap leverage (config cap)
        lev_int = max(1, int(leverage))
        if leverage > Decimal(str(self.max_leverage)):
            logger.warning("Leverage capped", requested=str(leverage), max=self.max_leverage)
            lev_int = int(self.max_leverage)
        
        client_order_id = f"order_{uuid.uuid4().hex[:16]}"
        kraken_order_type_map = {
            OrderType.LIMIT: "lmt",
            OrderType.MARKET: "mkt",
            OrderType.STOP_LOSS: "stp",
            OrderType.TAKE_PROFIT: "take_profit",
        }
        kraken_order_type = kraken_order_type_map.get(order_type, "lmt")
        kraken_side = "buy" if side == Side.LONG else "sell"
        
        price_use = price if price and price > 0 else size_notional / Decimal("1")  # fallback for market
        size_contracts: Decimal
        effective_leverage: Optional[Decimal]
        contract_size = Decimal("1")
        
        if self.instrument_spec_registry:
            await self.instrument_spec_registry.refresh()
            spec = self.instrument_spec_registry.get_spec(symbol)
            if not spec:
                logger.error(
                    "AUCTION_OPEN_REJECTED",
                    symbol=symbol,
                    reason="NO_SPEC",
                    requested_leverage=lev_int,
                    spec_summary=None,
                )
                raise ValueError(f"Instrument specs for {symbol} not found")
            size_contracts, size_reason = compute_size_contracts(spec, size_notional, price_use)
            if size_reason:
                logger.warning(
                    "AUCTION_OPEN_REJECTED",
                    symbol=symbol,
                    reason=size_reason,
                    requested_leverage=lev_int,
                    spec_summary={"min_size": str(spec.min_size), "size_step": str(spec.size_step), "leverage_mode": spec.leverage_mode, "max_leverage": spec.max_leverage},
                )
                raise ValueError(f"Size validation failed: {size_reason}")
            contract_size = spec.contract_size
            effective_lev, lev_reason = resolve_leverage(spec, lev_int)
            if lev_reason:
                logger.warning(
                    "AUCTION_OPEN_REJECTED",
                    symbol=symbol,
                    reason=lev_reason,
                    requested_leverage=lev_int,
                    spec_summary={"leverage_mode": spec.leverage_mode, "allowed_leverages": spec.allowed_leverages, "max_leverage": spec.max_leverage},
                )
                raise ValueError(f"Leverage rejected: {lev_reason}")
            effective_leverage = Decimal(str(effective_lev)) if effective_lev is not None else None
            if effective_leverage is None and not reduce_only:
                self.instrument_spec_registry.log_unknown_leverage_once(symbol)
        else:
            # Legacy path: fetch instruments and resolve size manually
            instruments = await self.kraken_client.get_futures_instruments()
            symbol_upper = symbol.upper()
            base = symbol_upper.split("/")[0] if "/" in symbol_upper else symbol_upper.replace("PF_", "").replace("USD", "").replace(":", "").replace("-", "")
            if not base:
                base = symbol_upper
            lookup_variants = [f"PF_{base}USD", f"{base}USD", f"{base}/USD:USD", symbol_upper]
            instr = None
            for v in lookup_variants:
                instr = next((i for i in instruments if str(i.get("symbol", "")).strip().upper() == v.upper()), None)
                if instr:
                    break
            if not instr:
                logger.error("Instrument specs not found", requested_symbol=symbol, total_instruments=len(instruments))
                raise ValueError(f"Instrument specs for {symbol} not found")
            contract_size = Decimal(str(instr.get("contractSize", 1)))
            size_contracts = (size_notional / (price_use * contract_size)).quantize(Decimal("0.0001"), rounding="ROUND_DOWN")
            effective_leverage = leverage
        
        logger.info(
            "Converting notional to contracts",
            symbol=symbol,
            notional=float(size_notional),
            price=float(price_use),
            multiplier=float(contract_size),
            contracts=float(size_contracts),
        )
        
        try:
            response = await self.kraken_client.place_futures_order(
                symbol=symbol,
                side=kraken_side,
                order_type=kraken_order_type,
                size=float(size_contracts),
                price=price,
                stop_price=price if order_type in [OrderType.STOP_LOSS, OrderType.TAKE_PROFIT] else None,
                reduce_only=reduce_only,
                leverage=effective_leverage,
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
        # Skip cancellation for "unknown_" order IDs - these are placeholders when exchange
        # doesn't return a proper order_id. They can't be cancelled because they don't exist on exchange.
        if order_id and order_id.startswith("unknown_"):
            logger.debug(
                "Skipping cancellation for placeholder order ID",
                order_id=order_id,
                symbol=symbol,
                reason="Placeholder ID - order may not exist on exchange"
            )
            return
        
        try:
            await self.kraken_client.cancel_futures_order(order_id, symbol)
            logger.info("Order cancelled via adapter", order_id=order_id, symbol=symbol)
        except Exception as e:
            # Don't raise for invalidArgument errors - order may already be cancelled or not exist
            error_str = str(e)
            if "invalidArgument" in error_str or "order_id" in error_str.lower():
                logger.warning(
                    "Order cancellation skipped - invalid order ID",
                    order_id=order_id,
                    symbol=symbol,
                    error=error_str
                )
                return
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

