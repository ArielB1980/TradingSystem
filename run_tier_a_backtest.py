"""
Run 6-month backtest on Tier A coins (20 major assets).
"""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from collections import defaultdict
from src.config.config import load_config
from src.backtest.backtest_engine import BacktestEngine
from src.monitoring.logger import setup_logging, get_logger

logger = get_logger(__name__)

async def backtest_single_coin(symbol: str, config, start_date, end_date):
    """Run backtest for a single coin."""
    try:
        engine = BacktestEngine(config, symbol=symbol)
        metrics = await engine.run(start_date=start_date, end_date=end_date)
        
        return {
            'symbol': symbol,
            'success': True,
            'metrics': metrics
        }
    except Exception as e:
        logger.error(f"Backtest failed for {symbol}: {e}")
        return {
            'symbol': symbol,
            'success': False,
            'error': str(e)
        }

async def run_tier_a_backtest():
    """Run 6-month backtest on Tier A coins."""
    
    # Load config
    config = load_config("src/config/config.yaml")
    setup_logging("INFO", "json")
    
    # Set backtest parameters
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=180)  # 6 months
    
    # Get Tier A coins
    tier_a_coins = config.coin_universe.liquidity_tiers['A']
    
    print("\n" + "="*80)
    print("TIER A BACKTEST - 6 MONTHS (Major Coins)")
    print("="*80)
    print(f"Period: {start_date.date()} to {end_date.date()}")
    print(f"Coins: {len(tier_a_coins)}")
    print(f"Max Concurrent Positions: {config.risk.max_concurrent_positions}")
    print("\nCoins being tested:")
    for i, coin in enumerate(tier_a_coins, 1):
        print(f"  {i:2d}. {coin}")
    print("\n" + "-"*80)
    print("Running backtests (this will take ~30 minutes)...")
    print("-"*80 + "\n")
    
    # Run backtests sequentially to avoid rate limits
    all_results = []
    
    for i, symbol in enumerate(tier_a_coins, 1):
        print(f"[{i}/{len(tier_a_coins)}] Testing {symbol}...", end=" ", flush=True)
        result = await backtest_single_coin(symbol, config, start_date, end_date)
        all_results.append(result)
        
        if result['success']:
            metrics = result['metrics']
            print(f"✓ {metrics.total_trades} trades, PnL: ${float(metrics.total_pnl):,.2f}")
        else:
            print(f"✗ Failed: {result['error'][:50]}")
    
    # Aggregate results
    print("\n" + "="*80)
    print("AGGREGATING RESULTS")
    print("="*80)
    
    successful = [r for r in all_results if r['success']]
    failed = [r for r in all_results if not r['success']]
    
    print(f"Successful: {len(successful)}/{len(all_results)}")
    print(f"Failed: {len(failed)}/{len(all_results)}")
    
    if failed:
        print("\nFailed coins:")
        for r in failed:
            print(f"  - {r['symbol']}: {r['error'][:60]}")
    
    # Calculate aggregate metrics
    total_trades = 0
    winning_trades = 0
    losing_trades = 0
    total_pnl = Decimal(0)
    coin_performance = []
    
    for result in successful:
        metrics = result['metrics']
        total_trades += metrics.total_trades
        winning_trades += metrics.winning_trades
        losing_trades += metrics.losing_trades
        total_pnl += metrics.total_pnl
        
        coin_performance.append({
            'symbol': result['symbol'],
            'trades': metrics.total_trades,
            'pnl': float(metrics.total_pnl),
            'win_rate': metrics.win_rate if metrics.total_trades > 0 else 0,
            'return_pct': float(metrics.total_return_pct),
            'max_dd': float(metrics.max_drawdown_pct)
        })
    
    # Sort by PnL
    coin_performance.sort(key=lambda x: x['pnl'], reverse=True)
    
    # Calculate overall metrics
    win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0
    avg_trade_pnl = (total_pnl / total_trades) if total_trades > 0 else Decimal(0)
    
    # Calculate return on initial capital
    initial_capital = Decimal(10000) * len(successful)
    total_return_pct = (total_pnl / initial_capital * 100) if initial_capital > 0 else 0
    
    # Print results
    print("\n" + "="*80)
    print("TIER A BACKTEST RESULTS - 6 MONTHS")
    print("="*80)
    print(f"\nPERFORMANCE SUMMARY")
    print("-"*80)
    print(f"Total Trades: {total_trades:,}")
    print(f"Winning Trades: {winning_trades:,} ({winning_trades/total_trades*100:.1f}%)" if total_trades > 0 else "Winning Trades: 0")
    print(f"Losing Trades: {losing_trades:,} ({losing_trades/total_trades*100:.1f}%)" if total_trades > 0 else "Losing Trades: 0")
    print(f"Win Rate: {win_rate:.2f}%")
    print(f"\nTotal PnL: ${float(total_pnl):,.2f}")
    print(f"Total Return: {float(total_return_pct):.2f}%")
    print(f"Average Trade PnL: ${float(avg_trade_pnl):,.2f}")
    print(f"Initial Capital: ${float(initial_capital):,.2f} ({len(successful)} coins × $10,000)")
    
    print(f"\n" + "-"*80)
    print("PERFORMANCE BY COIN")
    print("-"*80)
    print(f"{'Rank':<6}{'Symbol':<18}{'Trades':<8}{'PnL':<15}{'Win%':<8}{'Return%':<10}{'Max DD%'}")
    print("-"*80)
    
    for i, coin in enumerate(coin_performance, 1):
        print(f"{i:<6}{coin['symbol']:<18}{coin['trades']:<8}"
              f"${coin['pnl']:>12,.2f}{coin['win_rate']:>7.1f}%"
              f"{coin['return_pct']:>9.1f}%{coin['max_dd']:>8.1f}%")
    
    print("\n" + "="*80)
    
    # Save detailed results
    with open('tier_a_backtest_results.txt', 'w') as f:
        f.write("TIER A BACKTEST RESULTS - 6 MONTHS\n")
        f.write("="*80 + "\n")
        f.write(f"Period: {start_date.date()} to {end_date.date()}\n")
        f.write(f"Coins Tested: {len(tier_a_coins)}\n")
        f.write(f"Successful: {len(successful)}\n")
        f.write(f"\nTotal Trades: {total_trades}\n")
        f.write(f"Win Rate: {win_rate:.2f}%\n")
        f.write(f"Total PnL: ${float(total_pnl):,.2f}\n")
        f.write(f"Total Return: {float(total_return_pct):.2f}%\n\n")
        f.write("-"*80 + "\n")
        f.write("DETAILED RESULTS\n")
        f.write("-"*80 + "\n")
        for coin in coin_performance:
            f.write(f"{coin['symbol']:<20} Trades: {coin['trades']:<5} "
                   f"PnL: ${coin['pnl']:>12,.2f} Win%: {coin['win_rate']:>6.2f}% "
                   f"Return%: {coin['return_pct']:>7.2f}% MaxDD%: {coin['max_dd']:>7.2f}%\n")
    
    print("\nDetailed results saved to: tier_a_backtest_results.txt")
    print("\n" + "="*80)

if __name__ == "__main__":
    asyncio.run(run_tier_a_backtest())
