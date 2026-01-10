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
            ohlcv = await self.exchange.fetch_ohlcv(
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
        Get current futures position.
        
        Args:
            symbol: Futures symbol (e.g., "BTCUSD-PERP")
        
        Returns:
            Position dict or None if no position
        """
        await self.private_limiter.wait_for_token()
        
        try:
            # TODO: Implement Kraken Futures API call
            # This requires futures-specific authentication
            logger.warning("Futures position fetching not yet implemented", symbol=symbol)
            return None
            
        except Exception as e:
            logger.error("Failed to fetch futures position", symbol=symbol, error=str(e))
            raise
    
    async def get_futures_mark_price(self, symbol: str) -> Decimal:
        """
        Get current mark price from futures market.
        
        CRITICAL: Mark price MUST be sourced from Kraken Futures mark/index feed,
        not computed from bid/ask.
        
        Args:
            symbol: Futures symbol (e.g., "BTCUSD-PERP")
        
        Returns:
            Mark price as Decimal
        """
        await self.public_limiter.wait_for_token()
        
        try:
            # TODO: Implement Kraken Futures mark price API call
            # Must use official mark/index feed
            logger.warning("Futures mark price fetching not yet implemented", symbol=symbol)
            raise NotImplementedError("Futures mark price API not implemented")
            
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
            balance = await self.exchange.fetch_balance()
            return {
                currency: Decimal(str(amount))
                for currency, amount in balance['total'].items()
                if amount > 0
            }
            
        except Exception as e:
            logger.error("Failed to fetch account balance", error=str(e))
            raise


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
