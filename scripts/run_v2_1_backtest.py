import asyncio
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import sys
import os

# Add src to path
sys.path.append(os.getcwd())

from src.config.config import Config, load_config
from src.backtest.backtest_engine import BacktestEngine
from src.data.kraken_client import KrakenClient

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("BacktestRunner")

async def run_backtest():
    """Run V2.1 Backtest."""
    config = load_config("src/config/config.yaml") # Load default config
    
    # Overrides for V2.1 strict testing
    # Using defaults from config.yaml directly

    
    # Run for 6 months
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=180)
    
    logger.info("Initializing Backtest Engine (V2.1)...")
    engine = BacktestEngine(config, symbol="BTC/USD", starting_equity=Decimal("10000"))
    
    # Use real client for data fetching (cached)
    # Using dummy keys for public data access in backtest
    client = KrakenClient("dummy_key", "dummy_secret", use_testnet=True)
    engine.set_client(client)
    
    try:
        metrics = await engine.run(start_date, end_date)
        
        print("\n\n" + "="*50)
        print("V2.1 BACKTEST RESULTS")
        print("="*50)
        print(f"Period: {start_date.date()} to {end_date.date()}")
        print(f"Total Trades: {metrics.total_trades}")
        print(f"Win Rate: {metrics.win_rate:.1f}%")
        print(f"Net PnL: ${metrics.total_pnl:.2f}")
        print(f"Max Drawdown: {metrics.max_drawdown:.2%}")
        print(f"Final Equity: ${engine.current_equity:.2f}")
        print("="*50)
        
        # Check Go-Live Criteria
        success = True
        if metrics.total_trades < 25:
            print("❌ Criteria Fail: Trades < 25")
            success = False
        if metrics.max_drawdown > 0.02:
            print("❌ Criteria Fail: Drawdown > 2%")
            success = False
        if metrics.total_pnl <= 0:
            print("❌ Criteria Fail: Expectancy <= 0")
            success = False
            
        if success:
            print("✅ ALL GO-LIVE CRITERIA MET")
        else:
            print("⚠️ VALIDATION FAILED")
            
    finally:
        await client.close()

if __name__ == "__main__":
    asyncio.run(run_backtest())
