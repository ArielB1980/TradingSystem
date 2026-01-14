"""
System test script to verify API connection, data acquisition, and signal processing.

Can be run locally or on the server to verify system health.
"""
import asyncio
import sys
import os
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config.config import load_config
from src.monitoring.logger import setup_logging, get_logger
from src.storage.db import get_db
from src.data.kraken_client import KrakenClient
from src.data.data_acquisition import DataAcquisition
from src.strategy.smc_engine import SMCEngine
from decimal import Decimal

logger = get_logger(__name__)


async def test_database():
    """Test database connection."""
    print("\n" + "="*60)
    print("TEST 1: Database Connection")
    print("="*60)
    
    try:
        from sqlalchemy import text
        import os
        db = get_db()
        database_url = os.getenv("DATABASE_URL", "")
        
        with db.get_session() as session:
            # Use database-specific query
            if database_url.startswith("postgresql"):
                result = session.execute(text("SELECT version();"))
                version = result.fetchone()[0]
                print(f"✅ Database connected: PostgreSQL {version.split(',')[0].split()[-1]}")
            elif database_url.startswith("sqlite"):
                result = session.execute(text("SELECT sqlite_version();"))
                version = result.fetchone()[0]
                print(f"✅ Database connected: SQLite {version}")
            else:
                # Generic test - just try to query
                result = session.execute(text("SELECT 1;"))
                result.fetchone()
                print(f"✅ Database connected: {database_url.split('://')[0] if database_url else 'default'}")
            return True
    except Exception as e:
        print(f"❌ Database connection failed: {e}")
        return False


async def test_kraken_api(config):
    """Test Kraken API connection."""
    print("\n" + "="*60)
    print("TEST 2: Kraken API Connection")
    print("="*60)
    
    try:
        client = KrakenClient(
            api_key=config.exchange.api_key or "",
            api_secret=config.exchange.api_secret or "",
            use_testnet=config.exchange.use_testnet
        )
        
        # Test spot market data (no auth required)
        print("Testing spot market data access...")
        ticker = await client.get_ticker("BTC/USD")
        if ticker:
            print(f"✅ Spot API working - BTC/USD price: ${ticker.get('last', 'N/A')}")
        else:
            print("⚠️  Spot API returned no data")
        
        # Test futures balance (requires auth)
        if config.exchange.futures_api_key:
            print("Testing futures API access...")
            try:
                balance = await client.get_futures_balance()
                if balance:
                    print(f"✅ Futures API working - Balance retrieved")
                else:
                    print("⚠️  Futures API returned no balance data")
            except Exception as e:
                print(f"⚠️  Futures API test failed (may need valid credentials): {e}")
        else:
            print("⚠️  Futures API credentials not configured")
        
        await client.close()
        return True
        
    except Exception as e:
        print(f"❌ Kraken API connection failed: {e}")
        return False


async def test_data_acquisition(config):
    """Test data acquisition for coins."""
    print("\n" + "="*60)
    print("TEST 3: Data Acquisition (Getting Coin Data)")
    print("="*60)
    
    try:
        client = KrakenClient(
            api_key=config.exchange.api_key or "",
            api_secret=config.exchange.api_secret or "",
            use_testnet=config.exchange.use_testnet
        )
        
        data_acq = DataAcquisition(client, config)
        
        # Test getting candles for a few coins
        test_symbols = ["BTC/USD", "ETH/USD"]
        if hasattr(config, 'coin_universe') and config.coin_universe.markets:
            test_symbols = config.coin_universe.markets[:3]  # Test first 3 coins
        
        print(f"Testing data acquisition for {len(test_symbols)} coins...")
        
        success_count = 0
        for symbol in test_symbols:
            try:
                print(f"  Fetching {symbol}...")
                candles = await data_acq.get_candles(symbol, "15m", limit=10)
                if candles and len(candles) > 0:
                    latest = candles[-1]
                    print(f"    ✅ {symbol}: Got {len(candles)} candles, latest: ${latest.close} @ {latest.timestamp}")
                    success_count += 1
                else:
                    print(f"    ⚠️  {symbol}: No candles returned")
            except Exception as e:
                print(f"    ❌ {symbol}: Failed - {e}")
        
        await client.close()
        
        if success_count > 0:
            print(f"\n✅ Data acquisition working - Successfully fetched data for {success_count}/{len(test_symbols)} coins")
            return True
        else:
            print(f"\n❌ Data acquisition failed - No candles retrieved")
            return False
        
    except Exception as e:
        print(f"❌ Data acquisition test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_signal_processing(config):
    """Test signal processing (SMC engine)."""
    print("\n" + "="*60)
    print("TEST 4: Signal Processing (SMC Engine)")
    print("="*60)
    
    try:
        client = KrakenClient(
            api_key=config.exchange.api_key or "",
            api_secret=config.exchange.api_secret or "",
            use_testnet=config.exchange.use_testnet
        )
        
        data_acq = DataAcquisition(client, config)
        smc_engine = SMCEngine(config.strategy)
        
        # Test signal generation for a coin
        test_symbol = "BTC/USD"
        if hasattr(config, 'coin_universe') and config.coin_universe.markets:
            test_symbol = config.coin_universe.markets[0]
        
        print(f"Testing signal generation for {test_symbol}...")
        
        # Get required candles
        print("  Fetching candles...")
        candles_15m = await data_acq.get_candles(test_symbol, "15m", limit=100)
        candles_1h = await data_acq.get_candles(test_symbol, "1h", limit=100)
        candles_4h = await data_acq.get_candles(test_symbol, "4h", limit=100)
        candles_1d = await data_acq.get_candles(test_symbol, "1d", limit=100)
        
        if not all([candles_15m, candles_1h, candles_4h, candles_1d]):
            print(f"  ❌ Missing candle data for signal generation")
            await client.close()
            return False
        
        print(f"  ✅ Got candles: 15m={len(candles_15m)}, 1h={len(candles_1h)}, 4h={len(candles_4h)}, 1d={len(candles_1d)}")
        
        # Generate signal
        print("  Generating signal...")
        signal = smc_engine.generate_signal(
            symbol=test_symbol,
            bias_candles_4h=candles_4h,
            bias_candles_1d=candles_1d,
            exec_candles_15m=candles_15m,
            exec_candles_1h=candles_1h
        )
        
        print(f"  ✅ Signal generated:")
        print(f"     Type: {signal.signal_type.value}")
        print(f"     Entry: ${signal.entry_price}")
        print(f"     Stop: ${signal.stop_loss}")
        print(f"     Regime: {signal.regime}")
        print(f"     Bias: {signal.higher_tf_bias}")
        
        if signal.reasoning:
            print(f"     Reasoning: {signal.reasoning[:100]}...")
        
        await client.close()
        return True
        
    except Exception as e:
        print(f"❌ Signal processing test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


async def run_all_tests():
    """Run all system tests."""
    print("\n" + "="*60)
    print("TRADING SYSTEM HEALTH CHECK")
    print("="*60)
    
    # Load config
    try:
        config_path = os.getenv("CONFIG_PATH", "src/config/config.yaml")
        config = load_config(config_path)
        setup_logging(config.monitoring.log_level, config.monitoring.log_format)
        print(f"✅ Config loaded from {config_path}")
    except Exception as e:
        print(f"❌ Failed to load config: {e}")
        return False
    
    # Run tests
    results = []
    
    results.append(await test_database())
    results.append(await test_kraken_api(config))
    results.append(await test_data_acquisition(config))
    results.append(await test_signal_processing(config))
    
    # Summary
    print("\n" + "="*60)
    print("TEST SUMMARY")
    print("="*60)
    
    test_names = [
        "Database Connection",
        "Kraken API Connection",
        "Data Acquisition",
        "Signal Processing"
    ]
    
    for i, (name, result) in enumerate(zip(test_names, results), 1):
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{i}. {name}: {status}")
    
    all_passed = all(results)
    
    if all_passed:
        print("\n🎉 All tests passed! System is ready for trading.")
    else:
        print("\n⚠️  Some tests failed. Review errors above.")
    
    return all_passed


if __name__ == "__main__":
    success = asyncio.run(run_all_tests())
    sys.exit(0 if success else 1)
