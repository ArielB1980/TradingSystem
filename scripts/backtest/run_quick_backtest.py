"""
Quick backtest for BTC, ETH, SOL over 30 days.
Run this to check if the strategy generates signals.
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from src.config.config import load_config
from src.backtest.backtest_engine import BacktestEngine
from src.monitoring.logger import setup_logging, get_logger

logger = get_logger(__name__)


async def backtest_coin(symbol: str, config, start_date, end_date):
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


async def run_quick_backtest():
    """Run 30-day backtest on BTC, ETH, SOL."""
    
    # Load config
    config = load_config("src/config/config.yaml")
    setup_logging("INFO", "json")
    # =========================================================================
    # SHADOW MODE - VERIFY LAST 12 HOURS (Production v2.3 Config)
    # =========================================================================
    
    # 1. Trend Fixes (v2.3)
    config.strategy.require_ms_change_confirmation = False  # MATCH PROD: Bypass for trend
    config.strategy.skip_reconfirmation_in_trends = True    # MATCH PROD: Enter on confirmation
    
    # 2. Filters (v2.3)
    config.strategy.adx_threshold = 25.0                    # MATCH PROD: ADX > 25
    config.strategy.entry_zone_tolerance_pct = 0.02         # MATCH PROD: 2% tolerance
    
    # 3. Scores (v2.3)
    config.strategy.min_score_tight_smc_aligned = 65.0      # MATCH PROD
    config.strategy.min_score_wide_structure_aligned = 60.0 # MATCH PROD
    
    print(f"\n🔍 SHADOW MODE (Last 12h Verification):")
    print(f"   require_ms_change_confirmation: {config.strategy.require_ms_change_confirmation}")
    print(f"   skip_reconfirmation_in_trends: {getattr(config.strategy, 'skip_reconfirmation_in_trends', 'N/A')}")
    print(f"   entry_zone_tolerance_pct: {config.strategy.entry_zone_tolerance_pct}")
    print(f"   adx_threshold: {config.strategy.adx_threshold}")
    
    # Set backtest parameters - Recent 30 days for verification
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=30)
    
    symbols = ["BTC/USD", "ETH/USD", "SOL/USD"]
    
    print("\n" + "="*80)
    print("EXTENDED BACKTEST - 90 DAYS")
    print("="*80)
    print(f"Period: {start_date.date()} to {end_date.date()}")
    print(f"Symbols: {', '.join(symbols)}")
    print("-"*80)
    
    # Run backtests
    results = []
    for symbol in symbols:
        print(f"\nRunning backtest for {symbol}...")
        result = await backtest_coin(symbol, config, start_date, end_date)
        results.append(result)
        
        if result['success']:
            m = result['metrics']
            print(f"  Total Trades: {m.total_trades}")
            print(f"  Win Rate: {m.win_rate:.1f}%")
            print(f"  Total PnL: ${float(m.total_pnl):,.2f}")
            print(f"  Fees: ${float(m.total_fees):,.2f}")
            return_pct = float(m.total_pnl / Decimal("10000") * 100) if m.total_pnl else 0
            print(f"  Total Return: {return_pct:.2f}%")
            if getattr(m, 'runner_exits', 0) > 0:
                print(f"  Runner exits: {m.runner_exits} (beyond 3R: {m.runner_exits_beyond_3r})")
                print(f"  Runner avg R: {m.runner_avg_r:.2f}, best: {m.runner_max_r:.2f}R")
        else:
            print(f"  FAILED: {result['error']}")
    
    # Summary
    print("\n" + "="*80)
    print("SUMMARY")
    print("="*80)
    
    total_trades = 0
    total_pnl = Decimal(0)
    
    for r in results:
        if r['success']:
            m = r['metrics']
            total_trades += m.total_trades
            total_pnl += m.total_pnl
            print(f"{r['symbol']}: {m.total_trades} trades, ${float(m.total_pnl):,.2f} PnL, {m.win_rate:.1f}% win rate")
        else:
            print(f"{r['symbol']}: FAILED - {r['error']}")
    
    print("-"*80)
    print(f"TOTAL: {total_trades} trades, ${float(total_pnl):,.2f} PnL")
    print("="*80)
    
    if total_trades == 0:
        print("\n⚠️  NO SIGNALS GENERATED during backtest period!")
        print("This suggests the strategy filters may be too strict.")


if __name__ == "__main__":
    asyncio.run(run_quick_backtest())
