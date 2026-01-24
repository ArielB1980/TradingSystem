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
        logger.info("Hydrating candle cache from database...", symbol_count=len(markets))

        # Merge results into existing cache instead of replacing it
        # This prevents data loss if initialize is called while the system is already running
        res_15m = await asyncio.to_thread(load_candles_map, markets, "15m", days=14)
        for s, cands in res_15m.items():
            if s not in self.candles["15m"]: self.candles["15m"][s] = cands
            else: self._merge_candles(s, "15m", cands)

        res_1h = await asyncio.to_thread(load_candles_map, markets, "1h", days=60)
        for s, cands in res_1h.items():
            if s not in self.candles["1h"]: self.candles["1h"][s] = cands
            else: self._merge_candles(s, "1h", cands)
        
        res_4h = await asyncio.to_thread(load_candles_map, markets, "4h", days=180)
        for s, cands in res_4h.items():
            if s not in self.candles["4h"]: self.candles["4h"][s] = cands
            else: self._merge_candles(s, "4h", cands)
        
        res_1d = await asyncio.to_thread(load_candles_map, markets, "1d", days=365)
        for s, cands in res_1d.items():
            if s not in self.candles["1d"]: self.candles["1d"][s] = cands
            else: self._merge_candles(s, "1d", cands)
        
        # Initialize update trackers
        now = datetime.now(timezone.utc)
        for symbol in markets:
            self.last_candle_update[symbol] = {
                "15m": self.candles["15m"].get(symbol, [])[-1].timestamp if self.candles["15m"].get(symbol) else datetime.min.replace(tzinfo=timezone.utc),
                "1h": self.candles["1h"].get(symbol, [])[-1].timestamp if self.candles["1h"].get(symbol) else datetime.min.replace(tzinfo=timezone.utc),
                "4h": self.candles["4h"].get(symbol, [])[-1].timestamp if self.candles["4h"].get(symbol) else datetime.min.replace(tzinfo=timezone.utc),
                "1d": self.candles["1d"].get(symbol, [])[-1].timestamp if self.candles["1d"].get(symbol) else datetime.min.replace(tzinfo=timezone.utc),
            }

        # Hydration summary: helps explain "only N coins with sufficient candles"
        sufficient_15m = sum(1 for s in markets if len(self.candles["15m"].get(s, [])) >= 50)
        zero_15m = sum(1 for s in markets if len(self.candles["15m"].get(s, [])) == 0)
        logger.info(
            "Hydration complete",
            total=len(markets),
            with_sufficient_15m=sufficient_15m,
            with_zero_15m=zero_15m,
            hint="Run backfill against this DB if most have zero; ensure universe matches live discovery.",
        )

    def _merge_candles(self, symbol: str, timeframe: str, new_candles: List[Candle]):
        """Helper to merge candles into cache, avoiding duplicates."""
        buffer = self.candles[timeframe]
        if symbol not in buffer:
            buffer[symbol] = new_candles
            return
            
        existing = buffer[symbol]
        if not existing:
            buffer[symbol] = new_candles
            return
            
        last_ts = existing[-1].timestamp
        to_append = [c for c in new_candles if c.timestamp > last_ts]
        if to_append:
            existing.extend(to_append)
        
        # Prune if over limit (2,000 for HTF context)
        if len(existing) > 2000:
            buffer[symbol] = existing[-2000:]

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
            if not candles:
                logger.warning(f"Fetched EMPTY candles for {symbol} {tf}", since_ms=since_ms, limit=300)
                return
            
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
                 
                 # Prune buffer (Keep up to 2,000 in memory for HTF analysis)
                 if len(buffer[symbol]) > 2000:
                     buffer[symbol] = buffer[symbol][-2000:]
            
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

        # Snapshot the batch to avoid clearing new data if this batch fails
        batch = list(self.pending_candles)
        
        try:
            # Group by symbol/tf for efficient upsert
            grouped = defaultdict(list)
            for candle in batch:
                key = (candle.symbol, candle.timeframe)
                grouped[key].append(candle)
            
            save_tasks = []
            for _, candle_group in grouped.items():
                save_tasks.append(asyncio.to_thread(save_candles_bulk, candle_group))
            
            if save_tasks:
                results = await asyncio.gather(*save_tasks, return_exceptions=True)
                
                # Check for individual task failures
                failures = [r for r in results if isinstance(r, Exception)]
                if failures:
                    logger.error("Some candle batches failed to save", count=len(failures), error=str(failures[0]))
                    # If any batch fails, we don't clear the queue to prevent data loss.
                    # Duplicates are handled by DB-level Upsert.
                    return 

            # Only clear the items we successfully attempted to save
            # This is safer than clear() if new items were added during the await
            processed_ids = {(c.symbol, c.timeframe, c.timestamp) for c in batch}
            self.pending_candles = [c for c in self.pending_candles if (c.symbol, c.timeframe, c.timestamp) not in processed_ids]
            
            if batch:
                logger.debug("Batched save complete", candles_saved=len(batch))
        except Exception as e:
            logger.error("Failed to batch save candles", error=str(e))
            # Keep pending_candles for next attempt
