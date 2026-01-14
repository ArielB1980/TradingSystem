"""
Periodic maintenance tasks for live trading.

Ensures data consistency and prevents issues.
"""
from datetime import datetime, timezone, timedelta
from typing import List
from src.storage.repository import get_latest_traces
from src.live.startup_validator import ensure_all_coins_have_traces
from src.monitoring.logger import get_logger

logger = get_logger(__name__)


async def periodic_data_maintenance(monitored_symbols: List[str], max_age_hours: float = 6.0):
    """
    Periodic maintenance to ensure data freshness.
    
    Checks for stale data and creates initial traces for missing coins.
    Runs periodically (e.g., every hour) to catch any gaps.
    
    Args:
        monitored_symbols: List of all symbols being monitored
        max_age_hours: Maximum age in hours before coin is considered stale
    """
    logger.info("Running periodic data maintenance", total_coins=len(monitored_symbols))
    
    # Get latest traces
    traces = get_latest_traces(limit=1000)
    traces_by_symbol = {trace.get('symbol'): trace for trace in traces}
    
    now = datetime.now(timezone.utc)
    stale_count = 0
    missing_count = 0
    
    # Check for stale or missing data
    for symbol in monitored_symbols:
        trace = traces_by_symbol.get(symbol)
        
        if not trace:
            missing_count += 1
            continue
        
        # Check age
        last_update = trace.get('timestamp')
        if isinstance(last_update, str):
            try:
                last_update = datetime.fromisoformat(last_update.replace('Z', '+00:00'))
            except:
                continue
        elif last_update and last_update.tzinfo is None:
            last_update = last_update.replace(tzinfo=timezone.utc)
        
        if last_update:
            age_hours = (now - last_update).total_seconds() / 3600
            if age_hours > max_age_hours:
                stale_count += 1
    
    # If we have stale or missing coins, ensure they get traces
    if missing_count > 0 or stale_count > 0:
        logger.warning(
            "Found stale or missing coin data",
            missing=missing_count,
            stale=stale_count,
            total=len(monitored_symbols)
        )
        
        # Ensure all coins have traces (will only create for missing ones)
        result = await ensure_all_coins_have_traces(monitored_symbols)
        
        if result['created'] > 0:
            logger.info(
                "Created missing traces during maintenance",
                created=result['created']
            )
    else:
        logger.debug("All coins have fresh data", total=len(monitored_symbols))
