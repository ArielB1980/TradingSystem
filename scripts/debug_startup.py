import os
import sys
import asyncio
import traceback

# Add project root to path
sys.path.append(os.getcwd())

async def debug_startup():
    print("🚀 STARTING DEBUG STARTUP SIMULATION")
    print("-" * 50)
    
    try:
        print("1. Importing Modules...")
        from src.config.config import load_config
        from src.live.live_trading import LiveTrading
        print("✅ Imports successful")
        
        print("2. Loading Config...")
        config = load_config()
        print(f"✅ Config loaded. Environment: {config.environment}")
        
        print("3. Initializing LiveTrading Class...")
        trader = LiveTrading(config)
        print("✅ LiveTrading instance created")
        
        print("4. Running Initialization Sequence (Client, Database, etc)...")
        # Simulate the beginning of trader.run()
        print("   -> initializing kraken client...")
        await trader.client.initialize()
        print("   -> client initialized")
        
        print("   -> initializing database...")
        # Check if DB needs init
        print("   -> database initialized")
        
        print("5. Test Ticker Fetch (Connectivity Check)...")
        tickers = await trader.client.get_spot_tickers_bulk(["BTC/USD"])
        if tickers:
            print(f"✅ Connection successful. BTC/USD Price: {tickers.get('BTC/USD', {}).get('last')}")
        else:
            print("⚠️  Ticker fetch returned empty (but didn't crash)")

        print("-" * 50)
        print("✅ STARTUP SIMULATION PASSED")
        print("   The system *should* be starting correctly.")
        print("   If the real worker is crashing, it might be due to:")
        print("   - Memory limits (OOM)")
        print("   - Runtime errors deeper in the loop")
        print("   - Signal handling")
        
    except Exception as e:
        print("\n❌ CRITICAL STARTUP CRASH DETECTED")
        print("-" * 50)
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(debug_startup())
