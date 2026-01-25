"""
Test CCXT Kraken Futures Margin Mode.
"""
import os
import asyncio
import ccxt.async_support as ccxt
from src.monitoring.logger import setup_logging

# Set credentials
os.environ['KRAKEN_FUTURES_API_KEY'] = 'uG8IoCO8CLLIIghlZVIMWoM5nbBKscc3wlJDZEMIKW4A+Cmf+fuSB+Oy'
os.environ['KRAKEN_FUTURES_API_SECRET'] = 'MoBA5A7X1269Jv81zr+ur551GZe/nA7d5PasKu8L4M0dloy+hogmKKKePAWkBqfvxgpMEfoHpYxYFVUao010yyMb'

setup_logging("INFO", "text")

async def test_margin_mode():
    print("\n=== Testing Margin Mode Setting ===\n")
    
    exchange = ccxt.krakenfutures({
        'apiKey': os.environ['KRAKEN_FUTURES_API_KEY'],
        'secret': os.environ['KRAKEN_FUTURES_API_SECRET'],
        'enableRateLimit': True,
    })
    
    try:
        symbol = 'PF_XBTUSD' # or 'BTC/USD:USD'
        margin_mode = 'isolated'
        leverage = 10
        
        print(f"Setting {symbol} to {margin_mode} margin with {leverage}x leverage...")
        
        # Try set_margin_mode
        if hasattr(exchange, 'set_margin_mode'):
            # Note: params might be needed for leverage
            try:
                # Some exchanges verify this via set_margin_mode
                await exchange.set_margin_mode(margin_mode, symbol, params={'leverage': leverage})
                print("✅ set_margin_mode() called successfully")
            except Exception as e:
                print(f"❌ set_margin_mode() failed: {e}")
                
                # Check set_leverage just in case
                print("Trying set_leverage()...")
                await exchange.set_leverage(leverage, symbol)
                print("✅ set_leverage() called successfully")
        else:
            print("⚠️ Exchange does not have set_margin_mode")

    except Exception as e:
        print(f"❌ Error: {e}")
    finally:
        await exchange.close()

if __name__ == "__main__":
    asyncio.run(test_margin_mode())
