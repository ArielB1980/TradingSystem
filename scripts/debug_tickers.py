import os
import sys
import asyncio
from src.config.config import load_config
from src.data.kraken_client import KrakenClient

async def debug_tickers():
    print("Loading Config...")
    config = load_config()
    
    # 1. Simulate Market Loading Logic from LiveTrading
    markets = config.exchange.spot_markets
    if hasattr(config, "assets") and config.assets.mode == "whitelist":
         markets = config.assets.whitelist
         print("Mode: Whitelist")
    elif config.coin_universe and config.coin_universe.enabled:
         print("Mode: Coin Universe")
         expanded = []
         for tier, coins in config.coin_universe.liquidity_tiers.items():
             expanded.extend(coins)
         markets = list(set(expanded))
    
    print(f"Total Markets Loaded: {len(markets)}")
    
    target = "AGLD/USD"
    if target in markets:
        print(f"✅ {target} is in the target list.")
    else:
        print(f"❌ {target} is NOT in the target list! Config issue?")
        return

    # 2. Simulate Bulk Fetch
    print(f"\nAttempting Bulk Fetch for {target}...")
    client = KrakenClient(
        api_key=config.exchange.api_key,
        api_secret=config.exchange.api_secret,
    )
    
    try:
        # We'll just fetch this one to see if it works in a list context
        # In live it runs with 249 others, but let's try isolation first
        tickers = await client.get_spot_tickers_bulk([target])
        
        if target in tickers:
            t = tickers[target]
            print(f"✅ Ticker Data Received:")
            print(f"   Price: {t.get('last')}")
            print(f"   Volume: {t.get('volume')}")
            print(f"   QuoteVolume: {t.get('quoteVolume')}")
        else:
            print(f"❌ Fetch successful but {target} missing from result keys: {list(tickers.keys())}")
            
    except Exception as e:
        print(f"❌ Fetch Failed: {e}")
    finally:
        await client.close()

if __name__ == "__main__":
    asyncio.run(debug_tickers())
