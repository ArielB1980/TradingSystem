import asyncio
import time
import sys
import signal
import os
from asyncio import Queue

from src.config.config import Config, load_config
from src.monitoring.logger import get_logger, setup_logging
from src.services.data_service import DataService
from src.services.trading_service import TradingService
from src.ipc.messages import ServiceCommand

logger = get_logger("Main")

async def main_async():
    setup_logging()
    logger.info("Initializing Architecture v3 (Single Process Async)...")
    
    # Load Config
    try:
        config = load_config()
    except Exception as e:
        logger.critical(f"Failed to load config: {e}")
        return
        
    # Async Queues
    # Data -> Trading
    # Limit queue size to prevent memory explosion if consumer lags
    market_data_queue = Queue(maxsize=100)
    # Main -> Services
    command_queue_data = Queue()
    command_queue_trading = Queue()
    
    # Services
    data_service = DataService(market_data_queue, command_queue_data, config)
    trading_service = TradingService(market_data_queue, command_queue_trading, config)
    
    logger.info("Launching Service Tasks...")
    
    # Create Tasks
    t_data = asyncio.create_task(data_service.start())
    t_trading = asyncio.create_task(trading_service.start())
    
    # Verify DB Persistence
    try:
        from src.storage.repository import record_event
        # Note: record_event is sync (blocking), but okay for startup event
        # Ideally should be async or threaded, but it's one-off.
        # PID is just current PID
        pid = os.getpid()
        record_event("SYSTEM_STARTUP", "system", {
            "version": config.system.version,
            "pid": pid,
            "mode": "SingleProcessAsync"
        })
        logger.info(f"Startup Event recorded (PID {pid})")
    except Exception as e:
        logger.error(f"Failed to record startup event: {e}")
    
    # Monitor Loop
    stop_event = asyncio.Event()
    
    def signal_handler():
        logger.info("Shutdown Signal Received.")
        stop_event.set()
        
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)
        
    while not stop_event.is_set():
        if t_data.done():
            logger.critical("Data Service Task Ended Unexpectedly!")
            if t_data.exception():
                logger.critical(f"Data Exception: {t_data.exception()}")
            break
            
        if t_trading.done():
            logger.critical("Trading Service Task Ended Unexpectedly!")
            if t_trading.exception():
                logger.critical(f"Trading Exception: {t_trading.exception()}")
            break
            
        await asyncio.sleep(1.0)
        
    # Shutdown
    logger.info("Stopping Services...")
    await command_queue_data.put(ServiceCommand("STOP"))
    await command_queue_trading.put(ServiceCommand("STOP"))
    
    # Wait for completion
    try:
        await asyncio.wait_for(asyncio.gather(t_data, t_trading, return_exceptions=True), timeout=5.0)
    except asyncio.TimeoutError:
        logger.warning("Timed out waiting for services to stop")
        
    logger.info("System Shutdown Complete.")

def main():
    asyncio.run(main_async())

if __name__ == "__main__":
    main()
