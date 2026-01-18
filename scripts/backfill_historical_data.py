#!/usr/bin/env python3
"""
Backfill historical candle data for all tracked coins.

This script fetches 200+ days of historical data for each coin
to ensure EMA 200 calculations work properly.
"""
import asyncio
import sys
import os
from datetime import datetime, timedelta, timezone

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config.config import load_config
from src.data.kraken_client import KrakenClient
from src.data.data_acquisition import DataAcquisition
from src.monitoring.logger import get_logger

logger = get_logger(__name__)


async def backfill_historical_data():
    """Backfill historical candle data for all coins."""
    
    print("\n" + "=" * 70)
    print("📊 HISTORICAL DATA BACKFILL")
    print("=" * 70)
    print()
    
    # Load config
    config = load_config()
    
    # Get all spot symbols
    spot_symbols = config.spot_symbols
    print(f"📋 Found {len(spot_symbols)} coins to backfill")
    print()
    
    # Initialize Kraken client
    kraken_client = KrakenClient(
        api_key=os.getenv("KRAKEN_API_KEY", ""),
        api_secret=os.getenv("KRAKEN_API_SECRET", ""),
        timeout=30
    )
    
    # Initialize data acquisition
    data_acq = DataAcquisition(
        kraken_client=kraken_client,
        spot_symbols=spot_symbols,
        futures_symbols=config.futures_symbols
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
    
    # Backfill each symbol
    total_candles = 0
    successful = 0
    failed = []
    
    for i, symbol in enumerate(spot_symbols, 1):
        print(f"[{i}/{len(spot_symbols)}] {symbol:15s} ", end="", flush=True)
        
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
                    
                    # Small delay to avoid rate limits
                    await asyncio.sleep(0.5)
                    
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
    asyncio.run(backfill_historical_data())
