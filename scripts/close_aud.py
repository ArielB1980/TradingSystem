import asyncio
import os
import sys

# Add project root to path
sys.path.append(os.getcwd())

from src.data.kraken_client import KrakenClient
from src.config.dotenv_loader import load_dotenv_files

async def close_aud():
    print("Loading environment...")
    load_dotenv_files()
    
    api_key = os.getenv("KRAKEN_API_KEY")
    futures_key = os.getenv("KRAKEN_FUTURES_API_KEY")
    
    if not api_key or not futures_key:
        print("❌ Error: API keys not found in environment")
        return

    print("Connecting to Kraken...")
    client = KrakenClient(
        api_key=api_key,
        api_secret=os.getenv("KRAKEN_API_SECRET"),
        futures_api_key=futures_key,
        futures_api_secret=os.getenv("KRAKEN_FUTURES_API_SECRET")
    )
    
    symbol = "PF_AUDUSD"
    print(f"Checking position for {symbol}...")
    
    try:
        positions = await client.get_futures_positions()
        target = next((p for p in positions if p.get('symbol') == symbol), None)
        
        if not target:
            print(f"⚪️ No open position found for {symbol}")
            await client.close()
            return

        size = float(target.get('size', 0))
        if size == 0:
             print(f"⚪️ Position size is 0 for {symbol}")
             await client.close()
             return
             
        side = "sell" if size > 0 else "buy"
        print(f"🔴 Found position: {size} contracts. Closing via {side}...")
        
        # Cancel open orders first
        print("Cancelling open orders...")
        await client.cancel_all_orders(symbol)
        
        # Place market close
        print(f"Placing {side} market order for {abs(size)}...")
        resp = await client.place_futures_order(
            symbol=symbol,
            side=side,
            order_type="market",
            size=abs(size),
            reduce_only=True
        )
        print(f"✅ Close order placed: {resp}")
        
    except Exception as e:
        print(f"❌ Error: {e}")
    finally:
        await client.close()
        print("Done.")

if __name__ == "__main__":
    asyncio.run(close_aud())
