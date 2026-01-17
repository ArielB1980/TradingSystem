
import asyncio
import os
import sys
from datetime import datetime, timezone
from src.config.config import load_config
from src.data.kraken_client import KrakenClient
from src.data.candle_manager import CandleManager
from src.storage.db import init_db

async def main():
    print("Initializing components...")
    config = load_config()
    init_db(config.data.database_url)
    
    client = KrakenClient(
        api_key=config.exchange.api_key,
        api_secret=config.exchange.api_secret,
        use_testnet=config.exchange.use_testnet
    )
    await client.initialize()
    
    manager = CandleManager(client)
    
    symbol = "AXS/USD"
    # Pre-check DB state (via manager)
    # We bypass initialize to see what update_candles does on cold start for a symbol
    
    print(f"Testing update_candles for {symbol} (1h)...")
    await manager.update_candles(symbol)
    
    c1h = manager.get_candles(symbol, "1h")
    print(f"Manager 1h count: {len(c1h)}")
    
    if c1h:
        print(f"First: {c1h[0].timestamp}")
        print(f"Last: {c1h[-1].timestamp}")
        
        # Check if it persists
        await manager.flush_pending()
        print("Flushed pending.")
        
    await client.exchange.close()

if __name__ == "__main__":
    asyncio.run(main())
