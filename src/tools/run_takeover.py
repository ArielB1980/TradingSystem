"""
Production Takeover Script.

Runs the Production Takeover Protocol to stabilize all open positions.

USAGE:
    python -m src.tools.run_takeover

REQUIREMENTS:
    - Kraken API credentials in environment
    - No other bots running (stop LiveTrading first)
"""
import asyncio
import os
import sys
from decimal import Decimal

# Ensure src is in path
sys.path.append(os.getcwd())

from src.exceptions import OperationalError, DataError
from src.config.config import load_config
from src.data.kraken_client import KrakenClient
from src.execution.execution_gateway import ExecutionGateway
from src.execution.production_takeover import ProductionTakeover, TakeoverConfig
from src.monitoring.logger import get_logger

logger = get_logger(__name__)

async def main():
    logger.critical("PRODUCTION TAKEOVER SCRIPT INITIATED")
    
    # 1. Load Configuration (for credentials)
    try:
        config = load_config()
        logger.info("Configuration loaded")
    except (OperationalError, DataError, OSError, ValueError, TypeError, KeyError) as e:
        logger.critical("Failed to load config", error=str(e), error_type=type(e).__name__)
        return
    
    # 2. Initialize Client
    try:
        client = KrakenClient(
            api_key=config.exchange.api_key,
            api_secret=config.exchange.api_secret,
            futures_api_key=config.exchange.futures_api_key,
            futures_api_secret=config.exchange.futures_api_secret,
            use_testnet=config.exchange.use_testnet
        )
        await client.initialize()
        logger.info("Kraken Client Initialized")
    except (OperationalError, DataError, OSError) as e:
        logger.critical("Failed to init Kraken Client", error=str(e), error_type=type(e).__name__)
        return

    # 3. Initialize Gateway
    # Note: Gateway creates its own PositionManager/Registry/Persistence
    # We want these fresh for the takeover
    gateway = ExecutionGateway(client)
    
    # 4. Configure Takeover
    takeover_config = TakeoverConfig(
        takeover_stop_pct=Decimal("0.02"), # 2% default
        stop_replace_atomically=True,
        dry_run=os.environ.get("TAKEOVER_DRY_RUN", "false").lower() == "true"
    )
    
    # 5. Run Takeover
    takeover = ProductionTakeover(gateway, takeover_config)
    
    try:
        stats = await takeover.execute_takeover()
        
        # 6. Output Instructions
        logger.critical("TAKEOVER COMPLETED.")
        print("\n" + "="*50)
        print("PRODUCTION TAKEOVER COMPLETE")
        print("="*50)
        print(f"Positions Imported: {stats['imported']}/{stats['total_positions']}")
        print(f"Stops Placed:     {stats['stops_placed']}")
        print(f"Quarantined:      {stats['quarantined']}")
        print("-" * 20)
        print("Detailed Breakdown:")
        print(f"  Case A (Protected): {stats.get('case_a', 0)}")
        print(f"  Case B (Naked):     {stats.get('case_b', 0)}")
        print(f"  Case C (Chaos):     {stats.get('case_c', 0)}")
        print(f"  Case D (Duplicate): {stats.get('case_d', 0)}")
        print("-" * 50)
        print("NEXT STEPS:")
        print("1. Set environment variables for Safe Mode:")
        print("   export TRADING_NEW_ENTRIES_ENABLED=false")
        print("   export TRADING_REVERSALS_ENABLED=false")
        print("   export TRADING_PARTIALS_ENABLED=false")
        print("   export TRADING_TRAILING_ENABLED=false")
        print("   export USE_STATE_MACHINE_V2=true")
        print("   export CONFIRM_LIVE=YES")
        print("\n2. Start LiveTrading bot:")
        print("   WITH_HEALTH=1 python -m src.entrypoints.prod_live")
        print("="*50)
        
    finally:
        await client.close()

if __name__ == "__main__":
    asyncio.run(main())
