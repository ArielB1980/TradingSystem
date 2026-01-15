import multiprocessing
import time
import sys
import signal
import os
from src.config.config import Config, load_config
from src.monitoring.logger import get_logger, setup_logging
from src.services.data_service import DataService
from src.services.trading_service import TradingService
from src.ipc.messages import ServiceCommand

# Force 'spawn' method for macOS compatibility - try default for Linux container
# multiprocessing.set_start_method("spawn", force=True)

logger = get_logger("Main")


def main():
    setup_logging()
    logger.info("Initializing Architecture v2 (Multiprocessing)...")
    
    # Load Config
    try:
        config = load_config()
    except Exception as e:
        logger.critical(f"Failed to load config: {e}")
        sys.exit(1)
        
    # Queues
    # Data -> Trading
    market_data_queue = multiprocessing.Queue()
    # Main -> Services
    command_queue_data = multiprocessing.Queue()
    command_queue_trading = multiprocessing.Queue()
    
    # Processes
    data_service = DataService(market_data_queue, command_queue_data, config)
    trading_service = TradingService(market_data_queue, command_queue_trading, config)
    
    # Start
    logger.info("Launching Services...")
    data_service.start()
    trading_service.start()
    
    logger.info(f"Services Started. Data PID: {data_service.pid}, Trading PID: {trading_service.pid}")
    
    # Verify DB Persistence
    try:
        from src.storage.repository import record_event
        record_event("SYSTEM_STARTUP", "system", {
            "version": config.system.version,
            "data_pid": data_service.pid,
            "trading_pid": trading_service.pid
        })
        logger.info("Startup Event recorded in DB")
    except Exception as e:
        logger.error(f"Failed to record startup event: {e}")
    
    # Signal Handling
    stop_event = multiprocessing.Event()
    
    def signal_handler(sig, frame):
        logger.info("Shutdown Signal Received.")
        stop_event.set()
        
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Monitor Loop
    try:
        while not stop_event.is_set():
            if not data_service.is_alive():
                logger.critical("Data Service Died unexpectedly!")
                stop_event.set()
            if not trading_service.is_alive():
                logger.critical("Trading Service Died unexpectedly!")
                stop_event.set()
            
            time.sleep(1.0)
            
    except Exception as e:
        logger.error(f"Main Loop Error: {e}")
    finally:
        logger.info("Stopping Services...")
        
        # Send Stop Commands
        try:
             command_queue_data.put(ServiceCommand("STOP"))
             command_queue_trading.put(ServiceCommand("STOP"))
        except:
             pass
             
        # Wait for join
        time.sleep(1.0) # Give time for graceful shutdown
        data_service.join(timeout=5.0)
        trading_service.join(timeout=5.0)
        
        # Force Kill if needed
        if data_service.is_alive():
            logger.warning("Force killing Data Service")
            data_service.terminate()
        if trading_service.is_alive():
            logger.warning("Force killing Trading Service")
            trading_service.terminate()
            
        logger.info("System Shutdown Complete.")

if __name__ == "__main__":
    main()
