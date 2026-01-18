import asyncio
import time
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional
from asyncio import Queue, QueueEmpty

from src.config.config import Config
from src.monitoring.logger import get_logger, setup_logging
from src.data.kraken_client import KrakenClient
from src.ipc.messages import MarketUpdate, ServiceCommand, ServiceStatus
from src.storage.repository import get_candles, save_candle, save_candles_bulk, get_latest_candle_timestamp
from src.domain.models import Candle

logger = get_logger("DataService")

class DataService:
    """
    Async Service for Data Ingestion and Hydration.
    Runs as a Task within the main event loop.
    """
    def __init__(self, output_queue: Queue, command_queue: Queue, config: Config):
        self.output_queue = output_queue
        self.command_queue = command_queue
        self.config = config
        self.active = True
        self.iteration_count = 0
        
        # Data Quality Metrics
        self.metrics = {
            'api_calls_total': 0,
            'api_failures': 0,
            'last_update': {},  # symbol -> {tf -> timestamp}
            'candle_counts': {},  # symbol -> {tf -> count}
        }
        
        logger.info("DataService initialized")
        
    async def start(self):
        """Entry point for the async task."""
        logger.info("Data Service Task Starting...")
        try:
            await self._service_loop()
        except asyncio.CancelledError:
             logger.info("Data Service Cancelled")
        except Exception as e:
            logger.critical(f"Data Service Crashed: {e}", exc_info=True)
            
    async def _service_loop(self):
        """Main async loop for Data Service."""
        # Verify DB Connection & Env
        import os
        db_url = os.getenv("DATABASE_URL", "NOT_SET")
        masked = db_url.split("@")[-1] if "@" in db_url else "LOCAL/SQLITE"
        logger.info(f"DataService utilizing DB: {masked}")

        # Initialize resources in this process
        self.kraken = KrakenClient(
            api_key=self.config.exchange.api_key,
            api_secret=self.config.exchange.api_secret,
            futures_api_key=self.config.exchange.futures_api_key,
            futures_api_secret=self.config.exchange.futures_api_secret,
            use_testnet=self.config.exchange.use_testnet
        )
        
        # Report Status
        await self._send_status("RUNNING", {"msg": "Service Initialization Complete"})

        # Initialize Client Lazy
        await self.kraken.initialize()
        
        # Spawn background tasks
        asyncio.create_task(self._perform_background_hydration())
        asyncio.create_task(self._perform_live_polling())
        asyncio.create_task(self._periodic_gap_detection())  # New: Gap detection every 6 hours Task
        
        while self.active:
            # 1. Process Commands
            try:
                while not self.command_queue.empty():
                    try:
                        cmd = self.command_queue.get_nowait()
                        if cmd.command == "STOP":
                            logger.info("Received STOP command")
                            self.active = False
                            break
                        elif cmd.command == "PING":
                            await self._send_status("RUNNING", {"pong": time.time()})
                    except QueueEmpty:
                        break
            except Exception as e:
                logger.error(f"Command processing error: {e}")
            
            if not self.active:
                break
                
            await asyncio.sleep(0.1) # Responsive yield
            
        logger.info("Data Service Shutting Down...")
            
    def _get_active_markets(self) -> List[str]:
        """Resolve list of active markets based on config."""
        if self.config.coin_universe.enabled:
            # Aggregate all tiers
            markets = []
            for tier_list in self.config.coin_universe.liquidity_tiers.values():
                markets.extend(tier_list)
            # Dedup and sort
            return sorted(list(set(markets)))
        else:
            return self.config.exchange.spot_markets

    async def _perform_background_hydration(self):
        """Crawl DB for history and push updates. Runs once for full history, then periodically for gaps."""
        logger.info("Starting background hydration task...")
        markets = self._get_active_markets()
        logger.info(f"Hydrating {len(markets)} markets")
        
        # 1. INITIAL DB PRE-LOAD (Fast startup)
        # Load existing data from database BEFORE hitting API
        logger.info("Phase 1: Loading existing data from database...")
        scopes_initial = [
            ("15m", 30),
            ("1h", 60), 
            ("4h", 90),
            ("1d", 180)
        ]
        
        # Pre-load from DB in parallel
        await self._preload_from_database(markets, scopes_initial)
        logger.info("Database pre-load complete - trading can begin immediately")
        
        # 2. INCREMENTAL API FETCH (Only recent/missing data)
        # Now fetch only what's missing or recent from API
        logger.info("Phase 2: Fetching recent data from API...")
        await self._run_hydration_cycle(markets, scopes_initial, incremental=True)
        logger.info("Initial Background Hydration Fully Complete")
        await self._send_status("HYDRATION_COMPLETE", {"msg": "Initial Sync Done"})

        # 2. PERIODIC GAP FILLING (Every 10 minutes, check last 24h)
        while self.active:
            await asyncio.sleep(600) # Wait 10 mins
            logger.info("Starting periodic gap-fill hydration...")
            scopes_periodic = [
                ("15m", 3), # Increased from 1 to 3 days to cover EMA 200 (~2.1 days)
                ("1h", 7),  # Increased from 1 to 7 days for safety
                ("4h", 30), # Increased from 1 to 30 days
                ("1d", 30)  # Added to ensure Daily bias data is self-healing
            ]
            await self._run_hydration_cycle(markets, scopes_periodic)

    async def _preload_from_database(self, markets: List[str], scopes: List[tuple]):
        """Pre-load existing candle data from database to enable fast startup."""
        sem = asyncio.Semaphore(20)  # Higher concurrency for DB reads (no API limits)
        
        async def load_symbol_data(symbol: str, tf: str, days: int):
            async with sem:
                if not self.active:
                    return
                try:
                    start_date = datetime.now(timezone.utc) - timedelta(days=days)
                    
                    def _fetch():
                        return get_candles(symbol, tf, start_time=start_date)
                    
                    history = await asyncio.to_thread(_fetch)
                    
                    if history:
                        msg = MarketUpdate(
                            symbol=symbol,
                            candles=history,
                            timeframe=tf,
                            is_historical=True
                        )
                        await self.output_queue.put(msg)
                        logger.debug(f"Pre-loaded {len(history)} {tf} candles for {symbol} from DB")
                except Exception as e:
                    logger.debug(f"DB pre-load failed for {symbol} {tf}: {e}")
        
        # Load all symbols/timeframes in parallel
        tasks = []
        for tf, days in scopes:
            for symbol in markets:
                tasks.append(load_symbol_data(symbol, tf, days))
        
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info(f"Database pre-load complete: {len(markets)} symbols × {len(scopes)} timeframes")

    async def _run_hydration_cycle(self, markets: List[str], scopes: List[tuple], incremental: bool = False):
        """Internal helper to iterate through a set of scopes and markets."""
        sem = asyncio.Semaphore(5) # Strict concurrency for hydration to favor polling

        async def hydrate_symbol(symbol: str, tf: str, start_date: datetime):
            async with sem:
                if not self.active: return
                try:
                    # If incremental mode, fetch only recent data from API
                    if incremental:
                        # Get latest timestamp from DB
                        def _get_latest():
                            return get_latest_candle_timestamp(symbol, tf)
                        
                        latest_ts = await asyncio.to_thread(_get_latest)
                        
                        if latest_ts:
                            # Fetch only data since latest timestamp
                            since_ms = int(latest_ts.timestamp() * 1000)
                            candles = await self.kraken.get_spot_ohlcv(symbol, tf, since=since_ms, limit=100)
                            
                            if candles:
                                await asyncio.to_thread(save_candles_bulk, candles)
                                msg = MarketUpdate(
                                    symbol=symbol,
                                    candles=candles,
                                    timeframe=tf,
                                    is_historical=False
                                )
                                await self.output_queue.put(msg)
                                logger.debug(f"Incremental fetch: {len(candles)} new {tf} candles for {symbol}")
                            return
                    
                    # Non-incremental mode: load from DB (legacy behavior)
                    def _fetch():
                        return get_candles(symbol, tf, start_time=start_date)
                    
                    history = await asyncio.to_thread(_fetch)
                    
                    if history:
                        msg = MarketUpdate(
                            symbol=symbol, 
                            candles=history, 
                            timeframe=tf, 
                            is_historical=True
                        )
                        await self.output_queue.put(msg)
                except Exception as e:
                    logger.error(f"Hydration error for {symbol} {tf}: {e}")

        for tf, days in scopes:
            start_date = datetime.now(timezone.utc) - timedelta(days=days)
            tasks = [hydrate_symbol(s, tf, start_date) for s in markets]
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _perform_live_polling(self):
        """Poll API for latest candles."""
        markets = self._get_active_markets()
        logger.info(f"Starting Live Polling for {len(markets)} markets...")
        
        # Track which symbols have been bootstrapped with full history
        bootstrapped_symbols = set()
        
        while self.active:
            loop_start = time.time()
            
            # Parallel Polling for 250 symbols
            # Reduced concurrency to prevent rate limit queue stacking
            sem = asyncio.Semaphore(8) 
            
            # Smart Polling: Only fetch 1h every 15 mins, 4h every 4 hours, 1d every 4 hours
            fetch_1h = (self.iteration_count % 15 == 0)
            fetch_4h = (self.iteration_count % 240 == 0)  # Changed from 60 to 240 (4h candles close every 4h)
            fetch_1d = (self.iteration_count % 240 == 0)

            async def poll_symbol(symbol: str):
                async with sem:
                    if not self.active: return
                    
                    is_bootstrap = symbol not in bootstrapped_symbols
                    # If bootstrap, we MUST fetch everything regardless of cycle
                    do_1h = fetch_1h or is_bootstrap
                    do_4h = fetch_4h or is_bootstrap
                    do_1d = fetch_1d or is_bootstrap
                    limit = 300 if is_bootstrap else 3
                    
                    try:
                        # 1. Primary Polling: 15m (Every loop)
                        self.metrics['api_calls_total'] += 1
                        candles_15m = await self.kraken.get_spot_ohlcv(symbol, "15m", limit=limit)
                        if candles_15m:
                            await asyncio.to_thread(save_candles_bulk, candles_15m)
                            await self.output_queue.put(MarketUpdate(symbol=symbol, candles=candles_15m, timeframe="15m", is_historical=False))
                            if is_bootstrap: bootstrapped_symbols.add(symbol)
                            # Track metrics
                            self._update_metrics(symbol, "15m", len(candles_15m))
                            
                        # 2. Secondary Polling: 1h (Periodic)
                        if do_1h:
                            self.metrics['api_calls_total'] += 1
                            candles_1h = await self.kraken.get_spot_ohlcv(symbol, "1h", limit=limit)
                            if candles_1h:
                                await asyncio.to_thread(save_candles_bulk, candles_1h)
                                await self.output_queue.put(MarketUpdate(symbol=symbol, candles=candles_1h, timeframe="1h", is_historical=False))
                                logger.info(f"DataService: Fetched {len(candles_1h)} 1h candles for {symbol}", is_bootstrap=is_bootstrap)
                                self._update_metrics(symbol, "1h", len(candles_1h))
                            else:
                                logger.warning(f"Fetched EMPTY 1h candles for {symbol} (DataService)", limit=limit)

                        # 3. Tertiary Polling: 4h (Periodic)
                        if do_4h:
                            self.metrics['api_calls_total'] += 1
                            candles_4h = await self.kraken.get_spot_ohlcv(symbol, "4h", limit=limit)
                            if candles_4h:
                                await asyncio.to_thread(save_candles_bulk, candles_4h)
                                await self.output_queue.put(MarketUpdate(symbol=symbol, candles=candles_4h, timeframe="4h", is_historical=False))
                                self._update_metrics(symbol, "4h", len(candles_4h))
                            
                        # 4. Quaternary Polling: 1d (Periodic)
                        if do_1d:
                            self.metrics['api_calls_total'] += 1
                            candles_1d = await self.kraken.get_spot_ohlcv(symbol, "1d", limit=limit)
                            if candles_1d:
                                await asyncio.to_thread(save_candles_bulk, candles_1d)
                                await self.output_queue.put(MarketUpdate(symbol=symbol, candles=candles_1d, timeframe="1d", is_historical=False))
                                self._update_metrics(symbol, "1d", len(candles_1d))
                            
                    except asyncio.TimeoutError:
                        self.metrics['api_failures'] += 1
                        logger.debug(f"Timeout polling {symbol}")
                    except Exception as e:
                        self.metrics['api_failures'] += 1
                        logger.error(f"Polling failed for {symbol}: {e}")

            # Run all polls and wait
            await asyncio.gather(*[poll_symbol(s) for s in markets], return_exceptions=True)
            self.iteration_count += 1
            
            # Log data quality metrics every 60 iterations (~1 hour)
            if self.iteration_count % 60 == 0:
                self._log_data_quality()

            elapsed = time.time() - loop_start
            sleep_time = max(5.0, 60.0 - elapsed)
            await asyncio.sleep(sleep_time)
    
    def _update_metrics(self, symbol: str, tf: str, count: int):
        """Update data quality metrics for a symbol/timeframe."""
        if symbol not in self.metrics['last_update']:
            self.metrics['last_update'][symbol] = {}
            self.metrics['candle_counts'][symbol] = {}
        
        self.metrics['last_update'][symbol][tf] = datetime.now(timezone.utc)
        self.metrics['candle_counts'][symbol][tf] = count
    
    def _log_data_quality(self):
        """Log data quality metrics for monitoring."""
        total_calls = self.metrics['api_calls_total']
        failures = self.metrics['api_failures']
        failure_rate = (failures / total_calls * 100) if total_calls > 0 else 0
        
        logger.info(
            "Data Quality Metrics",
            symbols_tracked=len(self.metrics['last_update'])
        )
    
    def _find_gaps(self, candles: List[Candle], tf: str) -> List[tuple]:
        """
        Detect gaps in candle data.
        
        Returns:
            List of (gap_start, gap_end) tuples
        """
        if len(candles) < 2:
            return []
        
        # Calculate expected interval in seconds
        intervals = {
            "15m": 900,
            "1h": 3600,
            "4h": 14400,
            "1d": 86400
        }
        expected_interval = intervals.get(tf, 3600)
        
        gaps = []
        for i in range(len(candles) - 1):
            current = candles[i]
            next_candle = candles[i + 1]
            
            actual_gap = (next_candle.timestamp - current.timestamp).total_seconds()
            
            # If gap is more than 2x expected interval, it's a missing candle
            if actual_gap > expected_interval * 2:
                gaps.append((current.timestamp, next_candle.timestamp))
        
        return gaps
    
    async def _detect_and_fill_gaps(self):
        """Detect gaps in candle data and backfill automatically."""
        markets = self._get_active_markets()
        
        for symbol in markets[:10]:  # Limit to 10 symbols per check to avoid overload
            for tf in ["15m", "1h", "4h", "1d"]:
                try:
                    # Get recent candles from DB
                    def _fetch():
                        return get_candles(symbol, tf, limit=100)
                    
                    candles = await asyncio.to_thread(_fetch)
                    
                    if not candles:
                        continue
                    
                    gaps = self._find_gaps(candles, tf)
                    
                    for gap_start, gap_end in gaps:
                        logger.warning(
                            f"Gap detected: {symbol} {tf}",
                            gap_start=gap_start,
                            gap_end=gap_end,
                            gap_hours=(gap_end - gap_start).total_seconds() / 3600
                        )
                        
                        # Fetch missing data
                        since_ms = int(gap_start.timestamp() * 1000)
                        missing = await self.kraken.get_spot_ohlcv(
                            symbol, tf, since=since_ms, limit=100
                        )
                        
                        if missing:
                            await asyncio.to_thread(save_candles_bulk, missing)
                            logger.info(f"Gap filled: {symbol} {tf}, fetched {len(missing)} candles")
                
                except Exception as e:
                    logger.debug(f"Gap detection failed for {symbol} {tf}: {e}")
    
    async def _periodic_gap_detection(self):
        """Run gap detection every 6 hours."""
        # Wait for initial hydration to complete
        await asyncio.sleep(300)  # 5 minutes
        
        while self.active:
            logger.info("Starting periodic gap detection...")
            await self._detect_and_fill_gaps()
            logger.info("Gap detection complete")
            
            # Run every 6 hours
            await asyncio.sleep(21600)

    async def _send_status(self, status: str, details: Dict = None):
        msg = ServiceStatus(
            service_name="DataService",
            status=status,
            timestamp=datetime.now(timezone.utc),
            details=details
        )
        # Status messages should skip the queue if full, or wait?
        # Better to wait to ensure observability, or drop if critical.
        # Let's wait, as status is infrequent.
        await self.output_queue.put(msg)
