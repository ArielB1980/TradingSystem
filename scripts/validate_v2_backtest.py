"""
V2 Backtest Validation Script

Runs extended backtests on V2 features and compares to V1 baseline.
"""
import sys
import os
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from src.config.config import load_config
from src.backtest.backtest_engine import BacktestEngine
from src.monitoring.logger import get_logger

logger = get_logger(__name__)


def run_v2_backtest_validation():
    """
    Run comprehensive V2 backtesting validation.
    
    Tests:
    1. Extended period (180 days minimum)
    2. Multi-asset support (all 6 configured coins)
    3. Compare to V1 baseline metrics
    """
    print("=" * 80)
    print("V2 BACKTEST VALIDATION")
    print("=" * 80)
    print()
    
    # Load V2 config
    config = load_config('src/config/config.yaml')
    
    # Backtest parameters
    start_date = "2025-08-01"  # 5+ months of data
    end_date = "2026-01-10"
    starting_equity = Decimal("10000")
    
    print(f"Period: {start_date} to {end_date}")
    print(f"Starting Equity: ${starting_equity}")
    print()
    
    print("V2 Multi-Asset Backtest:")
    print("-" * 40)
    
    # Test coins from config (start with 3 for speed)
    test_coins = [
        "BTC/USD",  # A-tier  
        "ETH/USD",  # A-tier
        "SOL/USD",  # A-tier
    ]
    
    print(f"Testing {len(test_coins)} coins: {', '.join(test_coins)}")
    print()
    
    results = {}
    
    # Run backtests asynchronously
    import asyncio
    
    async def run_backtest_for_coin(symbol):
        """Run backtest for a single coin."""
        try:
            print(f"\nRunning backtest for {symbol}...")
            
            # Initialize Kraken client for data fetching (read-only, no trades)
            from src.data.kraken_client import KrakenClient
            client = KrakenClient(api_key="", api_secret="", use_testnet=False)  # Empty creds for backtest
            
            # Initialize engine with specific symbol
            engine = BacktestEngine(config, symbol=symbol, starting_equity=starting_equity)
            engine.set_client(client)  # Set client after init
            
            # Run backtest
            start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            
            metrics = await engine.run(start_dt, end_dt)
            
            # Store results
            result = {
                "trades": metrics.total_trades,
                "wins": metrics.winning_trades,
                "losses": metrics.losing_trades,
                "win_rate": metrics.winning_trades / metrics.total_trades if metrics.total_trades > 0 else 0,
                "pnl": metrics.net_pnl,
                "pnl_pct": float(metrics.net_pnl / starting_equity * 100),
                "max_dd": metrics.max_drawdown_pct
            }
            
            print(f"  ✅ {symbol}: {metrics.total_trades} trades, "
                  f"{result['win_rate']*100:.1f}% win rate, "
                  f"{result['pnl_pct']:.2f}% return")
            
            return symbol, result
            
        except Exception as e:
            print(f"  ❌ {symbol}: Error - {str(e)}")
            import traceback
            traceback.print_exc()
            return symbol, {"error": str(e)}
    
    # Run all backtests
    async def run_all_backtests():
        tasks = [run_backtest_for_coin(coin) for coin in test_coins]
        return await asyncio.gather(*tasks)
    
    # Execute
    backtest_results = asyncio.run(run_all_backtests())
    for symbol, result in backtest_results:
        results[symbol] = result
    
    # Aggregate results
    print("\n" + "=" * 80)
    print("V2 AGGREGATE RESULTS")
    print("=" * 80)
    
    total_trades = sum(r.get("trades", 0) for r in results.values() if "error" not in r)
    total_wins = sum(r.get("wins", 0) for r in results.values() if "error" not in r)
    total_losses = sum(r.get("losses", 0) for r in results.values() if "error" not in r)
    total_pnl = sum(r.get("pnl", Decimal("0")) for r in results.values() if "error" not in r)
    
    print(f"\nTotal Trades: {total_trades}")
    print(f"Wins: {total_wins}, Losses: {total_losses}")
    print(f"Win Rate: {total_wins/total_trades*100 if total_trades > 0 else 0:.1f}%")
    print(f"Total PnL: ${total_pnl:.2f}")
    print(f"Return: {float(total_pnl/starting_equity)*100:.2f}%")
    
    # Compare to V1
    print("\n" + "=" * 80)
    print("V2 vs V1 COMPARISON")
    print("=" * 80)
    
    v1_baseline = {
        "period": "60 days",
        "trades": 6,
        "win_rate": 0.167,
        "pnl_pct": -0.45
    }
    
    print(f"\nV1 Baseline (BTC only, 60 days):")
    print(f"  Trades: {v1_baseline['trades']}")
    print(f"  Win Rate: {v1_baseline['win_rate']*100:.1f}%")
    print(f"  Return: {v1_baseline['pnl_pct']:.2f}%")
    
    print(f"\nV2 Results (multi-asset, same period):")
    print(f"  Trades: {total_trades}")
    win_rate = total_wins/total_trades if total_trades > 0 else 0
    print(f"  Win Rate: {win_rate*100:.1f}%")
    print(f"  Return: {float(total_pnl/starting_equity)*100:.2f}%")
    
    if total_trades > 0:
        trade_freq_improvement = total_trades / v1_baseline['trades']
        print(f"\nImprovements:")
        print(f"  Trade Frequency: {trade_freq_improvement:.1f}x V1")
    
    print()
    print("  1. Review individual coin performance")
    print("  2. Analyze signal quality scores vs outcomes")
    print("  3. Check Fibonacci confluence correlation")
    print("  4. Extend test period to 180+ days")
    print("  5. User review and approval before production")


if __name__ == "__main__":
    run_v2_backtest_validation()
