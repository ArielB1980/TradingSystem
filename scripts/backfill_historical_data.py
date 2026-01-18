#!/usr/bin/env python3
"""
Backfill historical candle data for all tracked coins.

This script intelligently fetches historical data with:
- API rate limit protection
- Batch processing
- Resume capability
- Existing data checks (skip if already have enough data)
"""
import asyncio
import sys
import os
import argparse
from datetime import datetime, timedelta, timezone

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config.config import load_config
from src.data.kraken_client import KrakenClient
from src.data.data_acquisition import DataAcquisition
from src.storage.repository import count_candles
from src.monitoring.logger import get_logger

logger = get_logger(__name__)


async def backfill_historical_data(
    batch_size: int = 50,
    delay_between_requests: float = 1.0,
    skip_existing: bool = True,
    days: int = 250
):
    """
    Backfill historical candle data for all coins.
    
    Args:
        batch_size: Number of coins to process before pausing
        delay_between_requests: Seconds to wait between API calls (rate limiting)
        skip_existing: Skip coins that already have enough data
        days: Number of days of historical data to fetch
    """
    
    print("\n" + "=" * 70)
    print("📊 HISTORICAL DATA BACKFILL (API-FRIENDLY)")
    print("=" * 70)
    print()
    print(f"⚙️  Settings:")
    print(f"   Batch size: {batch_size} coins")
    print(f"   API delay: {delay_between_requests}s per request")
    print(f"   Skip existing: {skip_existing}")
    print(f"   Days to fetch: {days}")
    print()
    
    # Load config
    config = load_config()
    
    # Get all spot symbols using the same logic as dashboard
    from src.dashboard.utils import _get_monitored_symbols
    spot_symbols = _get_monitored_symbols(config)
    
    print(f"📋 Total coins: {len(spot_symbols)}")
    
    # Check which coins need backfill
    if skip_existing:
        print(f"\n🔍 Checking existing data...")
        coins_to_backfill = []
        coins_skipped = []
        
        for symbol in spot_symbols:
            # Check if we have enough 1d candles for EMA 200
            daily_count = count_candles(symbol, "1d")
            if daily_count < 200:
                coins_to_backfill.append(symbol)
            else:
                coins_skipped.append(symbol)
        
        print(f"   ✅ {len(coins_skipped)} coins already have sufficient data")
        print(f"   📥 {len(coins_to_backfill)} coins need backfill")
        
        if not coins_to_backfill:
            print("\n✅ All coins already have sufficient historical data!")
            return
        
        spot_symbols = coins_to_backfill
    
    print(f"\n📊 Will backfill {len(spot_symbols)} coins")
    print()
    
    # Initialize Kraken client
    kraken_client = KrakenClient(
        api_key=os.getenv("KRAKEN_API_KEY", ""),
        api_secret=os.getenv("KRAKEN_API_SECRET", "")
    )
    
    # Initialize data acquisition
    data_acq = DataAcquisition(
        kraken_client=kraken_client,
        spot_symbols=spot_symbols,
        futures_symbols=config.exchange.futures_markets
    )
    
    # Timeframes to backfill
    timeframes = ["1d", "4h", "1h", "15m"]
    
    # Calculate date range (250 days to ensure 200+ daily candles)
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(days=250)
    
    print(f"📅 Date Range:")
    print(f"   Start: {start_time.strftime('%Y-%m-%d')}")
    print(f"   End:   {end_time.strftime('%Y-%m-%d')}")
    print(f"   Days:  250")
    print()
    
    print(f"⏱️  Timeframes: {', '.join(timeframes)}")
    print()
    
    # Backfill in batches to avoid overwhelming API
    total_candles = 0
    successful = 0
    failed = []
    
    for batch_start in range(0, len(spot_symbols), batch_size):
        batch_end = min(batch_start + batch_size, len(spot_symbols))
        batch = spot_symbols[batch_start:batch_end]
        
        print(f"\n📦 Batch {batch_start//batch_size + 1}/{(len(spot_symbols)-1)//batch_size + 1} ({len(batch)} coins)")
        print("-" * 70)
        
        for i, symbol in enumerate(batch, 1):
            global_idx = batch_start + i
            print(f"[{global_idx}/{len(spot_symbols)}] {symbol:15s} ", end="", flush=True)
            
            try:
                symbol_candles = 0
                
                for tf in timeframes:
                    try:
                        candles = await data_acq.fetch_spot_historical(
                            symbol=symbol,
                            timeframe=tf,
                            start_time=start_time,
                            end_time=end_time
                        )
                        
                        symbol_candles += len(candles)
                        print(f".", end="", flush=True)
                        
                        # Rate limiting: wait between requests
                        await asyncio.sleep(delay_between_requests)
                        
                    except Exception as e:
                        print(f"✗", end="", flush=True)
                        logger.error(f"Failed to fetch {symbol} {tf}", error=str(e))
                
                print(f" ✓ ({symbol_candles} candles)")
                total_candles += symbol_candles
                successful += 1
                
            except Exception as e:
                print(f" ✗ FAILED")
                failed.append(symbol)
                logger.error(f"Failed to backfill {symbol}", error=str(e))
        
        # Pause between batches to be extra safe with API limits
        if batch_end < len(spot_symbols):
            print(f"\n⏸️  Pausing 5 seconds before next batch...")
            await asyncio.sleep(5)
    
    # Summary
    print()
    print("=" * 70)
    print("📊 BACKFILL SUMMARY")
    print("=" * 70)
    print(f"✅ Successful: {successful}/{len(spot_symbols)}")
    print(f"❌ Failed:     {len(failed)}/{len(spot_symbols)}")
    print(f"📈 Total Candles Stored: {total_candles:,}")
    print()
    
    if failed:
        print("Failed symbols:")
        for symbol in failed:
            print(f"  - {symbol}")
        print()
    
    # Close client
    await kraken_client.close()
    
    print("✅ Backfill complete!")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Backfill historical candle data')
    parser.add_argument('--batch-size', type=int, default=50,
                        help='Number of coins per batch (default: 50)')
    parser.add_argument('--delay', type=float, default=1.0,
                        help='Delay between API requests in seconds (default: 1.0)')
    parser.add_argument('--no-skip', action='store_true',
                        help='Fetch all coins, even if they have data')
    parser.add_argument('--days', type=int, default=250,
                        help='Number of days to backfill (default: 250)')
    
    args = parser.parse_args()
    
    asyncio.run(backfill_historical_data(
        batch_size=args.batch_size,
        delay_between_requests=args.delay,
        skip_existing=not args.no_skip,
        days=args.days
    ))
