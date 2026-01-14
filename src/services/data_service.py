import multiprocessing
import asyncio
import time
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional
from queue import Empty

from src.config.config import Config
from src.monitoring.logger import get_logger, setup_logging
from src.data.kraken_client import KrakenClient
from src.ipc.messages import MarketUpdate, ServiceCommand, ServiceStatus
from src.storage.repository import get_candles, save_candle
from src.domain.models import Candle

logger = get_logger("DataService")

class DataService(multiprocessing.Process):
    """
    Dedicated process for Data Ingestion and Hydration.
    Runs an asyncio loop isolated from the Trading Engine.
    """
    def __init__(self, output_queue: multiprocessing.Queue, command_queue: multiprocessing.Queue, config: Config):
        super().__init__()
        self.output_queue = output_queue
        self.command_queue = command_queue
        self.config = config
        self.active = True
        
    def run(self):
        """Entry point for the separate process."""
        setup_logging()
        logger.info("Data Service Process Started (PID %s)", self.pid)
        
        try:
            asyncio.run(self._service_loop())
        except KeyboardInterrupt:
            logger.info("Data Service stopped by User")
        except Exception as e:
            logger.critical(f"Data Service Crashed: {e}", exc_info=True)
            
    async def _service_loop(self):
        """Main async loop for Data Service."""
        # Initialize resources in this process
        self.kraken = KrakenClient(
            api_key=self.config.exchange.api_key,
            api_secret=self.config.exchange.api_secret,
            futures_api_key=self.config.exchange.futures_api_key,
            futures_api_secret=self.config.exchange.futures_api_secret,
            use_testnet=self.config.exchange.use_testnet
        )
        
        # Report Status
        self._send_status("RUNNING", {"msg": "Service Initialization Complete"})
        
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
                            self._send_status("RUNNING", {"pong": time.time()})
                    except Empty:
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
        self._send_status("HYDRATION_COMPLETE", {"msg": "Initial Sync Done"})

        # 2. PERIODIC GAP FILLING (Every 10 minutes, check last 24h)
        while self.active:
            await asyncio.sleep(600) # Wait 10 mins
            logger.info("Starting periodic gap-fill hydration...")
            scopes_periodic = [
                ("15m", 1),
                ("1h", 1),
                ("4h", 1)
            ]
            await self._run_hydration_cycle(markets, scopes_periodic)

    async def _run_hydration_cycle(self, markets: List[str], scopes: List[tuple]):
        """Internal helper to iterate through a set of scopes and markets."""
        for tf, days in scopes:
            start_date = datetime.now(timezone.utc) - timedelta(days=days)
            
            for symbol in markets:
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
                        self.output_queue.put(msg)
                        
                except Exception as e:
                    logger.error(f"Hydration error for {symbol}: {e}")
                
                await asyncio.sleep(0.01)

    async def _perform_live_polling(self):
        """Poll API for latest candles."""
        markets = self._get_active_markets()
        logger.info(f"Starting Live Polling for {len(markets)} markets...")
        
        while self.active:
            loop_start = time.time()
            
            for symbol in markets:
                if not self.active: break
                
                try:
                    # 1. Primary Polling: 15m (Every loop)
                    candles_15m = await self.kraken.get_spot_ohlcv(symbol, "15m", limit=100)
                    if candles_15m:
                        self.output_queue.put(MarketUpdate(symbol=symbol, candles=candles_15m, timeframe="15m", is_historical=False))
                        
                    # 2. Secondary Polling: 1h (Every ~15m loop equivalent or throttled)
                    # For simplicity, we can poll them but at higher sleep or random?
                    # Let's just poll them every time for now as 3 calls is cheap.
                    candles_1h = await self.kraken.get_spot_ohlcv(symbol, "1h", limit=100)
                    if candles_1h:
                        self.output_queue.put(MarketUpdate(symbol=symbol, candles=candles_1h, timeframe="1h", is_historical=False))

                    # 3. Tertiary Polling: 4h (Occasionally)
                    # We can use time-based check per symbol if we want to be hyper-optimized.
                    # But 3 requests per symbol is still well within 15/s limit (200 symbols * 3 = 600 total).
                    # Loop takes 60s. 600 calls / 60s = 10 calls/s. Still safe.
                    candles_4h = await self.kraken.get_spot_ohlcv(symbol, "4h", limit=100)
                    if candles_4h:
                        self.output_queue.put(MarketUpdate(symbol=symbol, candles=candles_4h, timeframe="4h", is_historical=False))
                        
                except Exception as e:
                   logger.debug(f"Polling failed for {symbol}: {e}")
                   
                # Throttle
                await asyncio.sleep(0.1)

            elapsed = time.time() - loop_start
            sleep_time = max(5.0, 60.0 - elapsed)
            await asyncio.sleep(sleep_time)

    def _send_status(self, status: str, details: Dict = None):
        msg = ServiceStatus(
            service_name="DataService",
            status=status,
            timestamp=datetime.now(timezone.utc),
            details=details
        )
        self.output_queue.put(msg)
