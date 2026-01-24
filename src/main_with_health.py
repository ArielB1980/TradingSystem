"""
Main entry point: DataService + TradingService with embedded health API.

DEPRECATED for production. Production uses run.py live → LiveTrading and
src.health for the web service. Exits with code 1 if ENVIRONMENT=prod.
See docs/PRODUCTION_RUNTIME.md.
"""
import asyncio
import time
import sys
import signal
import os
from asyncio import Queue
from typing import Optional
import threading
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from src.config.config import Config, load_config
from src.monitoring.logger import get_logger, setup_logging
from src.services.data_service import DataService
from src.services.trading_service import TradingService
from src.ipc.messages import ServiceCommand

logger = get_logger("Main")

# Global reference to services for health checks
data_service_ref: Optional[DataService] = None
trading_service_ref: Optional[TradingService] = None
system_start_time = time.time()

# FastAPI app for health endpoints
app = FastAPI(title="Trading Bot Health API")


@app.get("/")
async def root():
    """Root endpoint."""
    return {"status": "ok", "service": "trading-bot"}


@app.get("/health")
async def health():
    """Health check endpoint."""
    checks = {
        "status": "healthy",
        "uptime_seconds": int(time.time() - system_start_time),
        "database": "unknown",
        "environment": os.getenv("ENVIRONMENT", "unknown"),
    }

    # Check database connection
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        checks["database"] = "configured"
        try:
            from src.storage.db import get_db
            from sqlalchemy import text
            db = get_db()
            with db.get_session() as session:
                session.execute(text("SELECT 1;"))
            checks["database"] = "connected"
        except Exception as e:
            checks["database"] = f"error: {str(e)[:50]}"
            checks["status"] = "unhealthy"
    else:
        checks["database"] = "missing"
        checks["status"] = "unhealthy"

    status_code = 200 if checks["status"] == "healthy" else 503
    return JSONResponse(content=checks, status_code=status_code)


@app.get("/ready")
async def ready():
    """Readiness probe."""
    return {"status": "ready"}


@app.get("/api/quick-test")
async def quick_test():
    """Quick system connectivity test."""
    results = {
        "database": "unknown",
        "api_keys": "unknown",
        "environment": os.getenv("ENVIRONMENT", "unknown"),
        "uptime_seconds": int(time.time() - system_start_time)
    }

    database_url = os.getenv("DATABASE_URL", "")
    if database_url:
        results["database"] = "configured"
        try:
            from src.storage.db import get_db
            from sqlalchemy import text
            db = get_db()
            with db.get_session() as session:
                session.execute(text("SELECT 1;"))
            results["database"] = "connected"
        except Exception as e:
            results["database"] = f"error: {str(e)[:50]}"
    else:
        results["database"] = "not_configured"

    has_spot_key = bool(os.getenv("KRAKEN_API_KEY"))
    has_spot_secret = bool(os.getenv("KRAKEN_API_SECRET"))
    has_futures_key = bool(os.getenv("KRAKEN_FUTURES_API_KEY"))
    has_futures_secret = bool(os.getenv("KRAKEN_FUTURES_API_SECRET"))

    if has_spot_key and has_spot_secret:
        results["api_keys"] = "spot_configured"
    if has_futures_key and has_futures_secret:
        results["api_keys"] = "futures_configured" if results["api_keys"] == "spot_configured" else "futures_only"
    if not has_spot_key and not has_futures_key:
        results["api_keys"] = "not_configured"

    results["status"] = "ok" if results["database"] == "connected" else "issues"

    return JSONResponse(content=results)


@app.get("/api/debug/signals")
async def debug_signals(symbol: Optional[str] = None):
    """Debug endpoint to find the last generated signal."""
    try:
        from src.storage.db import get_db
        from sqlalchemy import text
        import json

        db = get_db()
        results = {
            "status": "success",
            "last_signal": None,
            "checked_events": 0,
            "recent_decisions": []
        }

        with db.get_session() as session:
            if symbol:
                query = text("""
                    SELECT timestamp, details, symbol
                    FROM system_events
                    WHERE event_type = 'DECISION_TRACE' AND symbol = :symbol
                    ORDER BY timestamp DESC
                    LIMIT 20
                """)
                params = {"symbol": symbol}
            else:
                query = text("""
                    SELECT timestamp, details, symbol
                    FROM system_events
                    WHERE event_type = 'DECISION_TRACE'
                    ORDER BY timestamp DESC
                    LIMIT 50
                """)
                params = {}

            rows = session.execute(query, params)

            for row in rows:
                timestamp = row[0]
                details_raw = row[1]
                symbol = row[2]

                results["checked_events"] += 1

                try:
                    if isinstance(details_raw, str):
                        data = json.loads(details_raw)
                    else:
                        data = details_raw
                except:
                    data = {"error": "failed to parse details"}

                signal = data.get('signal', 'NONE')
                quality = data.get('setup_quality', 0)
                reasoning = data.get('reasoning', [])

                results["recent_decisions"].append({
                    "time": str(timestamp),
                    "symbol": symbol,
                    "signal": signal,
                    "quality": quality,
                    "reasoning": reasoning[-1] if reasoning else "No reasoning logged"
                })

                if signal and signal.upper() in ['LONG', 'SHORT']:
                    if results["last_signal"] is None:
                        results["last_signal"] = {
                            "timestamp": str(timestamp),
                            "symbol": symbol,
                            "signal": signal,
                            "quality": quality,
                            "details": data,
                            "reasoning": reasoning
                        }
                        break

            return JSONResponse(content=results)

    except Exception as e:
        return JSONResponse(
            content={
                "status": "error",
                "message": str(e),
                "type": type(e).__name__
            },
            status_code=500
        )


async def main_async():
    """Main trading bot logic."""
    global data_service_ref, trading_service_ref

    setup_logging()
    logger.warning(
        "main_with_health is NOT the production runtime. "
        "Use run.py live + src.health. See docs/PRODUCTION_RUNTIME.md."
    )
    logger.info("Initializing Trading Bot with Health Endpoints...")

    # Load Config
    try:
        config = load_config()
    except Exception as e:
        logger.critical(f"Failed to load config: {e}")
        return

    # Async Queues
    market_data_queue = Queue(maxsize=100)
    command_queue_data = Queue()
    command_queue_trading = Queue()

    # Services
    data_service = DataService(market_data_queue, command_queue_data, config)
    trading_service = TradingService(market_data_queue, command_queue_trading, config)

    # Store references for health checks
    data_service_ref = data_service
    trading_service_ref = trading_service

    logger.info("Launching Service Tasks...")

    # Create Tasks
    t_data = asyncio.create_task(data_service.start())
    t_trading = asyncio.create_task(trading_service.start())

    # Record startup event
    try:
        from src.storage.repository import record_event
        pid = os.getpid()
        record_event("SYSTEM_STARTUP", "system", {
            "version": config.system.version,
            "pid": pid,
            "mode": "SingleProcessAsync_WithHealth"
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


def run_fastapi():
    """Run FastAPI server in background thread."""
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


def main():
    """Main entry point that runs both FastAPI and the trading bot."""
    if os.getenv("ENVIRONMENT") == "prod":
        logger.critical(
            "main_with_health must NOT run in production. "
            "Use run.py live + src.health. Set worker run_command to: "
            "python migrate_schema.py && python run.py live --force"
        )
        sys.exit(1)
    # Start FastAPI in background thread
    api_thread = threading.Thread(target=run_fastapi, daemon=True)
    api_thread.start()

    # Give FastAPI time to start
    time.sleep(2)
    logger.info("Health API started on port 8080")

    # Run trading bot in main thread
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
