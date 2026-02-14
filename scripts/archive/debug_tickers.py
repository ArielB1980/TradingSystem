import os
import sys
import asyncio
import pprint
# Add src to path
sys.path.append(os.getcwd())
try:
    from src.config.config import load_config
    from src.data.kraken_client import KrakenClient
except ImportError:
    print("❌ Could not import src modules.")
    sys.exit(1)

async def debug_tickers():
    print("Loading Config...")
    config = load_config()
    
    client = KrakenClient(
        api_key=config.exchange.api_key,
        api_secret=config.exchange.api_secret,
        futures_api_key=os.getenv('KRAKEN_FUTURES_API_KEY'),
        futures_api_secret=os.getenv('KRAKEN_FUTURES_API_SECRET')
    )
    
    try:
        print("Initializing Client...")
        await client.initialize()
        
        # 1. SPOT SEARCH
        print("\n--- SPOT MARKETS SEARCH (AGLD) ---")
        markets = await client.exchange.load_markets()
        found_spot = False
        for symbol, m in markets.items():
            if 'AGLD' in symbol:
                status = "✅ ACTIVE" if m.get('active') else "❌ INACTIVE"
                print(f"{symbol:<15} ID: {m['id']:<10} {status}")
                if m.get('active'):
                    found_spot = True
        
        if not found_spot:
            print("❌ NO ACTIVE SPOT PAIRS FOUND FOR AGLD on Kraken.")
            print("   The SMCEngine relies on Spot Data. Without Spot data, we cannot trade.")

        # 2. FUTURES SEARCH
        print("\n--- FUTURES MARKETS SEARCH (AGLD) ---")
        if client.futures_exchange:
            f_markets = await client.futures_exchange.load_markets()
            found_futures = False
            for symbol, m in f_markets.items():
                if 'AGLD' in symbol:
                    print(f"{symbol:<15} ID: {m['id']:<10} Type: {m.get('type')}")
                    found_futures = True
            
            if not found_futures:
                print("❌ NO FUTURES FOUND FOR AGLD.")
            else:
                print("✅ AGLD Futures Found. User is correct.")
        else:
             print("⚠️  Futures credentials missing, skipping futures check.")

                 
    except Exception as e:
        print(f"❌ EXCEPTION: {e}")
    finally:
        await client.close()

if __name__ == "__main__":
    asyncio.run(debug_tickers())
