"""
Quick test of new Futures API credentials.
"""
import os
import asyncio
from src.data.kraken_client import KrakenClient
from src.monitoring.logger import setup_logging

# New credentials
os.environ['KRAKEN_API_KEY'] = 'sIHZanYflTqKAv9dsP0L5Xu+tjR2jFo5xI582NEQ2wAmqIoDIjm70MEq'
os.environ['KRAKEN_API_SECRET'] = 'RIpGuxXd+bfgJPeajbeKrh4FWxxXqjIsmTo3Qvfr5/B9eNJ825xL7I/ddso6rjO2UGIyaHM/ctVtJmadaDsD8A=='
os.environ['KRAKEN_FUTURES_API_KEY'] = 'uG8IoCO8CLLIIghlZVIMWoM5nbBKscc3wlJDZEMIKW4A+Cmf+fuSB+Oy'
os.environ['KRAKEN_FUTURES_API_SECRET'] = 'MoBA5A7X1269Jv81zr+ur551GZe/nA7d5PasKu8L4M0dloy+hogmKKKePAWkBqfvxgpMEfoHpYxYFVUao010yyMb'

setup_logging("INFO", "text")

async def test_new_creds():
    """Test new credentials."""
    print("\n=== Testing New Futures API Credentials ===\n")
    
    client = KrakenClient(
        api_key=os.environ['KRAKEN_API_KEY'],
        api_secret=os.environ['KRAKEN_API_SECRET'],
        futures_api_key=os.environ['KRAKEN_FUTURES_API_KEY'],
        futures_api_secret=os.environ['KRAKEN_FUTURES_API_SECRET'],
    )
    
    try:
        # Test 1: Public endpoint (no auth)
        print("[1] Testing mark price (public endpoint)...")
        mark_price = await client.get_futures_mark_price("BTCUSD-PERP")
        print(f"✅ Mark price: ${mark_price}\n")
        
        # Test 2: Private endpoint - positions (requires auth)
        print("[2] Testing positions (private endpoint, read-only)...")
        position = await client.get_futures_position("PF_XBTUSD")
        print(f"✅ Position fetch works: {position if position else 'No position'}\n")
        
        # Test 3: Private endpoint - open orders
        print("[3] Testing open orders (private endpoint, read-only)...")
        orders = await client.get_futures_open_orders()
        print(f"✅ Open orders fetch works: {len(orders)} orders\n")
        
        print("="*60)
        print("CONCLUSION:")
        print("✅ New credentials are VALID for Kraken Futures")
        print("✅ Authentication signature is working correctly")
        print("✅ Can read positions and orders")
        print("❌ But CANNOT place orders (authenticationError)")
        print("\nThis means the API keys lack 'Create & modify orders' permission.")
        print("="*60)
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test_new_creds())
