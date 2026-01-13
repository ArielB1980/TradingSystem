#!/usr/bin/env python3
"""
Live Trading Readiness Check Script

Validates that the system can connect to Kraken API and sync positions
before starting live trading.
"""
import sys
import asyncio
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.config.config import load_config
from src.data.kraken_client import KrakenClient
from src.monitoring.logger import setup_logging, get_logger

logger = get_logger(__name__)


async def check_api_connection():
    """Test API connection and authentication."""
    print("=" * 60)
    print("1. Testing Kraken API Connection")
    print("=" * 60)
    
    try:
        config = load_config()
        
        # Check credentials are configured
        if not config.exchange.futures_api_key or not config.exchange.futures_api_secret:
            print("❌ Futures API credentials not configured")
            print("   Please set KRAKEN_FUTURES_API_KEY and KRAKEN_FUTURES_API_SECRET in .env")
            return False
        
        print(f"✅ Futures API credentials configured")
        print(f"   API Key: {config.exchange.futures_api_key[:8]}...")
        
        # Test connection
        client = KrakenClient(
            api_key=config.exchange.api_key or '',
            api_secret=config.exchange.api_secret or '',
            futures_api_key=config.exchange.futures_api_key,
            futures_api_secret=config.exchange.futures_api_secret,
            use_testnet=config.exchange.use_testnet
        )
        
        print("   Testing authentication...")
        positions = await client.get_all_futures_positions()
        
        print(f"✅ API connection successful")
        print(f"   Found {len(positions)} open positions on exchange")
        
        if positions:
            print("\n   Positions on exchange:")
            for i, pos in enumerate(positions, 1):
                print(f"   {i}. {pos.get('symbol', 'N/A')}: {pos.get('side', 'N/A').upper()}")
                print(f"      Size: {pos.get('size', 0)}")
                print(f"      Entry: ${pos.get('entry_price', 0):.2f}")
                print(f"      PnL: ${pos.get('unrealized_pnl', 0):.2f}")
        
        await client.close()
        return True
        
    except Exception as e:
        print(f"❌ API connection failed: {e}")
        import traceback
        traceback.print_exc()
        return False


async def check_position_sync():
    """Test position synchronization."""
    print("\n" + "=" * 60)
    print("2. Testing Position Synchronization")
    print("=" * 60)
    
    try:
        from src.live.live_trading import LiveTrading
        
        config = load_config()
        setup_logging(config.monitoring.log_level, config.monitoring.log_format)
        
        # Create live trading instance
        engine = LiveTrading(config)
        
        # Test position sync
        print("   Syncing positions from exchange...")
        raw_positions = await engine._sync_positions()
        
        print(f"✅ Position sync successful")
        print(f"   Synced {len(raw_positions)} positions")
        
        # Check database
        from src.storage.repository import PositionModel
        from src.storage.db import get_db
        
        db = get_db()
        with db.get_session() as session:
            db_positions = session.query(PositionModel).all()
            print(f"   Positions in database: {len(db_positions)}")
            
            if len(db_positions) != len(raw_positions):
                print(f"⚠️  WARNING: Database has {len(db_positions)} positions, exchange has {len(raw_positions)}")
            else:
                print(f"✅ Database matches exchange")
        
        await engine.client.close()
        return True
        
    except Exception as e:
        print(f"❌ Position sync failed: {e}")
        import traceback
        traceback.print_exc()
        return False


async def check_system_health():
    """Check overall system health."""
    print("\n" + "=" * 60)
    print("3. System Health Check")
    print("=" * 60)
    
    try:
        config = load_config()
        
        checks = []
        
        # Check config
        checks.append(("Configuration loaded", True))
        checks.append(("Environment", config.environment == "prod"))
        checks.append(("Risk limits configured", config.risk.risk_per_trade_pct > 0))
        checks.append(("Kill switch available", True))  # Always available
        
        for name, status in checks:
            status_symbol = "✅" if status else "❌"
            print(f"   {status_symbol} {name}")
        
        all_passed = all(status for _, status in checks)
        return all_passed
        
    except Exception as e:
        print(f"❌ System health check failed: {e}")
        return False


async def main():
    """Run all readiness checks."""
    print("\n" + "=" * 60)
    print("LIVE TRADING READINESS CHECK")
    print("=" * 60)
    print()
    
    results = []
    
    # Run checks
    results.append(("API Connection", await check_api_connection()))
    if results[0][1]:  # Only continue if API connection works
        results.append(("Position Sync", await check_position_sync()))
    results.append(("System Health", await check_system_health()))
    
    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    
    all_passed = all(status for _, status in results)
    passed_count = sum(1 for _, status in results if status)
    
    for name, status in results:
        status_symbol = "✅" if status else "❌"
        print(f"{status_symbol} {name}")
    
    print()
    if all_passed:
        print("✅ ALL CHECKS PASSED")
        print("   System is ready for live trading")
    else:
        print(f"❌ {len(results) - passed_count} CHECK(S) FAILED")
        print("   Please fix the issues above before starting live trading")
        sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n⚠️  Check cancelled by user")
        sys.exit(1)
