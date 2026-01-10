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
import hashlib
import hmac
import base64
import time
import asyncio
import json
import websockets
import aiohttp
from typing import Dict, List, Optional, Callable, Any
from datetime import datetime, timezone
from decimal import Decimal
from collections import deque
from dataclasses import dataclass
from src.monitoring.logger import get_logger
from src.domain.models import Candle

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
        
        # Initialize CCXT exchange
        self.exchange = ccxt.kraken({
            'apiKey': api_key,
            'secret': api_secret,
            'enableRateLimit': True,
        })
        
        # Rate limiters (configurable per endpoint group)
        self.public_limiter = RateLimiter(capacity=20, refill_rate=1.0)  # 1 req/sec
        self.private_limiter = RateLimiter(capacity=20, refill_rate=0.33)  # ~20 per minute
        
        logger.info("Kraken client initialized")
    
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
            ohlcv = self.exchange.fetch_ohlcv(
                symbol, timeframe, since=since, limit=limit
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
        await self.private_limiter.wait_for_token()
        
        if not self.futures_api_key or not self.futures_api_secret:
            raise ValueError("Futures API credentials not configured")
        
        try:
            url = "https://futures.kraken.com/derivatives/api/v3/openpositions"
            headers = await self._get_futures_auth_headers(url, "GET")
            
            import ssl
            ssl_context = ssl.SSLContext()
            ssl_context.verify_mode = ssl.CERT_NONE
            connector = aiohttp.TCPConnector(ssl=ssl_context)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(url, headers=headers) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error("Futures API error", status=response.status, error=error_text)
                        raise Exception(f"Futures API error: {error_text}")
                    
                    data = await response.json()
                    
                    # Find position for this symbol
                    for position in data.get('openPositions', []):
                        if position.get('symbol') == symbol:
                            return {
                                'size': Decimal(str(position.get('size', 0))),
                                'entry_price': Decimal(str(position.get('price', 0))),
                                'liquidation_price': Decimal(str(position.get('liquidationPrice', 0))),
                                'unrealized_pnl': Decimal(str(position.get('unrealizedPnl', 0))),
                                'side': 'long' if float(position.get('size', 0)) > 0 else 'short',
                            }
                    
                    # No position found
                    return None
            
        except Exception as e:
            logger.error("Failed to fetch futures position", symbol=symbol, error=str(e))
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
            # Kraken Futures public tickers endpoint
            url = "https://futures.kraken.com/derivatives/api/v3/tickers"
            
            import ssl
            ssl_context = ssl.SSLContext()
            ssl_context.verify_mode = ssl.CERT_NONE
            connector = aiohttp.TCPConnector(ssl=ssl_context)
            async with aiohttp.ClientSession(connector=connector) as session:
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
    
    async def _get_futures_auth_headers(self, url: str, method: str, postdata: str = "") -> Dict[str, str]:
        """
        Generate authentication headers for Kraken Futures API.
        
        Args:
            url: Full API endpoint URL
            method: HTTP method (GET, POST)
            postdata: POST data (for POST requests)
        
        Returns:
            Dict of headers including APIKey and Authent
        """
        # Extract path from URL
        path = url.split('.com', 1)[1]
        
        # Generate nonce (timestamp in milliseconds)
        nonce = str(int(time.time() * 1000))
        
        # Create signature
        # authent = sha256(postdata + nonce + path)
        message = postdata + nonce + path
        secret_decoded = base64.b64decode(self.futures_api_secret)
        signature = hmac.new(
            secret_decoded,
            message.encode('utf-8'),
            hashlib.sha256
        ).digest()
        authent = base64.b64encode(signature).decode('utf-8')
        
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
