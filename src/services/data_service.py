import asyncio
import time
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional
from asyncio import Queue, QueueEmpty

from src.config.config import Config
from src.monitoring.logger import get_logger, setup_logging
from src.data.kraken_client import KrakenClient
from src.ipc.messages import MarketUpdate, ServiceCommand, ServiceStatus
from src.storage.repository import get_candles, save_candle, save_candles_bulk
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
        
        # Start Background Hydration as a Task
        asyncio.create_task(self._perform_background_hydration())
        
        # Start Live Polling Task
        asyncio.create_task(self._perform_live_polling())
        
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
        
        # 1. INITIAL FULL HYDRATION
        scopes_initial = [
            ("15m", 30),
            ("1h", 60), 
            ("4h", 90),
            ("1d", 180)
        ]
        await self._run_hydration_cycle(markets, scopes_initial)
        logger.info("Initial Background Hydration Fully Complete")
        await self._send_status("HYDRATION_COMPLETE", {"msg": "Initial Sync Done"})

        # 2. PERIODIC GAP FILLING (Every 10 minutes, check last 24h)
        while self.active:
            await asyncio.sleep(600) # Wait 10 mins
            logger.info("Starting periodic gap-fill hydration...")
            scopes_periodic = [
                ("15m", 3), # Increased from 1 to 3 days to cover EMA 200 (~2.1 days)
                ("1h", 7),  # Increased from 1 to 7 days for safety
                ("4h", 30)  # Increased from 1 to 30 days
            ]
            await self._run_hydration_cycle(markets, scopes_periodic)

    async def _run_hydration_cycle(self, markets: List[str], scopes: List[tuple]):
        """Internal helper to iterate through a set of scopes and markets."""
        sem = asyncio.Semaphore(5) # Strict concurrency for hydration to favor polling

        async def hydrate_symbol(symbol: str, tf: str, start_date: datetime):
            async with sem:
                if not self.active: return
                try:
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
            
            # Smart Polling: Only fetch 1h every 15 mins, 4h every 60 mins
            fetch_1h = (self.iteration_count % 15 == 0)
            fetch_4h = (self.iteration_count % 60 == 0)

            async def poll_symbol(symbol: str):
                async with sem:
                    if not self.active: return
                    
                    is_bootstrap = symbol not in bootstrapped_symbols
                    # If bootstrap, we MUST fetch everything regardless of cycle
                    do_1h = fetch_1h or is_bootstrap
                    do_4h = fetch_4h or is_bootstrap
                    limit = 300 if is_bootstrap else 3
                    
                    try:
                        # 1. Primary Polling: 15m (Every loop)
                        candles_15m = await self.kraken.get_spot_ohlcv(symbol, "15m", limit=limit)
                        if candles_15m:
                            await asyncio.to_thread(save_candles_bulk, candles_15m)
                            await self.output_queue.put(MarketUpdate(symbol=symbol, candles=candles_15m, timeframe="15m", is_historical=False))
                            if is_bootstrap: bootstrapped_symbols.add(symbol)
                            
                        # 2. Secondary Polling: 1h (Periodic)
                        if do_1h:
                            candles_1h = await self.kraken.get_spot_ohlcv(symbol, "1h", limit=limit)
                            if candles_1h:
                                await asyncio.to_thread(save_candles_bulk, candles_1h)
                                await self.output_queue.put(MarketUpdate(symbol=symbol, candles=candles_1h, timeframe="1h", is_historical=False))
                            else:
                                logger.warning(f"Fetched EMPTY 1h candles for {symbol} (DataService)", limit=limit)

                        # 3. Tertiary Polling: 4h (Periodic)
                        if do_4h:
                            candles_4h = await self.kraken.get_spot_ohlcv(symbol, "4h", limit=limit)
                            if candles_4h:
                                await asyncio.to_thread(save_candles_bulk, candles_4h)
                                await self.output_queue.put(MarketUpdate(symbol=symbol, candles=candles_4h, timeframe="4h", is_historical=False))
                            
                    except asyncio.TimeoutError:
                        # Log as debug to reduce noise unless it persists
                        logger.debug(f"Timeout polling {symbol}")
                    except Exception as e:
                        logger.error(f"Polling failed for {symbol}: {e}")

            # Run all polls and wait
            await asyncio.gather(*[poll_symbol(s) for s in markets], return_exceptions=True)
            self.iteration_count += 1

            elapsed = time.time() - loop_start
            sleep_time = max(5.0, 60.0 - elapsed)
            await asyncio.sleep(sleep_time)

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
