
import asyncio
import os
import sys
from src.config.config import load_config
from src.data.kraken_client import KrakenClient

async def main():
    try:
        config = load_config("src/config/config.yaml")
        client = KrakenClient(
            api_key=config.exchange.api_key,
            api_secret=config.exchange.api_secret,
            futures_api_key=config.exchange.futures_api_key,
            futures_api_secret=config.exchange.futures_api_secret,
            use_testnet=config.exchange.use_testnet
        )
        
        print(f"Testing Balance Fetch (Testnet={config.exchange.use_testnet})...")
        try:
            balance = await client.get_futures_balance()
            print("SUCCESS! Balance fetched:")
            print(balance)
        except Exception:
            import traceback
            print("\nFAILED TO FETCH BALANCE:")
            traceback.print_exc()
            
    except Exception as e:
        print(f"Setup Error: {e}")
    finally:
        await client.close()

if __name__ == "__main__":
    asyncio.run(main())
