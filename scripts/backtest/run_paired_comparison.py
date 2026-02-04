"""
Paired Comparison Backtest: 1H Decision (old) vs 4H Decision (new)

Runs identical backtest on same universe and period with different decision timeframes.
Produces side-by-side comparison metrics.

Usage:
  python scripts/backtest/run_paired_comparison.py --mode 4h  # Run 4H decision (new)
  python scripts/backtest/run_paired_comparison.py --mode 1h  # Run 1H decision (old)
  python scripts/backtest/run_paired_comparison.py --mode both  # Run both sequentially
"""
import asyncio
import argparse
import os
import sys
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from src.config.config import load_config
from src.backtest.backtest_engine import BacktestEngine
from src.monitoring.logger import setup_logging, get_logger

logger = get_logger(__name__)

# Output directory for results
RESULTS_DIR = Path(__file__).parent.parent.parent / "data" / "backtest_results"

# Tier A symbols (20 coins) - high liquidity
TIER_A_SYMBOLS = [
    "BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "ADA/USD",
    "AVAX/USD", "DOT/USD", "LINK/USD", "UNI/USD", "LTC/USD",
    "BCH/USD", "ATOM/USD", "NEAR/USD", "APT/USD", "OP/USD",
    # Note: Some symbols may not be available on Kraken spot
    # Excluded: BNB (not on Kraken), TON, TRX, ICP, SUI (may not have spot data)
]

# Tier B symbols (partial - most liquid ones)
TIER_B_SYMBOLS = [
    "ARB/USD", "FIL/USD", "INJ/USD", "AAVE/USD", "SNX/USD",
    "CRV/USD", "LDO/USD", "GRT/USD", "SAND/USD", "MANA/USD",
    "AXS/USD", "GALA/USD", "CHZ/USD", "FLOW/USD", "ALGO/USD",
    "DOGE/USD", "ETC/USD", "HBAR/USD", "XLM/USD", "COMP/USD",
    "BAT/USD", "ZRX/USD", "SUSHI/USD", "DYDX/USD", "RUNE/USD",
    "KAVA/USD", "ENS/USD",
]

# Combined universe for backtest
ALL_SYMBOLS = TIER_A_SYMBOLS + TIER_B_SYMBOLS


async def backtest_symbol(
    symbol: str,
    config,
    start_date: datetime,
    end_date: datetime,
    mode: str,  # "1h" or "4h"
) -> Dict:
    """Run backtest for a single symbol with specified decision mode."""
    engine = BacktestEngine(config, symbol=symbol)
    try:
        metrics = await engine.run(start_date=start_date, end_date=end_date)
        return {
            'symbol': symbol,
            'mode': mode,
            'success': True,
            'trades': metrics.total_trades,
            'winning_trades': metrics.winning_trades,
            'losing_trades': metrics.losing_trades,
            'win_rate': metrics.win_rate,
            'total_pnl': float(metrics.total_pnl),
            'total_fees': float(metrics.total_fees),
            'max_drawdown': float(metrics.max_drawdown),
        }
    except Exception as e:
        error_msg = str(e)
        # Don't spam logs for known issues
        if "does not have market symbol" not in error_msg:
            logger.error(f"Backtest failed for {symbol}: {error_msg}")
        return {
            'symbol': symbol,
            'mode': mode,
            'success': False,
            'error': error_msg[:100],  # Truncate
        }
    finally:
        if getattr(engine, "client", None):
            await engine.client.close()


def configure_for_mode(config, mode: str):
    """
    Configure the strategy for either 1H or 4H decision authority.
    
    4H mode: SMC patterns detected on 4H (current production)
    1H mode: SMC patterns detected on 1H (legacy behavior for comparison)
    """
    # Common settings (match production)
    config.strategy.require_ms_change_confirmation = False
    config.strategy.skip_reconfirmation_in_trends = True
    config.strategy.adx_threshold = 25.0
    config.strategy.entry_zone_tolerance_pct = 0.02
    config.strategy.min_score_tight_smc_aligned = 65.0
    config.strategy.min_score_wide_structure_aligned = 60.0
    
    if mode == "4h":
        # 4H Decision Authority (new)
        config.strategy.decision_timeframes = ["4h"]
        config.strategy.refinement_timeframes = ["1h", "15m"]
        config.strategy.ms_confirmation_candles = 1  # 4 hours on 4H
        config.strategy.tight_smc_atr_stop_min = 0.15  # Halved for 4H ATR
        config.strategy.tight_smc_atr_stop_max = 0.30
        config.strategy.wide_structure_atr_stop_min = 0.50
        config.strategy.wide_structure_atr_stop_max = 0.60
    else:
        # 1H Decision Authority (legacy)
        config.strategy.decision_timeframes = ["1h"]
        config.strategy.refinement_timeframes = ["15m"]
        config.strategy.ms_confirmation_candles = 2  # 2 hours on 1H
        config.strategy.tight_smc_atr_stop_min = 0.30  # Original 1H ATR values
        config.strategy.tight_smc_atr_stop_max = 0.60
        config.strategy.wide_structure_atr_stop_min = 1.00
        config.strategy.wide_structure_atr_stop_max = 1.20
    
    return config


async def run_backtest_suite(mode: str, symbols: List[str], days: int = 90) -> Dict:
    """Run backtest suite for all symbols in specified mode."""
    config = load_config("src/config/config.yaml")
    config = configure_for_mode(config, mode)
    
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=days)
    
    print(f"\n{'='*80}")
    print(f"BACKTEST: {mode.upper()} DECISION AUTHORITY")
    print(f"{'='*80}")
    print(f"Period: {start_date.date()} to {end_date.date()} ({days} days)")
    print(f"Symbols: {len(symbols)}")
    print(f"Decision TF: {config.strategy.decision_timeframes}")
    print(f"Stop multipliers: {config.strategy.tight_smc_atr_stop_min}-{config.strategy.tight_smc_atr_stop_max}")
    print(f"-"*80)
    
    results = []
    failed = 0
    
    for i, symbol in enumerate(symbols, 1):
        print(f"[{i:2d}/{len(symbols)}] {symbol}...", end=" ", flush=True)
        result = await backtest_symbol(symbol, config, start_date, end_date, mode)
        results.append(result)
        
        if result['success']:
            pnl = result['total_pnl']
            trades = result['trades']
            win_rate = result['win_rate']
            print(f"✓ {trades} trades, {win_rate:.0f}% win, ${pnl:+,.2f}")
        else:
            failed += 1
            print(f"✗ {result.get('error', 'Unknown error')[:40]}")
        
        # Rate limit pause
        await asyncio.sleep(1.5)
    
    # Aggregate metrics
    successful = [r for r in results if r['success']]
    
    aggregate = {
        'mode': mode,
        'period_days': days,
        'start_date': start_date.isoformat(),
        'end_date': end_date.isoformat(),
        'total_symbols': len(symbols),
        'successful_symbols': len(successful),
        'failed_symbols': failed,
        'total_trades': sum(r['trades'] for r in successful),
        'total_wins': sum(r['winning_trades'] for r in successful),
        'total_losses': sum(r['losing_trades'] for r in successful),
        'total_pnl': sum(r['total_pnl'] for r in successful),
        'total_fees': sum(r['total_fees'] for r in successful),
        'max_drawdown': max((r['max_drawdown'] for r in successful), default=0),
        'winning_symbols': sum(1 for r in successful if r['total_pnl'] > 0),
        'losing_symbols': sum(1 for r in successful if r['total_pnl'] < 0),
        'per_symbol': results,
    }
    
    # Calculate overall win rate
    if aggregate['total_trades'] > 0:
        aggregate['overall_win_rate'] = aggregate['total_wins'] / aggregate['total_trades'] * 100
    else:
        aggregate['overall_win_rate'] = 0
    
    return aggregate


def print_summary(results: Dict):
    """Print formatted summary of backtest results."""
    print(f"\n{'='*80}")
    print(f"RESULTS: {results['mode'].upper()} DECISION AUTHORITY")
    print(f"{'='*80}")
    print(f"Period: {results['period_days']} days")
    print(f"Symbols: {results['successful_symbols']}/{results['total_symbols']} successful")
    print(f"-"*80)
    print(f"Total Trades:     {results['total_trades']}")
    print(f"Win Rate:         {results['overall_win_rate']:.1f}% ({results['total_wins']}W / {results['total_losses']}L)")
    print(f"Total PnL:        ${results['total_pnl']:,.2f}")
    print(f"Total Fees:       ${results['total_fees']:,.2f}")
    print(f"Max Drawdown:     {results['max_drawdown']:.1%}")
    print(f"Winning Symbols:  {results['winning_symbols']}")
    print(f"Losing Symbols:   {results['losing_symbols']}")
    if results['total_trades'] > 0:
        print(f"Avg PnL/Trade:    ${results['total_pnl']/results['total_trades']:,.2f}")
    print(f"{'='*80}")


def print_comparison(results_4h: Dict, results_1h: Dict):
    """Print side-by-side comparison of both modes."""
    print(f"\n{'='*80}")
    print("PAIRED COMPARISON: 4H vs 1H DECISION AUTHORITY")
    print(f"{'='*80}")
    print(f"{'Metric':<25} {'4H Decision':>20} {'1H Decision':>20} {'Delta':>15}")
    print(f"-"*80)
    
    def delta(a, b, fmt=""):
        diff = a - b
        if fmt == "%":
            return f"{diff:+.1f}%"
        elif fmt == "$":
            return f"${diff:+,.2f}"
        else:
            return f"{diff:+.0f}"
    
    print(f"{'Total Trades':<25} {results_4h['total_trades']:>20} {results_1h['total_trades']:>20} {delta(results_4h['total_trades'], results_1h['total_trades']):>15}")
    print(f"{'Win Rate':<25} {results_4h['overall_win_rate']:>19.1f}% {results_1h['overall_win_rate']:>19.1f}% {delta(results_4h['overall_win_rate'], results_1h['overall_win_rate'], '%'):>15}")
    print(f"{'Total PnL':<25} ${results_4h['total_pnl']:>18,.2f} ${results_1h['total_pnl']:>18,.2f} {delta(results_4h['total_pnl'], results_1h['total_pnl'], '$'):>15}")
    print(f"{'Max Drawdown':<25} {results_4h['max_drawdown']:>19.1%} {results_1h['max_drawdown']:>19.1%} {delta(results_4h['max_drawdown']*100, results_1h['max_drawdown']*100, '%'):>15}")
    print(f"{'Winning Symbols':<25} {results_4h['winning_symbols']:>20} {results_1h['winning_symbols']:>20} {delta(results_4h['winning_symbols'], results_1h['winning_symbols']):>15}")
    print(f"{'Losing Symbols':<25} {results_4h['losing_symbols']:>20} {results_1h['losing_symbols']:>20} {delta(results_4h['losing_symbols'], results_1h['losing_symbols']):>15}")
    
    print(f"-"*80)
    
    # Verdict
    if results_4h['total_pnl'] > results_1h['total_pnl']:
        print(f"VERDICT: 4H Decision Authority outperforms by ${results_4h['total_pnl'] - results_1h['total_pnl']:,.2f}")
    elif results_1h['total_pnl'] > results_4h['total_pnl']:
        print(f"VERDICT: 1H Decision Authority outperforms by ${results_1h['total_pnl'] - results_4h['total_pnl']:,.2f}")
    else:
        print("VERDICT: Both modes performed equally")
    
    print(f"{'='*80}")


def save_results(results: Dict, mode: str):
    """Save results to JSON file."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    filename = RESULTS_DIR / f"backtest_{mode}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(filename, 'w') as f:
        json.dump(results, f, indent=2, default=str)
    print(f"Results saved to: {filename}")
    return filename


async def main():
    parser = argparse.ArgumentParser(description="Paired comparison backtest")
    parser.add_argument("--mode", choices=["1h", "4h", "both"], default="both",
                        help="Decision mode to test")
    parser.add_argument("--days", type=int, default=90, help="Backtest period in days")
    parser.add_argument("--tier", choices=["a", "b", "all"], default="all",
                        help="Symbol tier to test")
    args = parser.parse_args()
    
    setup_logging("INFO", "json")
    
    # Select symbols
    if args.tier == "a":
        symbols = TIER_A_SYMBOLS
    elif args.tier == "b":
        symbols = TIER_B_SYMBOLS
    else:
        symbols = ALL_SYMBOLS
    
    print(f"\n🔬 PAIRED COMPARISON BACKTEST")
    print(f"Mode: {args.mode.upper()}")
    print(f"Days: {args.days}")
    print(f"Symbols: {len(symbols)} ({args.tier.upper()} tier)")
    
    results_4h = None
    results_1h = None
    
    if args.mode in ("4h", "both"):
        results_4h = await run_backtest_suite("4h", symbols, args.days)
        print_summary(results_4h)
        save_results(results_4h, "4h")
    
    if args.mode in ("1h", "both"):
        results_1h = await run_backtest_suite("1h", symbols, args.days)
        print_summary(results_1h)
        save_results(results_1h, "1h")
    
    if results_4h and results_1h:
        print_comparison(results_4h, results_1h)


if __name__ == "__main__":
    asyncio.run(main())
