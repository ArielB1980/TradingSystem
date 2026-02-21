"""
Run comprehensive 6-month backtest across all 249 coins with position limits.
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from collections import defaultdict
from typing import List, Dict

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from src.config.config import load_config
from src.backtest.backtest_engine import BacktestEngine
from src.monitoring.logger import setup_logging, get_logger

logger = get_logger(__name__)

async def backtest_single_coin(symbol: str, config, start_date, end_date):
    """Run backtest for a single coin."""
    engine = BacktestEngine(config, symbol=symbol)
    try:
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
    finally:
        if getattr(engine, "client", None):
            await engine.client.close()

async def run_full_backtest():
    """Run 6-month backtest across all coins with position limits."""
    
    # Load config
    config = load_config("src/config/config.yaml")
    setup_logging("INFO", "json")
    
    # Set backtest parameters
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=180)  # 6 months
    
    print("\n" + "="*80)
    print("MULTI-ASSET BACKTEST - 6 MONTHS")
    print("="*80)
    print(f"Period: {start_date.date()} to {end_date.date()}")
    print(f"Max Concurrent Positions: {config.risk.max_concurrent_positions}")
    
    # Get all coins from config
    all_coins = []
    tier_map = {}
    for tier, coins in config.coin_universe.liquidity_tiers.items():
        all_coins.extend(coins)
        for coin in coins:
            tier_map[coin] = tier
    
    print(f"Total Coins: {len(all_coins)}")
    print(f"Tiers: A={len(config.coin_universe.liquidity_tiers['A'])}, "
          f"B={len(config.coin_universe.liquidity_tiers['B'])}, "
          f"C={len(config.coin_universe.liquidity_tiers['C'])}")
    print("\nRunning backtests (this will take several minutes)...")
    print("-"*80)
    
    # Run backtests in batches to avoid overwhelming the system
    batch_size = 10
    all_results = []
    
    for i in range(0, len(all_coins), batch_size):
        batch = all_coins[i:i+batch_size]
        print(f"Processing batch {i//batch_size + 1}/{(len(all_coins)-1)//batch_size + 1} ({len(batch)} coins)...")
        
        tasks = [backtest_single_coin(symbol, config, start_date, end_date) for symbol in batch]
        batch_results = await asyncio.gather(*tasks)
        all_results.extend(batch_results)
    
    # Aggregate results
    print("\n" + "-"*80)
    print("Aggregating results...")
    print("-"*80)
    
    successful = [r for r in all_results if r['success']]
    failed = [r for r in all_results if not r['success']]
    
    print(f"Successful: {len(successful)}/{len(all_results)}")
    print(f"Failed: {len(failed)}/{len(all_results)}")
    
    if failed:
        print("\nFailed coins:")
        for r in failed[:10]:  # Show first 10
            print(f"  - {r['symbol']}: {r['error'][:50]}")
    
    # Calculate aggregate metrics
    total_trades = 0
    winning_trades = 0
    losing_trades = 0
    total_pnl = Decimal(0)
    all_trade_pnls = []
    coin_performance = []
    
    starting_equity = Decimal(str(config.backtest.initial_capital)) if hasattr(config.backtest, 'initial_capital') else Decimal("10000")
    
    for result in successful:
        metrics = result['metrics']
        total_trades += metrics.total_trades
        winning_trades += metrics.winning_trades
        losing_trades += metrics.losing_trades
        net_pnl = metrics.total_pnl - metrics.total_fees
        total_pnl += net_pnl
        
        return_pct = float(net_pnl / starting_equity * 100) if starting_equity > 0 else 0.0
        
        coin_performance.append({
            'symbol': result['symbol'],
            'tier': tier_map.get(result['symbol'], 'Unknown'),
            'trades': metrics.total_trades,
            'pnl': float(net_pnl),
            'win_rate': metrics.win_rate if metrics.total_trades > 0 else 0,
            'return_pct': return_pct,
            'runner_exits': getattr(metrics, 'runner_exits', 0),
            'runner_avg_r': getattr(metrics, 'runner_avg_r', 0.0),
            'runner_exits_beyond_3r': getattr(metrics, 'runner_exits_beyond_3r', 0),
            'runner_max_r': getattr(metrics, 'runner_max_r', 0.0),
            'max_drawdown': float(metrics.max_drawdown),
        })
        
        if metrics.total_trades > 0:
            avg_pnl = net_pnl / metrics.total_trades
            all_trade_pnls.extend([float(avg_pnl)] * metrics.total_trades)
    
    # Sort by PnL
    coin_performance.sort(key=lambda x: x['pnl'], reverse=True)
    
    # Calculate overall metrics
    win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0
    avg_trade_pnl = (total_pnl / total_trades) if total_trades > 0 else Decimal(0)
    
    # Calculate return on initial capital (assuming $10k per coin)
    initial_capital = Decimal(10000) * len(successful)
    total_return_pct = (total_pnl / initial_capital * 100) if initial_capital > 0 else 0
    
    # Print results
    print("\n" + "="*80)
    print("BACKTEST RESULTS")
    print("="*80)
    print(f"\nPERFORMANCE SUMMARY")
    print("-"*80)
    print(f"Total Trades: {total_trades:,}")
    print(f"Winning Trades: {winning_trades:,}")
    print(f"Losing Trades: {losing_trades:,}")
    print(f"Win Rate: {win_rate:.2f}%")
    print(f"\nTotal PnL: ${float(total_pnl):,.2f}")
    print(f"Total Return: {float(total_return_pct):.2f}%")
    print(f"Average Trade PnL: ${float(avg_trade_pnl):,.2f}")
    
    print(f"\n" + "-"*80)
    print("TOP 20 PERFORMING COINS")
    print("-"*80)
    print(f"{'Rank':<6}{'Symbol':<18}{'Tier':<6}{'Trades':<8}{'PnL':<15}{'Win%':<8}{'Return%'}")
    print("-"*80)
    
    for i, coin in enumerate(coin_performance[:20], 1):
        print(f"{i:<6}{coin['symbol']:<18}{coin['tier']:<6}{coin['trades']:<8}"
              f"${coin['pnl']:>12,.2f}{coin['win_rate']:>7.1f}%{coin['return_pct']:>8.1f}%")
    
    print(f"\n" + "-"*80)
    print("BOTTOM 10 PERFORMING COINS")
    print("-"*80)
    print(f"{'Rank':<6}{'Symbol':<18}{'Tier':<6}{'Trades':<8}{'PnL':<15}{'Win%':<8}{'Return%'}")
    print("-"*80)
    
    for i, coin in enumerate(coin_performance[-10:], len(coin_performance)-9):
        print(f"{i:<6}{coin['symbol']:<18}{coin['tier']:<6}{coin['trades']:<8}"
              f"${coin['pnl']:>12,.2f}{coin['win_rate']:>7.1f}%{coin['return_pct']:>8.1f}%")
    
    # Tier breakdown
    print(f"\n" + "-"*80)
    print("PERFORMANCE BY TIER")
    print("-"*80)
    
    tier_stats = defaultdict(lambda: {'trades': 0, 'pnl': 0, 'coins': 0})
    for coin in coin_performance:
        tier = coin['tier']
        tier_stats[tier]['trades'] += coin['trades']
        tier_stats[tier]['pnl'] += coin['pnl']
        tier_stats[tier]['coins'] += 1
    
    for tier in ['A', 'B', 'C']:
        stats = tier_stats[tier]
        avg_pnl = stats['pnl'] / stats['coins'] if stats['coins'] > 0 else 0
        print(f"Tier {tier}: {stats['coins']:>3} coins, {stats['trades']:>5} trades, "
              f"${stats['pnl']:>12,.2f} total, ${avg_pnl:>10,.2f} avg/coin")
    
    # Runner mode metrics
    total_runner_exits = sum(c.get('runner_exits', 0) for c in coin_performance)
    total_beyond_3r = sum(c.get('runner_exits_beyond_3r', 0) for c in coin_performance)
    runner_avg_rs = [c['runner_avg_r'] for c in coin_performance if c.get('runner_exits', 0) > 0]
    runner_max_rs = [c['runner_max_r'] for c in coin_performance if c.get('runner_max_r', 0) > 0]
    max_dd = max((c.get('max_drawdown', 0) for c in coin_performance), default=0)
    
    print(f"\n" + "-"*80)
    print("RUNNER MODE METRICS")
    print("-"*80)
    print(f"Runner exits: {total_runner_exits}")
    print(f"Runner exits beyond 3R: {total_beyond_3r}")
    if runner_avg_rs:
        print(f"Average runner R-multiple: {sum(runner_avg_rs)/len(runner_avg_rs):.2f}")
    if runner_max_rs:
        print(f"Best single runner: {max(runner_max_rs):.2f}R")
    print(f"Max drawdown (worst coin): {max_dd:.2f}%")
    
    print("\n" + "="*80)
    
    # Save detailed results
    with open('backtest_results_detailed.txt', 'w') as f:
        f.write("DETAILED BACKTEST RESULTS\\n")
        f.write("="*80 + "\\n")
        for coin in coin_performance:
            f.write(f"{coin['symbol']:<20} Tier {coin['tier']:<3} "
                   f"Trades: {coin['trades']:<5} PnL: ${coin['pnl']:>12,.2f} "
                   f"Win%: {coin['win_rate']:>6.2f}% Return%: {coin['return_pct']:>7.2f}%\\n")
    
    print("\\nDetailed results saved to: backtest_results_detailed.txt")

if __name__ == "__main__":
    asyncio.run(run_full_backtest())
