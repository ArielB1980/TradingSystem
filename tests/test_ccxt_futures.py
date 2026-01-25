"""
Test if Kraken SPOT API keys can trade futures/perpetuals.
Check account permissions and what they can access.
"""
import os
import ccxt

os.environ['KRAKEN_API_KEY'] = 'sIHZanYflTqKAv9dsP0L5Xu+tjR2jFo5xI582NEQ2wAmqIoDIjm70MEq'
os.environ['KRAKEN_API_SECRET'] = 'RIpGuxXd+bfgJPeajbeKrh4FWxxXqjIsmTo3Qvfr5/B9eNJ825xL7I/ddso6rjO2UGIyaHM/ctVtJmadaDsD8A=='

def test_spot_api_for_futures():
    """Check what the spot API can access."""
    
    print("\n=== Testing Spot API for Futures Access ===\n")
    
    exchange = ccxt.kraken({
        'apiKey': os.environ['KRAKEN_API_KEY'],
        'secret': os.environ['KRAKEN_API_SECRET'],
        'enableRateLimit': True,
    })
    
    markets = exchange.load_markets()
    
    # Look for derivative/future markets
    print("[1] Looking for BTC perpetual/futures symbols...")
    btc_symbols = [s for s in markets.keys() if 'BTC' in s]
    print(f"   Total BTC markets: {len(btc_symbols)}")
    
    # Check for perpetual format (CCXT uses :settle format for perpetuals)
    perp_format = [s for s in btc_symbols if ':' in s]
    print(f"   Perpetual format symbols: {len(perp_format)}")
    if perp_format:
        print(f"   Examples: {perp_format[:5]}")
        
        # Try to get details
        for symbol in perp_format[:2]:
            market = exchange.market(symbol)
            print(f"\n   Symbol: {symbol}")
            print(f"      Type: {market.get('type')}")
            print(f"      Swap: {market.get('swap')}")
            print(f"      Future: {market.get('future')}")
            print(f"      Linear: {market.get('linear')}")
            print(f"      Settle: {market.get('settle')}")
    
    # Check account permissions
    print("\n[2] Checking account permissions...")
    try:
        balance = exchange.fetch_balance()
        print("   ✅ Can fetch balance")
        
        # Try to fetch positions (futures-specific)
        if exchange.has['fetchPositions']:
            try:
                positions = exchange.fetch_positions()
                print(f"   ✅ Can fetch positions: {len(positions)} positions")
            except Exception as e:
                print(f"   ⚠️  fetch_positions error: {e}")
        
        # Try to fetch orders
        if exchange.has['fetchOrders']:
            try:
                orders = exchange.fetch_orders()
                print(f"   ✅ Can fetch orders: {len(orders)} orders")
            except Exception as e:
                print(f"   ⚠️  fetch_orders error: {e}")
                
    except Exception as e:
        print(f"   ❌ Error: {e}")
    
    print("\n[3] Key Findings:")
    print("   - Kraken spot API and Kraken Futures are SEPARATE platforms")
    print("   - Spot API keys CANNOT trade on futures.kraken.com")
    print("   - You need SEPARATE Futures API keys from futures.kraken.com")
    print("   - The project requires futures.kraken.com for leverage")

if __name__ == "__main__":
    test_spot_api_for_futures()
