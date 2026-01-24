
import asyncio
import os
import sys
from collections import defaultdict
from decimal import Decimal

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from src.data.kraken_client import KrakenClient
from src.monitoring.logger import get_logger
from src.config.config import load_config as get_config

async def audit_open_orders():
    try:
        config = get_config()
    except Exception as e:
        print(f"Config load failed: {e}")
        return

    client = KrakenClient(
        api_key=config.exchange.api_key,
        api_secret=config.exchange.api_secret,
        futures_api_key=config.exchange.futures_api_key,
        futures_api_secret=config.exchange.futures_api_secret,
        use_testnet=config.exchange.use_testnet
    )
    await client.initialize()
    
    try:
        print("Fetching all open orders...")
        orders = await client.get_futures_open_orders()
        
        print(f"Found {len(orders)} open orders.")
        
        orders_by_symbol = defaultdict(list)
        for order in orders:
            symbol = order.get('symbol')
            orders_by_symbol[symbol].append(order)
            
        print("\n=== DUPLICATE ORDER CHECK ===")
        duplicates_found = False
        
        for symbol, symbol_orders in orders_by_symbol.items():
            if len(symbol_orders) > 1:
                duplicates_found = True
                print(f"\n[!] DUPLICATE ORDERS FOR {symbol}: found {len(symbol_orders)}")
                for o in symbol_orders:
                    print(f"    - ID: {o.get('id')} | Type: {o.get('order_type', o.get('type'))} | Side: {o.get('side')} | Price: {o.get('stop_price', o.get('price'))} | Size: {o.get('size', o.get('amount'))}")
        
        if not duplicates_found:
            print("\nNo duplicate orders found. State is clean.")
        else:
            print("\n[!] Duplicates detected. You may want to run a deduplication script.")
            
    except Exception as e:
        print(f"Audit failed: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await client.close()

if __name__ == "__main__":
    asyncio.run(audit_open_orders())
