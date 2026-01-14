#!/usr/bin/env python3
"""
Check data freshness for all monitored coins.

Identifies which coins don't have fresh DECISION_TRACE events and why.
"""
import sys
import os
from datetime import datetime, timezone, timedelta

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.storage.db import get_db
from src.storage.repository import get_latest_traces, get_recent_events
from src.config.config import load_config
from src.dashboard.utils import _get_monitored_symbols
from src.monitoring.logger import get_logger, setup_logging

logger = get_logger(__name__)
setup_logging("INFO", "json")


def analyze_freshness():
    """Analyze data freshness for all monitored coins."""
    config = load_config()
    all_symbols = _get_monitored_symbols(config)
    
    print("\n" + "="*80)
    print("DATA FRESHNESS ANALYSIS")
    print("="*80)
    print(f"Total monitored coins: {len(all_symbols)}")
    print(f"Analysis time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("="*80 + "\n")
    
    # Get latest traces for all symbols
    traces = get_latest_traces(limit=1000)
    traces_by_symbol = {trace.get('symbol'): trace for trace in traces}
    
    now = datetime.now(timezone.utc)
    
    # Categorize coins
    active_coins = []
    stale_coins = []
    dead_coins = []
    missing_coins = []
    
    for symbol in sorted(all_symbols):
        trace = traces_by_symbol.get(symbol)
        
        if not trace:
            missing_coins.append({
                'symbol': symbol,
                'reason': 'no_trace_events',
                'last_update': None
            })
            continue
        
        # Parse timestamp
        last_update = trace.get('timestamp')
        if isinstance(last_update, str):
            try:
                last_update = datetime.fromisoformat(last_update.replace('Z', '+00:00'))
            except:
                last_update = None
        elif last_update and last_update.tzinfo is None:
            last_update = last_update.replace(tzinfo=timezone.utc)
        
        if not last_update:
            dead_coins.append({
                'symbol': symbol,
                'reason': 'invalid_timestamp',
                'last_update': None,
                'trace': trace
            })
            continue
        
        age_seconds = (now - last_update).total_seconds()
        age_minutes = age_seconds / 60
        age_hours = age_seconds / 3600
        
        # Get status from trace details
        details = trace.get('details', {})
        status = details.get('status', 'unknown')
        reason = details.get('reason', 'unknown')
        error = details.get('error', '')
        
        coin_info = {
            'symbol': symbol,
            'age_minutes': age_minutes,
            'age_hours': age_hours,
            'last_update': last_update,
            'status': status,
            'reason': reason,
            'error': error[:100] if error else None,
            'spot_price': details.get('spot_price', 0.0),
            'signal': details.get('signal', 'NO_SIGNAL')
        }
        
        if age_seconds < 3600:  # < 1 hour
            active_coins.append(coin_info)
        elif age_seconds < 21600:  # < 6 hours
            stale_coins.append(coin_info)
        else:  # > 6 hours
            dead_coins.append(coin_info)
    
    # Print summary
    print("SUMMARY")
    print("-" * 80)
    print(f"🟢 Active (< 1h):     {len(active_coins):4d} coins ({len(active_coins)/len(all_symbols)*100:.1f}%)")
    print(f"🟡 Stale (1-6h):      {len(stale_coins):4d} coins ({len(stale_coins)/len(all_symbols)*100:.1f}%)")
    print(f"🔴 Dead (> 6h):       {len(dead_coins):4d} coins ({len(dead_coins)/len(all_symbols)*100:.1f}%)")
    print(f"⚪ Missing (no data):  {len(missing_coins):4d} coins ({len(missing_coins)/len(all_symbols)*100:.1f}%)")
    print()
    
    # Analyze reasons for stale/dead coins
    if stale_coins or dead_coins or missing_coins:
        print("ISSUES BY REASON")
        print("-" * 80)
        
        reason_counts = {}
        for coin in stale_coins + dead_coins:
            reason = coin.get('reason', 'unknown')
            status = coin.get('status', 'unknown')
            key = f"{reason} ({status})"
            reason_counts[key] = reason_counts.get(key, 0) + 1
        
        for coin in missing_coins:
            reason = coin.get('reason', 'no_trace_events')
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        
        for reason, count in sorted(reason_counts.items(), key=lambda x: -x[1]):
            print(f"  {reason:40s}: {count:4d} coins")
        print()
    
    # Show stale coins
    if stale_coins:
        print("STALE COINS (1-6 hours old)")
        print("-" * 80)
        stale_coins.sort(key=lambda x: x['age_hours'], reverse=True)
        for coin in stale_coins[:20]:  # Show top 20
            age_str = f"{coin['age_hours']:.1f}h" if coin['age_hours'] >= 1 else f"{coin['age_minutes']:.0f}m"
            print(f"  {coin['symbol']:20s} | {age_str:8s} | {coin['status']:20s} | {coin['reason']}")
        if len(stale_coins) > 20:
            print(f"  ... and {len(stale_coins) - 20} more")
        print()
    
    # Show dead coins
    if dead_coins:
        print("DEAD COINS (> 6 hours old)")
        print("-" * 80)
        dead_coins.sort(key=lambda x: x['age_hours'], reverse=True)
        for coin in dead_coins[:30]:  # Show top 30
            age_str = f"{coin['age_hours']:.1f}h"
            error_str = f" | {coin['error']}" if coin['error'] else ""
            print(f"  {coin['symbol']:20s} | {age_str:8s} | {coin['status']:20s} | {coin['reason']}{error_str}")
        if len(dead_coins) > 30:
            print(f"  ... and {len(dead_coins) - 30} more")
        print()
    
    # Show missing coins
    if missing_coins:
        print("MISSING COINS (no DECISION_TRACE events)")
        print("-" * 80)
        for coin in missing_coins[:30]:
            print(f"  {coin['symbol']:20s} | {coin['reason']}")
        if len(missing_coins) > 30:
            print(f"  ... and {len(missing_coins) - 30} more")
        print()
    
    # Recommendations
    print("RECOMMENDATIONS")
    print("-" * 80)
    
    if missing_coins:
        print(f"❌ {len(missing_coins)} coins have NO data:")
        print("   → Check if live trading is running")
        print("   → Check if these symbols are valid/exist on exchange")
        print("   → Check for API errors in logs")
        print()
    
    error_coins = [c for c in stale_coins + dead_coins if c.get('status') == 'error']
    if error_coins:
        print(f"❌ {len(error_coins)} coins have processing errors:")
        print("   → Check error messages above")
        print("   → Review live trading logs for these symbols")
        print("   → May need to fix API connectivity or data issues")
        print()
    
    circuit_breaker_coins = [c for c in stale_coins + dead_coins if c.get('status') == 'circuit_breaker_open']
    if circuit_breaker_coins:
        print(f"⚠️  {len(circuit_breaker_coins)} coins are in circuit breaker:")
        print("   → These coins failed repeatedly and are temporarily disabled")
        print("   → Will auto-recover after timeout (5 minutes)")
        print("   → Check logs to see why they're failing")
        print()
    
    no_price_coins = [c for c in stale_coins + dead_coins if c.get('status') in ['no_price', 'zero_price']]
    if no_price_coins:
        print(f"⚠️  {len(no_price_coins)} coins have price issues:")
        print("   → Check if these symbols exist on exchange")
        print("   → Check API ticker endpoint for these symbols")
        print("   → May need to remove invalid symbols from config")
        print()
    
    fetch_error_coins = [c for c in stale_coins + dead_coins if c.get('status') == 'fetch_error']
    if fetch_error_coins:
        print(f"⚠️  {len(fetch_error_coins)} coins have fetch errors:")
        print("   → Check API connectivity")
        print("   → Check rate limits")
        print("   → May be temporary API issues")
        print()
    
    monitoring_coins = [c for c in stale_coins + dead_coins if c.get('status') == 'monitoring']
    if monitoring_coins:
        print(f"ℹ️  {len(monitoring_coins)} coins are in monitoring mode (insufficient candles):")
        print("   → These coins need more historical data")
        print("   → Will become active once 50+ candles are collected")
        print("   → This is normal for new coins")
        print()
    
    if not (missing_coins or error_coins or circuit_breaker_coins or no_price_coins):
        print("✅ All coins have fresh data or are in expected states!")
        print()
    
    # Check if live trading is running
    print("SYSTEM STATUS")
    print("-" * 80)
    try:
        import subprocess
        result = subprocess.run(
            ["pgrep", "-f", "src/cli.py live"],
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            print("✅ Live trading process is running")
        else:
            print("❌ Live trading process is NOT running")
            print("   → Start with: python3 run.py live --force")
    except Exception as e:
        print(f"⚠️  Could not check process status: {e}")
    
    # Check latest trace age
    if traces:
        latest_trace = max(traces, key=lambda t: t.get('timestamp', datetime.min.replace(tzinfo=timezone.utc)))
        latest_time = latest_trace.get('timestamp')
        if isinstance(latest_time, str):
            try:
                latest_time = datetime.fromisoformat(latest_time.replace('Z', '+00:00'))
            except:
                latest_time = None
        
        if latest_time:
            latest_age = (now - latest_time).total_seconds() / 60
            if latest_age < 10:
                print(f"✅ Latest trace is {latest_age:.1f} minutes old (fresh)")
            elif latest_age < 60:
                print(f"⚠️  Latest trace is {latest_age:.1f} minutes old (may be stale)")
            else:
                print(f"❌ Latest trace is {latest_age:.1f} minutes old (very stale)")
    
    print()
    print("="*80)


if __name__ == "__main__":
    try:
        analyze_freshness()
    except Exception as e:
        logger.error("Failed to analyze freshness", error=str(e))
        import traceback
        traceback.print_exc()
        sys.exit(1)
