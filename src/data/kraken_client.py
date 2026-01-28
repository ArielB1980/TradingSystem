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


def _extract_venue_error(exc: Exception) -> tuple:
    """Extract venue error code and message from Kraken/ccxt exception. Returns (code, message)."""
    code, msg = "UNKNOWN", str(exc)
    try:
        import json
        if hasattr(exc, "response") and exc.response is not None:
            r = getattr(exc, "response", {}) or {}
            errs = r.get("errors") or r.get("error") or []
            if isinstance(errs, list) and errs and isinstance(errs[0], dict):
                code = str(errs[0].get("code", code))
                msg = str(errs[0].get("message", msg))
                return (code, msg)
        s = str(exc)
        if "{" in s and "errors" in s.lower():
            start, end = s.find("{"), s.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(s[start:end])
                errs = data.get("errors") or []
                if errs and isinstance(errs[0], dict):
                    code = str(errs[0].get("code", code))
                    msg = str(errs[0].get("message", msg))
    except Exception:
        pass
    return (code, msg)


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
        *,
        market_cache_minutes: int = 60,
    ):
        """
        Initialize Kraken client.

        Args:
            api_key: Kraken spot API key
            api_secret: Kraken spot API secret
            futures_api_key: Kraken Futures API key (optional)
            futures_api_secret: Kraken Futures API secret (optional)
            use_testnet: Use testnet
            market_cache_minutes: TTL for get_spot_markets/get_futures_markets cache (default 60)
        """
        self.api_key = api_key
        self.api_secret = api_secret
        self.futures_api_key = futures_api_key
        self.futures_api_secret = futures_api_secret
        self.use_testnet = use_testnet
        self._market_cache_minutes = max(1, market_cache_minutes)

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

        # Markets cache for get_spot_markets / get_futures_markets (avoids spamming load_markets)
        self._markets_cache: Dict[str, tuple] = {}  # "spot" -> (ts, data), "futures" -> (ts, data)
        self._markets_lock = asyncio.Lock()

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

    async def get_spot_markets(self) -> Dict[str, dict]:
        """
        Fetch Kraken spot markets (USD-quoted, active). Cached for market_cache_minutes.
        Callers must use this instead of accessing spot_exchange.
        """
        if not self.exchange:
            await self.initialize()
        async with self._markets_lock:
            now = time.time()
            if "spot" in self._markets_cache:
                ts, data = self._markets_cache["spot"]
                if (now - ts) < self._market_cache_minutes * 60:
                    return data
            try:
                await self.exchange.load_markets()
                usd_markets = {}
                for m in self.exchange.markets.values():
                    if m.get("quote") == "USD" and m.get("active", True):
                        sym = m.get("symbol", "")
                        if sym:
                            usd_markets[sym] = {
                                "id": m.get("id"),
                                "base": m.get("base"),
                                "quote": m.get("quote"),
                                "active": m.get("active", True),
                            }
                self._markets_cache["spot"] = (now, usd_markets)
                return usd_markets
            except Exception as e:
                logger.error("Failed to fetch spot markets", error=str(e))
                raise

    async def get_futures_markets(self) -> Dict[str, dict]:
        """
        Fetch Kraken futures perpetuals (swap, active). Cached for market_cache_minutes.
        Returns Dict[base_quote, info] e.g. "BTC/USD" -> {symbol, base, quote, active}.
        """
        if not self.futures_exchange:
            await self.initialize()
        if not self.futures_exchange:
            return {}
        async with self._markets_lock:
            now = time.time()
            if "futures" in self._markets_cache:
                ts, data = self._markets_cache["futures"]
                if (now - ts) < self._market_cache_minutes * 60:
                    return data
            try:
                await self.futures_exchange.load_markets()
                perps = {}
                for m in self.futures_exchange.markets.values():
                    if m.get("type") == "swap" and m.get("active", True):
                        symbol = m.get("symbol", "")
                        if ":" in symbol:
                            base_quote = symbol.split(":")[0]
                        else:
                            base_quote = symbol
                        if base_quote:
                            perps[base_quote] = {
                                "id": m.get("id"),
                                "symbol": symbol,
                                "base": m.get("base"),
                                "quote": m.get("quote"),
                                "active": m.get("active", True),
                            }
                self._markets_cache["futures"] = (now, perps)
                return perps
            except Exception as e:
                logger.error("Failed to fetch futures markets", error=str(e))
                raise

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
                 logger.debug(f"Bulk fetch timed out for chunk {i//chunk_size}, falling back to individual")
                 pass 
            except Exception as e:
                # If bulk fetch fails, it might be due to ONE bad symbol in the chunk
                logger.debug(f"Bulk fetch failed for chunk {i//chunk_size}, falling back to individual", error=str(e))
                pass

            # Fallback logic: check which symbols are missing from results and fetch individually
            # This handles both timeouts and partial failures (invalid symbols)
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
                    except Exception as e:
                        # Log as debug to avoid spamming warnings for unsupported coins
                        logger.debug(f"Individual fetch failed for {symbol}", error=str(e))
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
            err_detail = str(e)
            err_type = type(e).__name__
            resp = getattr(e, "response", None)
            if resp is not None and hasattr(resp, "text"):
                err_detail = f"{err_detail}; response={resp.text[:200]!r}"
            logger.error(
                "Failed to fetch spot OHLCV",
                symbol=symbol,
                error=err_detail,
                error_type=err_type,
            )
            raise
    
    async def get_futures_ohlcv(
        self,
        futures_symbol: str,
        timeframe: str,
        since: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> List[Candle]:
        """
        Fetch OHLCV from Kraken Futures. Used when spot OHLCV is unavailable.

        Args:
            futures_symbol: Kraken futures symbol (e.g. PF_XBTUSD)
            timeframe: 15m, 1h, 4h, 1d
            since: Unix ms (optional)
            limit: Max candles (default 300)

        Returns:
            List of Candle (symbol set to futures_symbol; caller may overwrite for storage).
        """
        if not self.futures_exchange:
            logger.warning("Futures exchange not configured; cannot fetch futures OHLCV")
            return []
        limit = limit or 300
        await self.public_limiter.wait_for_token()
        try:
            if not self.futures_exchange.markets:
                await self.futures_exchange.load_markets()
            unified = futures_symbol
            for m in self.futures_exchange.markets.values():
                if m.get("id") and str(m["id"]).upper() == str(futures_symbol).upper():
                    unified = m["symbol"]
                    break
                if m.get("symbol") == futures_symbol:
                    break
            ohlcv = await asyncio.wait_for(
                self.futures_exchange.fetch_ohlcv(unified, timeframe, since=since, limit=limit),
                timeout=10.0,
            )
            candles = []
            for row in ohlcv:
                ts_ms, o, h, l, c, v = row
                ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
                candles.append(Candle(
                    timestamp=ts,
                    symbol=futures_symbol,
                    timeframe=timeframe,
                    open=Decimal(str(o)),
                    high=Decimal(str(h)),
                    low=Decimal(str(l)),
                    close=Decimal(str(c)),
                    volume=Decimal(str(v)),
                ))
            logger.debug("Fetched futures OHLCV", symbol=futures_symbol, timeframe=timeframe, count=len(candles))
            return candles
        except Exception as e:
            logger.debug("Futures OHLCV fetch failed", symbol=futures_symbol, timeframe=timeframe, error=str(e))
            return []

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
        Returns dict keyed by multiple formats for each ticker:
        - Original raw symbol (e.g., "PI_THETAUSD")
        - PF_{BASE}USD format (e.g., "PF_THETAUSD")
        - CCXT unified format (e.g., "THETA/USD:USD")
        - BASE/USD format (e.g., "THETA/USD")
        
        This ensures lookup works regardless of format used.
        """
        await self.public_limiter.wait_for_token()
        try:
            url = "https://futures.kraken.com/derivatives/api/v3/tickers"
            connector = aiohttp.TCPConnector(ssl=self._get_ssl_context())
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        raise Exception(f"Futures API error: {await response.text()}")
                    data = await response.json()

            results: Dict[str, Decimal] = {}
            
            def derive_base(symbol: str) -> Optional[str]:
                """Derive base currency from symbol (e.g., PI_THETAUSD -> THETA)."""
                # Strip prefixes: PI_, PF_, FI_
                base = symbol.upper()
                for prefix in ["PI_", "PF_", "FI_"]:
                    if base.startswith(prefix):
                        base = base[len(prefix):]
                        break
                # Strip trailing USD
                if base.endswith("USD"):
                    base = base[:-3]
                # Handle XBT -> BTC
                if base == "XBT":
                    base = "BTC"
                return base if base else None
            
            for ticker in data.get("tickers", []):
                raw_symbol = ticker.get("symbol")
                mark = ticker.get("markPrice")
                if not raw_symbol or not mark:
                    continue
                
                mark_decimal = Decimal(str(mark))
                
                # Store original raw key
                results[raw_symbol] = mark_decimal
                
                # Derive base and create normalized keys
                base = derive_base(raw_symbol)
                if base:
                    # Add PF_{BASE}USD format
                    pf_key = f"PF_{base}USD"
                    if pf_key not in results:
                        results[pf_key] = mark_decimal
                    
                    # Add {BASE}/USD:USD (CCXT unified)
                    ccxt_unified = f"{base}/USD:USD"
                    if ccxt_unified not in results:
                        results[ccxt_unified] = mark_decimal
                    
                    # Add {BASE}/USD (helper format)
                    base_usd = f"{base}/USD"
                    if base_usd not in results:
                        results[base_usd] = mark_decimal

            # Also add CCXT unified keys from exchange markets (for any we might have missed)
            if self.futures_exchange:
                try:
                    if not self.futures_exchange.markets:
                        await self.futures_exchange.load_markets()
                    for raw, val in list(results.items()):
                        raw_upper = str(raw).upper()
                        for m in self.futures_exchange.markets.values():
                            mid = m.get("id")
                            if not mid:
                                continue
                            if str(mid).upper() == raw_upper:
                                unified = m.get("symbol")
                                if unified and unified not in results:
                                    results[unified] = val
                                break
                except Exception as e:
                    logger.debug("Could not add CCXT keys to bulk futures tickers", error=str(e))

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
                
            # Map order type 'lmt' -> 'limit' for CCXT if passed as raw kraken string
            type_map = {'lmt': 'limit', 'mkt': 'market', 'stp': 'stop'}
            ccxt_type = type_map.get(order_type, order_type)
            
            # CRITICAL: Resolve unified symbol format ONCE for all API calls
            # Kraken Futures uses symbols like PF_XBTUSD but CCXT needs unified format
            unified_symbol = symbol
            if not self.futures_exchange.markets:
                await self.futures_exchange.load_markets()
            
            # Find the unified symbol from market ID
            for m in self.futures_exchange.markets.values():
                if m['id'] == symbol or m['id'].upper() == symbol.upper():
                    unified_symbol = m['symbol']
                    break
                elif m['symbol'] == symbol:
                    unified_symbol = symbol  # Already unified
                    break
            
            if stop_price:
                # Round stop price to precision using CCXT
                rounded_stop = self.futures_exchange.price_to_precision(unified_symbol, float(stop_price))
                params['stopPrice'] = float(rounded_stop)
            
            logger.info(
                "Placing futures order",
                symbol=symbol,
                unified_symbol=unified_symbol,
                side=side,
                type=ccxt_type,
                size=str(size),
                leverage=str(leverage) if leverage else "default",
            )
            
            # Set leverage only when explicitly requested and not reduce-only.
            # When leverage is None (e.g. unknown spec), skip set_leverage and use venue default.
            leverage_set_success = False
            if leverage is not None and leverage and not reduce_only:
                try:
                    await self.futures_exchange.set_leverage(float(leverage), unified_symbol)
                    logger.info("Leverage set successfully", leverage=float(leverage), symbol=unified_symbol)
                    leverage_set_success = True
                except Exception as lev_err:
                    error_str = str(lev_err).lower()
                    if "already" in error_str or "same" in error_str or "no change" in error_str:
                        logger.info("Leverage already set to target value", leverage=float(leverage), symbol=unified_symbol)
                        leverage_set_success = True
                    else:
                        venue_code, venue_msg = _extract_venue_error(lev_err)
                        logger.error(
                            "ORDER_REJECTED_BY_VENUE",
                            symbol=unified_symbol,
                            venue_error_code=venue_code,
                            venue_error_message=venue_msg,
                            payload_summary={"side": side, "type": ccxt_type, "amount": float(size), "leverage": float(leverage)},
                        )
                        raise Exception(f"Leverage setting failed for {unified_symbol}: {lev_err}. Order rejected for safety.")
            else:
                leverage_set_success = True
            
            if leverage is not None and leverage and not reduce_only:
                params['leverage'] = float(leverage)
            
            try:
                order = await self.futures_exchange.create_order(
                    symbol=unified_symbol,
                    type=ccxt_type,
                    side=side,
                    amount=float(size),
                    price=float(price) if price else None,
                    params=params
                )
            except Exception as order_err:
                venue_code, venue_msg = _extract_venue_error(order_err)
                logger.error(
                    "ORDER_REJECTED_BY_VENUE",
                    symbol=unified_symbol,
                    venue_error_code=venue_code,
                    venue_error_message=venue_msg,
                    payload_summary={"side": side, "type": ccxt_type, "amount": float(size)},
                )
                raise
            
            logger.info(
                "Futures order placed successfully",
                order_id=order['id'],
                symbol=unified_symbol,
                leverage=float(leverage) if leverage else "default",
                leverage_confirmed=leverage_set_success
            )
            
            return order
            
        except Exception as e:
            logger.error("Futures order placement failed", error=str(e))
            raise Exception(f"Futures API error: {str(e)}")

    async def create_order(
        self,
        symbol: str,
        type: str,
        side: str,
        amount: float,
        price: Optional[float] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        CCXT-style create_order for ExecutionGateway compatibility.
        Delegates to place_futures_order.
        """
        p = params or {}
        client_order_id = p.get("clientOrderId") or p.get("cliOrdId")
        reduce_only = bool(p.get("reduceOnly", False))
        stop_price = p.get("stopPrice")
        if stop_price is not None:
            stop_price = Decimal(str(stop_price))
        elif type in ("stop", "stop_loss") and price is not None:
            stop_price = Decimal(str(price))
        leverage = Decimal("7") if not reduce_only else None
        order_type = "stop" if type in ("stop", "stop_loss") else type
        size = Decimal(str(amount))
        price_dec = Decimal(str(price)) if price is not None else None
        return await self.place_futures_order(
            symbol=symbol,
            side=side,
            order_type=order_type,
            size=size,
            price=price_dec,
            stop_price=stop_price,
            reduce_only=reduce_only,
            leverage=leverage,
            client_order_id=client_order_id,
        )

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
        # CRITICAL: Runtime assertion - detect mocks in production
        import sys
        import os
        from unittest.mock import Mock, MagicMock
        
        is_test = (
            "pytest" in sys.modules or
            "PYTEST_CURRENT_TEST" in os.environ or
            any("test" in path.lower() for path in sys.path if isinstance(path, str))
        )
        
        if not is_test:
            # Verify futures_exchange is not a mock
            if isinstance(self.futures_exchange, Mock) or isinstance(self.futures_exchange, MagicMock):
                logger.critical("CRITICAL: futures_exchange is a Mock/MagicMock in production!")
                raise RuntimeError(
                    "CRITICAL: futures_exchange is a Mock/MagicMock. "
                    "This should never happen in production. Check for test code leaking into runtime."
                )
            
            # Verify fetch_open_orders is callable and not a mock
            if hasattr(self.futures_exchange, 'fetch_open_orders'):
                fetch_fn = getattr(self.futures_exchange, 'fetch_open_orders')
                if isinstance(fetch_fn, Mock) or isinstance(fetch_fn, MagicMock):
                    logger.critical("CRITICAL: fetch_open_orders is a Mock/MagicMock in production!")
                    raise RuntimeError(
                        "CRITICAL: fetch_open_orders is a Mock/MagicMock. "
                        "This should never happen in production. Check for test code leaking into runtime."
                    )
        
        if not self.futures_exchange:
            raise ValueError("Futures credentials not configured")
            
        try:
            orders = await self.futures_exchange.fetch_open_orders()
            logger.debug("Fetched open futures orders", count=len(orders))
            return orders
        except Exception as e:
            logger.error("Failed to fetch futures open orders", error=str(e))
            raise Exception(f"Futures API error: {str(e)}")

    async def fetch_order(self, order_id: str, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Fetch a single order by id (open or closed).
        Returns dict with id, status, filled, remaining, average, trades, clientOrderId
        for process_order_update, or None if not found / error.
        """
        if not self.futures_exchange:
            return None
        try:
            raw = await self.futures_exchange.fetch_order(order_id, symbol)
            if not raw:
                return None
            info = raw.get("info") or {}
            return {
                "id": raw.get("id"),
                "status": (raw.get("status") or "").lower(),
                "filled": raw.get("filled", 0),
                "remaining": raw.get("remaining", 0),
                "average": raw.get("average") or raw.get("price") or 0,
                "trades": raw.get("trades") or [],
                "clientOrderId": raw.get("clientOrderId") or info.get("cliOrdId") or info.get("clientOrderId"),
            }
        except Exception as e:
            logger.debug("fetch_order failed", order_id=order_id, symbol=symbol, error=str(e))
            return None

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

    async def cancel_order(self, order_id: str, symbol: Optional[str] = None) -> Dict[str, Any]:
        """CCXT-style cancel_order for ExecutionGateway. Delegates to cancel_futures_order."""
        return await self.cancel_futures_order(order_id, symbol)

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
