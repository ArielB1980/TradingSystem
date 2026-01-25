"""Quick test of backtest engine - 7 days of BTC."""
import asyncio
import os
from datetime import datetime, timezone, timedelta
from src.config.config import load_config
from src.data.kraken_client import KrakenClient
from src.backtest.backtest_engine import BacktestEngine
from src.monitoring.logger import setup_logging
from src.storage.db import init_db

# Set credentials
os.environ['KRAKEN_API_KEY'] = 'sIHZanYflTqKAv9dsP0L5Xu+tjR2jFo5xI582NEQ2wAmqIoDIjm70MEq'
os.environ['KRAKEN_API_SECRET'] = 'RIpGuxXd+bfgJPeajbeKrh4FWxxXqjIsmTo3Qvfr5/B9eNJ825xL7I/ddso6rjO2UGIyaHM/ctVtJmadaDsD8A=='

setup_logging("INFO", "text")

async def test_backtest():
    """Test backtest with 7 days."""
    print("Testing backtest engine...")
    
    # Load config
    config = load_config("src/config/config.yaml")
    
    # Override config for backtest
    config.data.database_url = "sqlite:///backtest.db"
    
    init_db(config.data.database_url)
    
    # Initialize client
    client = KrakenClient(
        api_key=os.environ['KRAKEN_API_KEY'],
        api_secret=os.environ['KRAKEN_API_SECRET'],
    )
    
    
    # Configure simulation parameters based on user request
    # User wants to simulate as if balance is larger (~3880) due to leverage/funds
    config.backtest.starting_equity = 3880.0
    
    # Create backtest engine
    engine = BacktestEngine(config, client)
    
    # Run 7-day backtest
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=7)
    
    print(f"\nBacktesting BTC/USD from {start_date.date()} to {end_date.date()}")
    print(f"Starting Equity: ${config.backtest.starting_equity}")
    print(f"Leverage Limit:  {config.risk.max_leverage}x\n")
    
    metrics = await engine.run("BTC/USD", start_date, end_date)
    
    # Display results
    print("\n" + "="*60)
    print("BACKTEST RESULTS (Last 7 Days)")
    print("="*60)
    print(f"Total Trades:      {metrics.total_trades}")
    print(f"Winning Trades:    {metrics.winning_trades}")
    print(f"Losing Trades:     {metrics.losing_trades}")
    print(f"Win Rate:          {metrics.win_rate:.1f}%")
    print(f"Total P&L:         ${metrics.total_pnl:,.2f}")
    print(f"Total Fees:        ${metrics.total_fees:,.2f}")
    print(f"Max Drawdown:      {metrics.max_drawdown:.1%}")
    print(f"Starting Equity:   ${metrics.equity_curve[0]:,.2f}")
    print(f"Final Equity:      ${metrics.equity_curve[-1]:,.2f}")
    print(f"Return:            {((metrics.equity_curve[-1] / metrics.equity_curve[0]) - 1):.1%}")
    print("="*60)

if __name__ == "__main__":
    asyncio.run(test_backtest())
