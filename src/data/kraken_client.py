"""
Kraken REST API and WebSocket client for spot and futures markets.

Handles:
- REST API calls (spot and futures)
- Authentication (API key + HMAC signature)
- Rate limiting (token bucket)
- WebSocket connections (spot and futures)
- Reconnection with exponential backoff
"""
import ccxt
import ccxt.async_support as ccxt_async
import hashlib
import hmac
import base64
import time
import asyncio
import json
import websockets
import aiohttp
import certifi
import ssl
from typing import Dict, List, Optional, Callable, Any
from datetime import datetime, timezone
from decimal import Decimal
from collections import deque
from dataclasses import dataclass
from src.monitoring.logger import get_logger
from src.domain.models import Candle
from src.constants import (
    PUBLIC_API_CAPACITY,
    PUBLIC_API_REFILL_RATE,
    PRIVATE_API_CAPACITY,
    PRIVATE_API_REFILL_RATE,
    KRAKEN_FUTURES_BASE_URL,
)
from src.exceptions import APIError, AuthenticationError
from src.utils.retry import retry_on_transient_errors

logger = get_logger(__name__)


@dataclass
class RateLimiter:
    """Token bucket rate limiter."""
    capacity: int  # Maximum tokens
    refill_rate: float  # Tokens per second
    tokens: float
    last_refill: float
    
    def __init__(self, capacity: int, refill_rate: float):
        self.capacity = capacity
        self.refill_rate = refill_rate
        self.tokens = float(capacity)
        self.last_refill = time.time()
    
    def consume(self, tokens: int = 1) -> bool:
        """
        Try to consume tokens.
        
        Returns:
            True if tokens consumed, False if insufficient tokens
        """
        self._refill()
        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False
    
    def _refill(self):
        """Refill tokens based on elapsed time."""
        now = time.time()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now
    
    async def wait_for_token(self):
        """Wait until a token is available."""
        while not self.consume(1):
            await asyncio.sleep(0.1)


class KrakenClient:
    """
    Kraken REST API client for spot and futures markets.
    """
    
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        futures_api_key: Optional[str] = None,
        futures_api_secret: Optional[str] = None,
        use_testnet: bool = False,
    ):
        """
        Initialize Kraken client.
        
        Args:
            api_key: Kraken spot API key
            api_secret: Kraken spot API secret
            futures_api_key: Kraken Futures API key (optional)
            futures_api_secret: Kraken Futures API secret (optional)
        """
        self.api_key = api_key
        self.api_secret = api_secret
        self.futures_api_key = futures_api_key
        self.futures_api_secret = futures_api_secret
        self.use_testnet = use_testnet
        
        # Helper to sanitize base64 secrets
        def sanitize_secret(secret: str) -> str:
            if not secret: return secret
            s = secret.strip()
            # Standardize padding
            return s + '=' * (-len(s) % 4)

        if self.api_secret:
            self.api_secret = sanitize_secret(self.api_secret)
        if self.futures_api_secret:
            self.futures_api_secret = sanitize_secret(self.futures_api_secret)
        
        if self.futures_api_secret:
            self.futures_api_secret = sanitize_secret(self.futures_api_secret)
        
        self.exchange = None
        self.futures_exchange = None
        
        # Rate limiters (configurable per endpoint group)
        self.public_limiter = RateLimiter(capacity=PUBLIC_API_CAPACITY, refill_rate=PUBLIC_API_REFILL_RATE)
        self.private_limiter = RateLimiter(capacity=PRIVATE_API_CAPACITY, refill_rate=PRIVATE_API_REFILL_RATE)
        
        # Reusable SSL context
        self._ssl_context = None
        
        logger.info("Kraken client configuration loaded")
    
    def has_valid_spot_credentials(self) -> bool:
        """Check if spot API keys are present."""
        return bool(self.api_key and self.api_secret and not self.api_key.startswith("${"))

    def has_valid_futures_credentials(self) -> bool:
        """Check if futures API keys are present."""
        return bool(self.futures_api_key and self.futures_api_secret and not self.futures_api_key.startswith("${"))

    async def initialize(self):
        """
        Lazy initialization of CCXT exchanges.
        MUST be called inside the running event loop of the target process.
        """
        # Initialize CCXT exchange (Spot - ASYNC)
        if not self.exchange:
            self.exchange = ccxt_async.kraken({
                'apiKey': self.api_key,
                'secret': self.api_secret,
                'enableRateLimit': True,
                'timeout': 30000,
            })

        # Initialize CCXT Futures Exchange (Futures - Async)
        if self.futures_api_key and self.futures_api_secret and not self.futures_exchange:
            self.futures_exchange = ccxt_async.krakenfutures({
                'apiKey': self.futures_api_key,
                'secret': self.futures_api_secret,
                'enableRateLimit': True,
                'timeout': 30000,
                'options': {'defaultType': 'future'},
            })
            if self.futures_exchange and self.use_testnet:
                 self.futures_exchange.set_sandbox_mode(True)
        
        logger.info("KrakenClient initialized (Lazy)")
        
        # Rate limiters (configurable per endpoint group)
        self.public_limiter = RateLimiter(capacity=PUBLIC_API_CAPACITY, refill_rate=PUBLIC_API_REFILL_RATE)
        self.private_limiter = RateLimiter(capacity=PRIVATE_API_CAPACITY, refill_rate=PRIVATE_API_REFILL_RATE)
        
        # Reusable SSL context
        self._ssl_context = None
        
        logger.info("Kraken client initialized")
    
    async def get_spot_balance(self) -> Dict[str, Any]:
        """
        Get spot account balance using CCXT.
        
        Returns:
            Dict containing balance info
        """
        await self.private_limiter.wait_for_token()
        
        try:
            # Now fully async
            balance = await self.exchange.fetch_balance()
            logger.debug("Fetched spot balance")
            return balance
        except Exception as e:
            logger.error("Failed to fetch spot balance", error=str(e))
            raise Exception(f"Spot API error: {str(e)}")

    async def get_spot_ticker(self, symbol: str) -> Dict:
        """Get current spot ticker information."""
        await self.public_limiter.wait_for_token()
        try:
            ticker = await self.exchange.fetch_ticker(symbol)
            return ticker
        except Exception as e:
            # Don't log errors for invalid symbols - just skip them silently
            error_msg = str(e).lower()
            if "does not have market" in error_msg or "invalid symbol" in error_msg:
                logger.debug(f"Symbol {symbol} not available on exchange, skipping", error=str(e))
            else:
                logger.error(f"Failed to fetch spot ticker for {symbol}", error=str(e))
            raise

    async def get_spot_tickers_bulk(self, symbols: List[str]) -> Dict[str, Dict]:
        """
        Get spot tickers for multiple symbols in one call.
        Returns dict: {symbol: ticker_data}
        Handles invalid symbols gracefully by skipping them.
        """
        await self.public_limiter.wait_for_token()
        results = {}
        chunk_size = 50 
        
        for i in range(0, len(symbols), chunk_size):
            chunk = symbols[i:i + chunk_size]
            try:
                # Wrap bulk fetch in timeout
                tickers = await asyncio.wait_for(
                    self.exchange.fetch_tickers(chunk),
                    timeout=3.0
                )
                results.update(tickers)
            except asyncio.TimeoutError:
                 logger.warning(f"Bulk fetch timed out for chunk {i//chunk_size}")
                 # Proceed to fallback
                 pass 
            except Exception as e:
                # If bulk fetch fails, try individual fetches for the chunk
                logger.warning(f"Bulk fetch failed for chunk {i//chunk_size}", error=str(e))
                pass
            except BaseException as be:
                logger.critical(f"Critical error in bulk fetch chunk {i//chunk_size}", error=str(be))
                # Don't re-raise immediately, try to fallback or continue
                pass

            # Fallback logic if results were not updated (either timeout or exception)
            # Check which symbols are missing from results
            missing = [s for s in chunk if s not in results]
            if missing:
                for symbol in missing:
                    try:
                        # Wrap individual fetch in short timeout
                        ticker = await asyncio.wait_for(
                            self.get_spot_ticker(symbol),
                            timeout=0.5
                        )
                        results[symbol] = ticker
                    except Exception:
                        pass
                    await asyncio.sleep(0) # Yield
        
        return results

    async def get_spot_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        since: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> List[Candle]:
        """
        Fetch OHLCV data from spot market.
        
        Args:
            symbol: Symbol (e.g., "BTC/USD")
            timeframe: Timeframe (e.g., "15m", "1h", "4h", "1d")
            since: Unix timestamp (milliseconds) to fetch from
            limit: Maximum number of candles
        
        Returns:
            List of Candle objects
        """
        await self.public_limiter.wait_for_token()
        
        try:
            # Wrap fetch in timeout to prevent hangs
            ohlcv = await asyncio.wait_for(
                self.exchange.fetch_ohlcv(
                    symbol, timeframe, since=since, limit=limit
                ),
                timeout=10.0
            )
            
            candles = []
            for row in ohlcv:
                timestamp_ms, open_price, high, low, close, volume = row
                timestamp = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
                
                candle = Candle(
                    timestamp=timestamp,
                    symbol=symbol,
                    timeframe=timeframe,
                    open=Decimal(str(open_price)),
                    high=Decimal(str(high)),
                    low=Decimal(str(low)),
                    close=Decimal(str(close)),
                    volume=Decimal(str(volume)),
                )
                candles.append(candle)
            
            logger.debug(
                "Fetched spot OHLCV",
                symbol=symbol,
                timeframe=timeframe,
                count=len(candles),
            )
            return candles
            
        except Exception as e:
            logger.error("Failed to fetch spot OHLCV", symbol=symbol, error=str(e))
            raise
    
    async def get_futures_position(self, symbol: str) -> Optional[Dict]:
        """
        Get current futures position from Kraken Futures API.
        
        Args:
            symbol: Futures symbol (e.g., "BTCUSD-PERP")
        
        Returns:
            Position dict with keys: size, entry_price, liquidation_price, unrealized_pnl
        """
        all_positions = await self.get_all_futures_positions()
        for pos in all_positions:
            if pos['symbol'] == symbol:
                return pos
        return None

    @retry_on_transient_errors(max_retries=3, base_delay=1.0)
    async def get_all_futures_positions(self) -> List[Dict]:
        """
        Get all open futures positions from Kraken Futures API.
        
        Returns:
            List of position dicts
        """
        await self.private_limiter.wait_for_token()
        
        if not self.futures_api_key or not self.futures_api_secret:
            raise ValueError("Futures API credentials not configured")
        
        try:
            url = "https://futures.kraken.com/derivatives/api/v3/openpositions"
            headers = await self._get_futures_auth_headers(url, "GET")
            
            connector = aiohttp.TCPConnector(ssl=self._get_ssl_context())
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                async with session.get(url, headers=headers) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        raise Exception(f"Futures API error: {error_text}")
                    
                    data = await response.json()
                    logger.debug("Raw Positions Response", keys=list(data.keys()), count=len(data.get('openPositions', [])))
                    
                    positions = []
                    for pos in data.get('openPositions', []):
                        positions.append({
                            'symbol': pos.get('symbol'),
                            'size': abs(Decimal(str(pos.get('size', 0)))),  # Always positive
                            'entry_price': Decimal(str(pos.get('price', 0))),
                            'liquidation_price': Decimal(str(pos.get('liquidationPrice', 0))),
                            'unrealized_pnl': Decimal(str(pos.get('unrealizedPnl', 0))),
                            'side': pos.get('side', 'long'),  # Use API's side field directly
                        })
                    
                    return positions
            
        except Exception as e:
            logger.error("Failed to fetch all futures positions", error=str(e))
            raise
    
    async def get_futures_instruments(self) -> List[Dict]:
        """
        Fetch all futures instruments and their specifications.
        Required to get contractSize for conversion.
        """
        await self.public_limiter.wait_for_token()
        try:
            url = "https://futures.kraken.com/derivatives/api/v3/instruments"
            connector = aiohttp.TCPConnector(ssl=self._get_ssl_context())
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        raise Exception(f"Futures API error: {await response.text()}")
                    data = await response.json()
                    return data.get('instruments', [])
        except Exception as e:
            logger.error("Failed to fetch futures instruments", error=str(e))
            raise

    async def get_futures_mark_price(self, symbol: str) -> Decimal:
        """
        Get current mark price from Kraken Futures official feed.
        
        CRITICAL: Mark price MUST be sourced from Kraken Futures mark/index feed,
        not computed from bid/ask. This is the official price used for liquidations.
        
        Args:
            symbol: Futures symbol (e.g., "BTCUSD-PERP", "PI_XBTUSD" for perpetual)
        
        Returns:
            Mark price as Decimal
        """
        await self.public_limiter.wait_for_token()
        
        try:
            url = "https://futures.kraken.com/derivatives/api/v3/tickers"
            
            connector = aiohttp.TCPConnector(ssl=self._get_ssl_context())
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error("Futures API error", status=response.status, error=error_text)
                        raise Exception(f"Futures API error: {error_text}")
                    
                    data = await response.json()
                    
                    # Kraken Futures uses PF_ prefix for perpetuals
                    # and XBT instead of BTC
                    search_symbols = [symbol]
                    if symbol.endswith('-PERP'):
                        base = symbol.replace('-PERP', '').replace('/', '')
                        # Kraken uses XBT for Bitcoin
                        base = base.replace('BTC', 'XBT')
                        search_symbols.append(f"PF_{base}")
                        search_symbols.append(f"PI_{base}")  # Legacy format
                    
                    # Find ticker for this symbol
                    for ticker in data.get('tickers', []):
                        ticker_symbol = ticker.get('symbol')
                        if ticker_symbol in search_symbols:
                            mark_price = ticker.get('markPrice')
                            if mark_price is None:
                                raise ValueError(f"Mark price not available for {symbol}")
                            
                            logger.debug(
                                "Fetched futures mark price",
                                symbol=symbol,
                                ticker_symbol=ticker_symbol,
                                mark_price=mark_price,
                            )
                            return Decimal(str(mark_price))
                    
                    raise ValueError(f"Symbol {symbol} not found in tickers. Searched: {search_symbols}")
            
        except Exception as e:
            logger.error("Failed to fetch futures mark price", symbol=symbol, error=str(e))
            raise

    async def get_futures_tickers_bulk(self) -> Dict[str, Decimal]:
        """
        Get ALL futures mark prices in one call.
        Returns mapped dict: {futures_symbol: mark_price}
        """
        await self.public_limiter.wait_for_token()
        try:
            # Fetch all tickers from API endpoint
            url = "https://futures.kraken.com/derivatives/api/v3/tickers"
            connector = aiohttp.TCPConnector(ssl=self._get_ssl_context())
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        raise Exception(f"Futures API error: {await response.text()}")
                    
                    data = await response.json()
                    
                    # Map results
                    results = {}
                    for ticker in data.get('tickers', []):
                        # Kraken returns weird symbols like 'PF_XBTUSD'
                        # We need to map them back to our 'BTCUSD-PERP' or keep as is and map later.
                        # For now, we store keyed by the raw symbol AND mapped versions if possible.
                        # Better strategy: Return the raw map, let caller handle mapping or map common ones.
                        
                        raw_symbol = ticker.get('symbol')
                        mark = ticker.get('markPrice')
                        if raw_symbol and mark:
                            results[raw_symbol] = Decimal(str(mark))
                            
                    return results
                    
        except Exception as e:
            logger.error("Failed to fetch bulk futures tickers", error=str(e))
            raise
    
    async def get_account_balance(self) -> Dict[str, Decimal]:
        """
        Get account balance (spot).
        
        Returns:
            Dict of currency -> balance
        """
        await self.private_limiter.wait_for_token()
        
        try:
            balance = self.exchange.fetch_balance()
            return {
                currency: Decimal(str(amount))
                for currency, amount in balance['total'].items()
                if amount > 0
            }
            
        except Exception as e:
            logger.error("Failed to fetch account balance", error=str(e))
            raise
    
    async def place_futures_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        size: Decimal,
        price: Optional[Decimal] = None,
        stop_price: Optional[Decimal] = None,
        reduce_only: bool = False,
        leverage: Optional[Decimal] = None,
        client_order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Place order on Kraken Futures using CCXT.
        
        Args:
            symbol: Futures symbol (e.g., "BTC/USD:USD" or "PF_XBTUSD")
            side: "buy" or "sell"
            order_type: "limit", "market", "stop", "take_profit"
            size: Order size in contracts
            price: Limit price (required for limit orders)
            stop_price: Stop price (for stop/take_profit orders)
            reduce_only: Whether order is reduce-only
            client_order_id: Optional client order ID
        
        Returns:
            Dict with order details from exchange
        """
        if not self.futures_exchange:
            raise ValueError("Futures credentials not configured")
            
        try:
            # Create params dict for extra options
            params = {}
            if reduce_only:
                params['reduceOnly'] = True
            if client_order_id:
                params['cliOrdId'] = client_order_id
            if stop_price:
                params['stopPrice'] = float(stop_price)
                
            # Map symbol if needed (ensure CCXT format usually Ticker:Quote)
            # But assume caller sends correct CCXT symbol for now or raw symbol
            # CCXT usually handles 'BTC/USD:USD' style best
            
            # Map order type 'lmt' -> 'limit' for CCXT if passed as raw kraken string
            type_map = {'lmt': 'limit', 'mkt': 'market', 'stp': 'stop'}
            ccxt_type = type_map.get(order_type, order_type)
            
            logger.info(
                "Placing futures order",
                symbol=symbol,
                side=side,
                type=ccxt_type,
                size=str(size),
                leverage=str(leverage) if leverage else "default",
            )
            
            # EXPLICITLY set leverage/margin mode if provided
            # Kraken Futures: Setting leverage implies Isolated Margin. 0 or omitted implies Cross.
            if leverage:
                try:
                    await self.futures_exchange.set_leverage(float(leverage), symbol)
                    logger.debug("Leverage set to isolated", leverage=leverage, symbol=symbol)
                except Exception as lev_err:
                    # Retry with unified symbol if possible
                    try:
                        logger.warning(f"set_leverage failed for {symbol}, trying resolution...", error=str(lev_err))
                        # Load markets if not loaded
                        if not self.futures_exchange.markets:
                            await self.futures_exchange.load_markets()
                        
                        # Try to find market by ID (e.g. PF_MONUSD) or other common formats
                        market = None
                        
                        # 1. Exact match by ID
                        for m in self.futures_exchange.markets.values():
                            if m['id'] == symbol or m['symbol'] == symbol:
                                market = m
                                break
                        
                        # 2. Case-insensitive check
                        if not market:
                             for m in self.futures_exchange.markets.values():
                                if m['id'].upper() == symbol.upper():
                                    market = m
                                    break
                        
                        if market:
                            unified_symbol = market['symbol']
                            logger.info(f"Resolved {symbol} -> {unified_symbol} for leverage setting")
                            await self.futures_exchange.set_leverage(float(leverage), unified_symbol)
                            logger.info("Leverage set successfully with unified symbol", symbol=unified_symbol)
                        else:
                            logger.warning(f"Could not resolve symbol {symbol} for leverage retry")
                            
                    except Exception as retry_err:
                        logger.warning("Failed to set leverage explicitly (retry failed)", error=str(retry_err))
                    
                    # Fallback: hope params['leverage'] works or user setting is already correct
            
            order = await self.futures_exchange.create_order(
                symbol=symbol,
                type=ccxt_type,
                side=side,
                amount=float(size),
                price=float(price) if price else None,
                params=params
            )
            
            logger.info(
                "Futures order placed successfully",
                order_id=order['id'],
                symbol=symbol
            )
            
            return order
            
        except Exception as e:
            logger.error("Futures order placement failed", error=str(e))
            raise Exception(f"Futures API error: {str(e)}")
    
    @retry_on_transient_errors(max_retries=3, base_delay=1.0)
    async def get_futures_balance(self) -> Dict[str, Any]:
        """
        Get futures account balance using CCXT.
        
        Returns:
            Dict containing balance info (free, used, total for each currency)
        """
        if not self.futures_exchange:
            raise ValueError("Futures credentials not configured")
            
        try:
            balance = await self.futures_exchange.fetch_balance()
            logger.debug("Fetched futures balance")
            return balance
        except Exception as e:
            logger.error("Failed to fetch futures balance", error=str(e))
            raise Exception(f"Futures API error: {str(e)}")

    async def get_futures_open_orders(self) -> List[Dict[str, Any]]:
        """
        Get all open futures orders using CCXT.
        
        Returns:
            List of open order dicts
        """
        if not self.futures_exchange:
            raise ValueError("Futures credentials not configured")
            
        try:
            orders = await self.futures_exchange.fetch_open_orders()
            logger.debug("Fetched open futures orders", count=len(orders))
            return orders
        except Exception as e:
            logger.error("Failed to fetch futures open orders", error=str(e))
            raise Exception(f"Futures API error: {str(e)}")
    
    async def cancel_futures_order(self, order_id: str, symbol: Optional[str] = None) -> Dict[str, Any]:
        """
        Cancel a futures order using CCXT.
        
        Args:
            order_id: Order ID to cancel
            symbol: Symbol (optional but recommended for CCXT)
        
        Returns:
            Cancellation response
        """
        if not self.futures_exchange:
            raise ValueError("Futures credentials not configured")
        
        try:
            await self.futures_exchange.cancel_order(order_id, symbol)
            logger.info("Futures order cancelled", order_id=order_id)
            return {"result": "success", "order_id": order_id}
        except Exception as e:
            logger.error("Failed to cancel futures order", order_id=order_id, error=str(e))
            raise Exception(f"Futures API error: {str(e)}")

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Cancel all open futures orders.
        
        Args:
            symbol: Optional symbol to filter cancellations
            
        Returns:
            List of cancellation responses
        """
        if not self.futures_exchange:
            raise ValueError("Futures credentials not configured")
            
        try:
            if symbol:
                # CCXT cancelAllOrders often supports symbol
                await self.futures_exchange.cancel_all_orders(symbol)
                logger.info("All futures orders cancelled", symbol=symbol)
                return [{"result": "success", "symbol": symbol}]
            else:
                # Iterate all open orders if global cancel not supported or to be safe
                open_orders = await self.get_futures_open_orders()
                results = []
                for order in open_orders:
                    try:
                        await self.cancel_futures_order(order['id'], order['symbol'])
                        results.append({"id": order['id'], "status": "cancelled"})
                    except Exception as e:
                        results.append({"id": order['id'], "status": "failed", "error": str(e)})
                return results
                
        except Exception as e:
            logger.error("Failed to cancel all orders", error=str(e))
            raise

    async def close_position(self, symbol: str) -> Dict[str, Any]:
        """
        Close an entire position at market price.
        
        Args:
            symbol: Futures symbol
            
        Returns:
            Order result for the closing trade
        """
        position = await self.get_futures_position(symbol)
        if not position or position['size'] == 0:
            logger.info("No position to close", symbol=symbol)
            return {"status": "no_position"}
            
        # Determine opposite side
        size = position['size'] # Decimal
        current_side = position['side']  # 'long' or 'short'
        close_side = 'sell' if current_side == 'long' else 'buy'
        
        logger.warning(
            "Closing position (Market)",
            symbol=symbol,
            size=str(size),
            side=close_side
        )
        
        # Place reduce-only market order
        return await self.place_futures_order(
            symbol=symbol,
            side=close_side,
            order_type='market',
            size=size,
            reduce_only=True
        )

    async def close(self):
        """Cleanup resources."""
        if self.futures_exchange:
            await self.futures_exchange.close()
        if self.exchange:
            await self.exchange.close()

    def _get_ssl_context(self) -> ssl.SSLContext:
        """
        Get or create reusable SSL context with certifi certificates.
        
        Returns:
            SSL context configured with certifi certificates
        """
        if self._ssl_context is None:
            self._ssl_context = ssl.create_default_context(cafile=certifi.where())
        return self._ssl_context
    
    def _generate_futures_signature(self, path: str, postdata: str, nonce: str) -> str:
        """
        Generate HMAC-SHA512 signature for Kraken Futures API.
        
        Args:
            path: API endpoint path (without /derivatives prefix)
            postdata: POST data string
            nonce: Timestamp nonce
            
        Returns:
            Base64-encoded signature
            
        Raises:
            AuthenticationError: If signature generation fails
        """
        try:
            # Step 1: Concatenate postdata + nonce + path
            message = postdata + nonce + path
            
            # Step 2: SHA-256 hash of the message
            sha256_hash = hashlib.sha256(message.encode('utf-8')).digest()
            
            # Step 3: Base64-decode the API secret
            secret = self.futures_api_secret.strip()
            padding = len(secret) % 4
            if padding != 0:
                secret += '=' * (4 - padding)
                
            secret_decoded = base64.b64decode(secret)
            
            # Step 4: HMAC-SHA-512 using the decoded secret and SHA-256 hash
            signature = hmac.new(
                secret_decoded,
                sha256_hash,
                hashlib.sha512
            ).digest()
            
            # Step 5: Base64-encode the signature
            return base64.b64encode(signature).decode('utf-8')
            
        except Exception as e:
            raise AuthenticationError(f"Failed to generate signature: {e}")
    
    async def _get_futures_auth_headers(self, url: str, method: str, postdata: str = "") -> Dict[str, str]:
        """
        Generate authentication headers for Kraken Futures API.
        
        Args:
            url: Full API endpoint URL
            method: HTTP method (GET, POST)
            postdata: POST data (for POST requests)
        
        Returns:
            Dict of headers including APIKey and Authent
            
        Raises:
            AuthenticationError: If credentials are missing or invalid
        """
        if not self.futures_api_key or not self.futures_api_secret:
            raise AuthenticationError("Futures API credentials not configured")
        
        # Extract path from URL
        path = url.split('.com', 1)[1]
        
        # CRITICAL: Signature uses path WITHOUT /derivatives prefix
        if path.startswith('/derivatives'):
            path = path[len('/derivatives'):]
        
        # Generate nonce (timestamp in milliseconds)
        nonce = str(int(time.time() * 1000))
        
        # Generate signature using extracted method
        authent = self._generate_futures_signature(path, postdata, nonce)
        
        return {
            'APIKey': self.futures_api_key,
            'Authent': authent,
            'Nonce': nonce,
        }


class KrakenWebSocket:
    """
    Kraken WebSocket client for real-time data feeds.
    """
    
    def __init__(
        self,
        endpoint: str,
        on_message: Callable[[Dict], None],
        max_retries: int = 10,
        backoff_seconds: int = 5,
    ):
        """
        Initialize WebSocket client.
        
        Args:
            endpoint: WebSocket endpoint URL
            on_message: Callback for received messages
            max_retries: Maximum reconnection attempts
            backoff_seconds: Base backoff for exponential backoff
        """
        self.endpoint = endpoint
        self.on_message = on_message
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.running = False
        self.retry_count = 0
        
        logger.info("WebSocket client initialized", endpoint=endpoint)
    
    async def connect(self):
        """Connect to WebSocket and start listening."""
        self.running = True
        
        while self.running and self.retry_count < self.max_retries:
            try:
                logger.info(
                    "Connecting to WebSocket",
                    endpoint=self.endpoint,
                    retry=self.retry_count,
                )
                
                async with websockets.connect(self.endpoint) as ws:
                    self.ws = ws
                    self.retry_count = 0  # Reset on successful connection
                    
                    logger.info("WebSocket connected", endpoint=self.endpoint)
                    
                    async for message in ws:
                        try:
                            data = json.loads(message)
                            self.on_message(data)
                        except Exception as e:
                            logger.error(
                                "Error processing WebSocket message",
                                error=str(e),
                                message=message[:200],  # Truncate for logging
                            )
                
            except Exception as e:
                self.retry_count += 1
                backoff = self.backoff_seconds * (2 ** (self.retry_count - 1))
                
                logger.warning(
                    "WebSocket connection failed",
                    endpoint=self.endpoint,
                    retry=self.retry_count,
                    max_retries=self.max_retries,
                    backoff=backoff,
                    error=str(e),
                )
                
                if self.retry_count < self.max_retries:
                    await asyncio.sleep(backoff)
                else:
                    logger.error(
                        "WebSocket max retries exceeded",
                        endpoint=self.endpoint,
                    )
                    break
    
    async def subscribe(self, channels: List[str]):
        """
        Subscribe to WebSocket channels.
        
        Args:
            channels: List of channel names to subscribe to
        """
        if not self.ws:
            raise RuntimeError("WebSocket not connected")
        
        subscription = {
            "event": "subscribe",
            "subscription": {"name": channels},
        }
        
        await self.ws.send(json.dumps(subscription))
        logger.info("Subscribed to channels", channels=channels)
    
    async def disconnect(self):
        """Disconnect WebSocket."""
        self.running = False
        if self.ws:
            await self.ws.close()
            logger.info("WebSocket disconnected", endpoint=self.endpoint)
