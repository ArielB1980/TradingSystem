"""
Test Kraken API connection with provided credentials.
"""
import os
import asyncio
from src.data.kraken_client import KrakenClient
from src.monitoring.logger import setup_logging, get_logger

# Set credentials
os.environ['KRAKEN_API_KEY'] = 'sIHZanYflTqKAv9dsP0L5Xu+tjR2jFo5xI582NEQ2wAmqIoDIjm70MEq'
os.environ['KRAKEN_API_SECRET'] = 'RIpGuxXd+bfgJPeajbeKrh4FWxxXqjIsmTo3Qvfr5/B9eNJ825xL7I/ddso6rjO2UGIyaHM/ctVtJmadaDsD8A=='
os.environ['KRAKEN_FUTURES_API_KEY'] = 'h9Q2qGIO3enaa1kM14e6RBNLQa5iY1RFjyCRJkuLOdq8y2BG9SVhWqh6'
os.environ['KRAKEN_FUTURES_API_SECRET'] = '6F+Zm32Eog6dri8ybrqkGchcDpnHzF/irfD4RRt2HN2DdSPjkplvURBoCh12egTVPNWIzz7662MAwEQdZAVgb8uZ'

setup_logging("INFO", "text")
logger = get_logger(__name__)

async def test_connection():
    """Test Kraken API connection."""
    print("Testing Kraken API connection...")
    
    client = KrakenClient(
        api_key=os.environ['KRAKEN_API_KEY'],
        api_secret=os.environ['KRAKEN_API_SECRET'],
        futures_api_key=os.environ['KRAKEN_FUTURES_API_KEY'],
        futures_api_secret=os.environ['KRAKEN_FUTURES_API_SECRET'],
    )
    
    try:
        # Test 1: Fetch spot OHLCV
        print("\n1. Testing spot OHLCV fetch (BTC/USD)...")
        candles = await client.get_spot_ohlcv("BTC/USD", "1h", limit=5)
        print(f"✅ Fetched {len(candles)} candles")
        if candles:
            print(f"   Latest: {candles[-1].timestamp} - Close: ${candles[-1].close}")
        
        # Test 2: Get account balance
        print("\n2. Testing account balance...")
        balance = await client.get_account_balance()
        print(f"✅ Balance: {balance}")
        
        # Test 3: Get futures mark price (will implement)
        print("\n3. Testing futures mark price (BTCUSD-PERP)...")
        try:
            mark_price = await client.get_futures_mark_price("BTCUSD-PERP")
            print(f"✅ Mark price: ${mark_price}")
        except NotImplementedError:
            print("⚠️  Mark price not yet implemented")
        
        # Test 4: Get futures positions
        print("\n4. Testing futures positions...")
        try:
            position = await client.get_futures_position("BTCUSD-PERP")
            print(f"✅ Position: {position}")
        except NotImplementedError:
            print("⚠️  Position fetch not yet implemented")
        
        print("\n✅ Connection test complete!")
        
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test_connection())
