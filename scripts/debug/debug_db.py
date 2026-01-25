
import asyncio
import os
import sys
from src.storage.repository import load_candles_map
from src.storage.db import init_db
from src.config.config import load_config

# Ensure we can import from src
sys.path.append(os.getcwd())

def main():
    print("Checking Database Content...")
    # Initialize DB connection (create engine)
    config = load_config()
    init_db(config.data.database_url)
    
    symbols = ["AXS/USD", "BTC/USD"]
    timeframe = "1h"
    
    print(f"Querying DB for {symbols} at {timeframe}...")
    
    # load_candles_map(symbols, timeframe, days)
    # Let's look back 10 days
    data = load_candles_map(symbols, timeframe, days=10)
    
    for sym in symbols:
        candles = data.get(sym, [])
        if not candles:
            print(f"❌ {sym} {timeframe}: NO DATA in DB (Empty list)")
        else:
            print(f"✅ {sym} {timeframe}: Found {len(candles)} candles.")
            print(f"   First: {candles[0].timestamp}")
            print(f"   Last:  {candles[-1].timestamp}")

if __name__ == "__main__":
    main()
