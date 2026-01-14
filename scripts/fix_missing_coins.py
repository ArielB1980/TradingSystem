#!/usr/bin/env python3
"""
Fix missing coins by ensuring they get DECISION_TRACE events logged.

This script identifies coins without data and logs initial trace events for them.
"""
import sys
import os
from datetime import datetime, timezone

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.storage.db import get_db
from src.storage.repository import get_latest_traces, async_record_event
from src.config.config import load_config
from src.dashboard.utils import _get_monitored_symbols
from src.monitoring.logger import get_logger, setup_logging
import asyncio

logger = get_logger(__name__)
setup_logging("INFO", "json")


async def fix_missing_coins():
    """Log initial DECISION_TRACE events for coins that have none."""
    config = load_config()
    all_symbols = _get_monitored_symbols(config)
    
    print("\n" + "="*80)
    print("FIXING MISSING COINS")
    print("="*80)
    
    # Get existing traces
    traces = get_latest_traces(limit=1000)
    traces_by_symbol = {trace.get('symbol'): trace for trace in traces}
    
    # Find missing coins
    missing_symbols = [s for s in all_symbols if s not in traces_by_symbol]
    
    print(f"Total monitored coins: {len(all_symbols)}")
    print(f"Coins with data: {len(traces_by_symbol)}")
    print(f"Missing coins: {len(missing_symbols)}")
    print()
    
    if not missing_symbols:
        print("✅ All coins have data!")
        return
    
    print(f"Logging initial trace events for {len(missing_symbols)} coins...")
    print()
    
    # Log initial trace for each missing coin
    now = datetime.now(timezone.utc)
    logged_count = 0
    error_count = 0
    
    for symbol in missing_symbols:
        try:
            trace_details = {
                "signal": "NO_SIGNAL",
                "regime": "unknown",
                "bias": "neutral",
                "adx": 0.0,
                "atr": 0.0,
                "ema200_slope": "flat",
                "spot_price": 0.0,
                "setup_quality": 0.0,
                "score_breakdown": {},
                "status": "initializing",
                "reason": "no_previous_data"
            }
            
            await async_record_event(
                event_type="DECISION_TRACE",
                symbol=symbol,
                details=trace_details,
                timestamp=now
            )
            
            logged_count += 1
            if logged_count % 10 == 0:
                print(f"  Logged {logged_count}/{len(missing_symbols)}...")
                
        except Exception as e:
            logger.error(f"Failed to log trace for {symbol}", error=str(e))
            error_count += 1
    
    print()
    print("="*80)
    print(f"✅ Successfully logged {logged_count} initial trace events")
    if error_count > 0:
        print(f"❌ Failed to log {error_count} trace events")
    print("="*80)
    print()
    print("Note: These coins will get real data once live trading processes them.")
    print("The initial trace ensures they appear in the dashboard with 'initializing' status.")


if __name__ == "__main__":
    try:
        asyncio.run(fix_missing_coins())
    except Exception as e:
        logger.error("Failed to fix missing coins", error=str(e))
        import traceback
        traceback.print_exc()
        sys.exit(1)
