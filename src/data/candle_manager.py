import asyncio
from datetime import datetime, timezone
from typing import Dict, List, Optional
from collections import defaultdict

from src.monitoring.logger import get_logger
from src.domain.models import Candle
from src.data.kraken_client import KrakenClient
from src.storage.repository import load_candles_map, save_candles_bulk, get_latest_candle_timestamp

logger = get_logger(__name__)

class CandleManager:
    """
    Manages fetching, caching, and persistence of candle data.
    """
    def __init__(self, client: KrakenClient):
        self.client = client
        # Storage: timeframe -> symbol -> list[Candle]
        self.candles: Dict[str, Dict[str, List[Candle]]] = {
            "15m": {},
            "1h": {},
            "4h": {},
            "1d": {}
        }
        # Last update tracking: symbol -> timeframe -> datetime
        self.last_candle_update: Dict[str, Dict[str, datetime]] = {}
        # Persistence queue
        self.pending_candles: List[Candle] = []

    async def initialize(self, markets: List[str]):
        """Bulk load history from database."""
        logger.info("Bulk loading candles from database...", symbol_count=len(markets))

        self.candles["15m"] = await asyncio.to_thread(load_candles_map, markets, "15m", days=14)
        logger.info("Loaded 15m candles")

        self.candles["1h"] = await asyncio.to_thread(load_candles_map, markets, "1h", days=60)
        logger.info("Loaded 1h candles")
        
        self.candles["4h"] = await asyncio.to_thread(load_candles_map, markets, "4h", days=180)
        logger.info("Loaded 4h candles")
        
        self.candles["1d"] = await asyncio.to_thread(load_candles_map, markets, "1d", days=365)
        logger.info("Loaded 1d candles")
        
        # Initialize update trackers
        now = datetime.now(timezone.utc)
        for symbol in markets:
            self.last_candle_update[symbol] = {
                "15m": self.candles["15m"].get(symbol, [])[-1].timestamp if self.candles["15m"].get(symbol) else datetime.min.replace(tzinfo=timezone.utc),
                "1h": self.candles["1h"].get(symbol, [])[-1].timestamp if self.candles["1h"].get(symbol) else datetime.min.replace(tzinfo=timezone.utc),
                "4h": self.candles["4h"].get(symbol, [])[-1].timestamp if self.candles["4h"].get(symbol) else datetime.min.replace(tzinfo=timezone.utc),
                "1d": self.candles["1d"].get(symbol, [])[-1].timestamp if self.candles["1d"].get(symbol) else datetime.min.replace(tzinfo=timezone.utc),
            }

    def get_candles(self, symbol: str, timeframe: str) -> List[Candle]:
        """Get cached candles."""
        return self.candles.get(timeframe, {}).get(symbol, [])

    async def update_candles(self, symbol: str):
        """Update candles for a symbol (Incremental)."""
        now = datetime.now(timezone.utc)
        if symbol not in self.last_candle_update:
            self.last_candle_update[symbol] = {}

        async def fetch_tf(tf: str, interval_min: int):
            # Check cache
            last_update = self.last_candle_update[symbol].get(tf, datetime.min.replace(tzinfo=timezone.utc))
            if (now - last_update).total_seconds() < (interval_min * 60):
                return # Cache hit
            
            buffer = self.candles[tf]
            
            # Determine fetch start time
            # Prioritize in-memory buffer (most recent), then DB
            last_ts = None
            existing = buffer.get(symbol, [])
            
            if existing:
                last_ts = existing[-1].timestamp
            else:
                 # Check DB
                 last_ts = await asyncio.to_thread(get_latest_candle_timestamp, symbol, tf)
                 
            since_ms = None
            if last_ts:
                # Use timestamp (ms) for since
                since_ms = int(last_ts.replace(tzinfo=timezone.utc).timestamp() * 1000)
            
            # Fetch (Incremental)
            candles = await self.client.get_spot_ohlcv(symbol, tf, since=since_ms, limit=300)
            if not candles: return
            
            # Update Cache
            self.last_candle_update[symbol][tf] = now
            
            # Smart Merge into Buffer
            if not existing:
                buffer[symbol] = candles
            else:
                 # Append only new ones
                 last_buffer_ts = existing[-1].timestamp
                 new_candles = [c for c in candles if c.timestamp > last_buffer_ts]
                 if new_candles:
                      buffer[symbol].extend(new_candles)
                 
                 # Prune buffer (Keep 500 in memory)
                 if len(buffer[symbol]) > 500:
                     buffer[symbol] = buffer[symbol][-500:]
            
            # Queue for DB Persistence 
            # We queue all fetched candles. Repository handles deduplication (Upsert).
            if candles:
                self.pending_candles.extend(candles)
            
        await asyncio.gather(
            fetch_tf("15m", 1),
            fetch_tf("1h", 5),
            fetch_tf("4h", 15),
            fetch_tf("1d", 60)
        )

    async def flush_pending(self):
        """Flush pending candles to DB."""
        if not self.pending_candles:
            return

        try:
            # Group by symbol/tf
            grouped = defaultdict(list)
            for candle in self.pending_candles:
                key = (candle.symbol, candle.timeframe)
                grouped[key].append(candle)
            
            save_tasks = []
            for _, candle_group in grouped.items():
                save_tasks.append(asyncio.to_thread(save_candles_bulk, candle_group))
            
            if save_tasks:
                await asyncio.gather(*save_tasks, return_exceptions=True)
            
            total = len(self.pending_candles)
            self.pending_candles.clear()
            logger.debug("Batched save complete", candles_saved=total)
        except Exception as e:
            logger.error("Failed to batch save candles", error=str(e))
            self.pending_candles.clear()
