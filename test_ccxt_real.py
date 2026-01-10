"""
Test CCXT krakenfutures class.
"""
import os
import asyncio
import ccxt.async_support as ccxt  # Use async version

# Set credentials
os.environ['KRAKEN_FUTURES_API_KEY'] = 'uG8IoCO8CLLIIghlZVIMWoM5nbBKscc3wlJDZEMIKW4A+Cmf+fuSB+Oy'
os.environ['KRAKEN_FUTURES_API_SECRET'] = 'MoBA5A7X1269Jv81zr+ur551GZe/nA7d5PasKu8L4M0dloy+hogmKKKePAWkBqfvxgpMEfoHpYxYFVUao010yyMb'

async def test_ccxt_futures_direct():
    """Test using ccxt.krakenfutures directly."""
    print("\n=== Testing CCXT Kraken Futures ===\n")
    
    exchange = ccxt.krakenfutures({
        'apiKey': os.environ['KRAKEN_FUTURES_API_KEY'],
        'secret': os.environ['KRAKEN_FUTURES_API_SECRET'],
        'enableRateLimit': True,
        # 'verbose': True, # Uncomment to see raw requests!
    })
    
    try:
        # 1. Load markets
        print("[1] Loading markets...")
        markets = await exchange.load_markets()
        print(f"   Markets loaded: {len(markets)}")
        
        # Check BTC symbol
        symbol = 'BTC/USD:USD'
        if symbol in markets:
            print(f"   ✅ Found {symbol}")
        
        # 2. Fetch balance (private)
        print("\n[2] Fetching balance...")
        balance = await exchange.fetch_balance()
        print("   ✅ Balance fetched")
        
        # 3. Create Order (Safe - limit far away)
        print("\n[3] Placing safe limit order...")
        price = 50000.0  # Far from ~90k
        amount = 1.0     # 1 contract
        
        # CCXT unifies this: create_order(symbol, type, side, amount, price)
        # We can see what it sends by enabling verbose, but let's just try to clear the hurdle
        try:
            order = await exchange.create_order(
                symbol=symbol,
                type='limit',
                side='buy',
                amount=amount,
                price=price,
                params={'cliOrdId': 'ccxt_test_001'} 
            )
            print(f"   ✅ Order placed! ID: {order['id']}")
            
            # Cancel it
            print("   Cancelling...")
            await exchange.cancel_order(order['id'], symbol)
            print("   ✅ Order cancelled")
            
        except Exception as e:
            print(f"   ❌ Order creation failed: {e}")

    except Exception as e:
        print(f"   ❌ Error: {e}")
    finally:
        await exchange.close()

if __name__ == "__main__":
    asyncio.run(test_ccxt_futures_direct())
