"""
Test Kraken Futures API with correct symbol format.
"""
import os
import asyncio
from src.data.kraken_client import KrakenClient
from src.monitoring.logger import setup_logging

# Set credentials
os.environ['KRAKEN_API_KEY'] = 'sIHZanYflTqKAv9dsP0L5Xu+tjR2jFo5xI582NEQ2wAmqIoDIjm70MEq'
os.environ['KRAKEN_API_SECRET'] = 'RIpGuxXd+bfgJPeajbeKrh4FWxxXqjIsmTo3Qvfr5/B9eNJ825xL7I/ddso6rjO2UGIyaHM/ctVtJmadaDsD8A=='
os.environ['KRAKEN_FUTURES_API_KEY'] = 'h9Q2qGIO3enaa1kM14e6RBNLQa5iY1RFjyCRJkuLOdq8y2BG9SVhWqh6'
os.environ['KRAKEN_FUTURES_API_SECRET'] = '6F+Zm32Eog6dri8ybrqkGchcDpnHzF/irfD4RRt2HN2DdSPjkplvURBoCh12egTVPNWIzz7662MAwEQdZAVgb8uZ'

setup_logging("INFO", "text")

async def test_futures():
    """Test Kraken Futures API."""
    print("Testing Kraken Futures API...")
    
    client = KrakenClient(
        api_key=os.environ['KRAKEN_API_KEY'],
        api_secret=os.environ['KRAKEN_API_SECRET'],
        futures_api_key=os.environ['KRAKEN_FUTURES_API_KEY'],
        futures_api_secret=os.environ['KRAKEN_FUTURES_API_SECRET'],
    )
    
    try:
        # Test with BTCUSD-PERP (will be converted to PF_XBTUSD)
        print("\n1. Testing mark price for BTCUSD-PERP...")
        mark_price = await client.get_futures_mark_price("BTCUSD-PERP")
        print(f"✅ Mark price: ${mark_price}")
        
        # Test positions
        print("\n2. Testing positions for PF_XBTUSD...")
        position = await client.get_futures_position("PF_XBTUSD")
        if position:
            print(f"✅ Position: {position}")
        else:
            print("✅ No open position")
        
        print("\n✅ Futures API test complete!")
        
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test_futures())
