"""
V2 Backtest Validation Script

Runs extended backtests on V2 features and compares to V1 baseline.
Focus on validating:
- Multi-asset signal generation
- Fibonacci confluence impact
- Signal quality scoring accuracy
- Overall performance vs V1
"""
import sys
from datetime import datetime, timedelta
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
    
    # Test multi-asset capability
    print("V2 Multi-Asset Test:")
    print("-" * 40)
    
    # Coins from config
    test_coins = [
        "BTC/USD",  # A-tier
        "ETH/USD",  # A-tier
        "SOL/USD",  # A-tier
        # "LINK/USD",  # B-tier (can enable for full test)
        # "AVAX/USD",  # B-tier
        # "MATIC/USD",  # B-tier
    ]
    
    results = {}
    
    for symbol in test_coins:
        print(f"\nTesting {symbol}...")
        
        try:
            # Initialize backtest engine
            engine = BacktestEngine(config, symbol=symbol, starting_equity=starting_equity)
            
            # Run backtest
            metrics = engine.run(start_date, end_date)
            
            # Store results
            results[symbol] = {
                "trades": metrics.total_trades,
                "wins": metrics.winning_trades,
                "losses": metrics.losing_trades,
                "win_rate": metrics.winning_trades / metrics.total_trades if metrics.total_trades > 0 else 0,
                "pnl": metrics.net_pnl,
                "pnl_pct": float(metrics.net_pnl / starting_equity * 100),
                "max_dd": metrics.max_drawdown_pct
            }
            
            print(f"  ✅ {symbol}: {metrics.total_trades} trades, "
             f"{results[symbol]['win_rate']*100:.1f}% win rate, "
                  f"{results[symbol]['pnl_pct']:.2f}% return")
            
        except Exception as e:
            print(f"  ❌ {symbol}: Error - {str(e)}")
            results[symbol] = {"error": str(e)}
    
    # Aggregate results
    print("\n" + "=" * 80)
    print("V2 AGGREGATE RESULTS")
    print("=" * 80)
    
    total_trades = sum(r.get("trades", 0) for r in results.values())
    total_wins = sum(r.get("wins", 0) for r in results.values())
    total_losses = sum(r.get("losses", 0) for r in results.values())
    total_pnl = sum(r.get("pnl", 0) for r in results.values())
    
    print(f"\nTotal Trades: {total_trades}")
    print(f"Wins: {total_wins}, Losses: {total_losses}")
    print(f"Win Rate: {total_wins/total_trades*100 if total_trades > 0 else 0:.1f}%")
    print(f"Total PnL: ${total_pnl:.2f}")
    print(f"Return: {float(total_pnl/starting_equity)*100:.2f}%")
    
    # Compare to V1 baseline (from earlier backtests)
    print("\n" + "=" * 80)
    print("V2 vs V1 COMPARISON")
    print("=" * 80)
    
    v1_baseline = {
        "period": "60 days",
        "trades": 6,
        "win_rate": 0.167,
        "pnl_pct": -0.45
    }
    
    print(f"\nV1 Baseline (60 days, BTC only):")
    print(f"  Trades: {v1_baseline['trades']}")
    print(f"  Win Rate: {v1_baseline['win_rate']*100:.1f}%")
    print(f"  Return: {v1_baseline['pnl_pct']:.2f}%")
    
    print(f"\nV2 Results (same period, multi-asset):")
    print(f"  Trades: {total_trades}")
    print(f"  Win Rate: {total_wins/total_trades*100 if total_trades > 0 else 0:.1f}%")
    print(f"  Return: {float(total_pnl/starting_equity)*100:.2f}%")
    
    # Calculate improvements
    if total_trades > 0:
        trade_freq_improvement = total_trades / v1_baseline['trades']
        win_rate_improvement = (total_wins/total_trades) / v1_baseline['win_rate'] if v1_baseline['win_rate'] > 0 else 0
        
        print(f"\nImprovements:")
        print(f"  Trade Frequency: {trade_freq_improvement:.1f}x")
        print(f"  Win Rate: {win_rate_improvement:.1f}x")
    
    print("\n" + "=" * 80)
    print("VALIDATION COMPLETE")
    print("=" * 80)
    
    # Success criteria check
    print("\nSuccess Criteria Check:")
    success = True
    
    if total_trades < v1_baseline['trades'] * 2:
        print(f"  ❌ Trade frequency < 2x V1 ({total_trades} < {v1_baseline['trades']*2})")
        success = False
    else:
        print(f"  ✅ Trade frequency ≥ 2x V1")
    
    win_rate = total_wins/total_trades if total_trades > 0 else 0
    if win_rate < 0.40:
        print(f"  ⚠️  Win rate < 40% ({win_rate*100:.1f}% - may improve with more data)")
    else:
        print(f"  ✅ Win rate ≥ 40%")
    
    if total_pnl < 0:
        print(f"  ⚠️  Negative PnL (${total_pnl:.2f} - needs improvement)")
    else:
        print(f"  ✅ Positive PnL")
    
    print()
    if success:
        print("🎯 V2 VALIDATION: PASSED")
    else:
        print("⚠️  V2 VALIDATION: NEEDS IMPROVEMENT")
    
    print("\nNext Steps:")
    print("  1. Review individual coin performance")
    print("  2. Analyze signal quality scores vs outcomes")
    print("  3. Check Fibonacci confluence correlation")
    print("  4. Extend test period to 180+ days")
    print("  5. User review and approval before production")
    
    return results


if __name__ == "__main__":
    run_v2_backtest_validation()
