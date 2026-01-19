"""
Quick backtest for BTC, ETH, SOL over 30 days.
Run this to check if the strategy generates signals.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from src.config.config import load_config
from src.backtest.backtest_engine import BacktestEngine
from src.monitoring.logger import setup_logging, get_logger

logger = get_logger(__name__)


async def backtest_coin(symbol: str, config, start_date, end_date):
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


async def run_quick_backtest():
    """Run 30-day backtest on BTC, ETH, SOL."""
    
    # Load config
    config = load_config("src/config/config.yaml")
    setup_logging("INFO", "json")
    
    # =========================================================================
    # BALANCED PRODUCTION CONFIG (v2.2) - Optimized for Quality + Activity
    # =========================================================================
    
    # Core quality gate - KEEP enabled for signal quality
    config.strategy.require_ms_change_confirmation = True
    config.strategy.ms_confirmation_candles = 2  # Faster confirmation (was 3)
    
    # Entry tolerance - allows entries "near" OB/FVG zones
    config.strategy.entry_zone_tolerance_pct = 0.02  # 2% tolerance
    config.strategy.entry_zone_tolerance_adaptive = True  # Scale with ATR
    config.strategy.entry_zone_tolerance_atr_mult = 0.3  # ATR scaling factor
    
    # Score thresholds - slightly relaxed for more candidates
    config.strategy.min_score_tight_smc_aligned = 65.0  # Was 70
    config.strategy.min_score_wide_structure_aligned = 60.0  # Was 65
    
    # REGIME FILTER: Require ADX > threshold for trending markets (skip chop)
    config.strategy.adx_threshold = 25.0  # Minimum ADX for trade approval
    
    # DIAGNOSTIC: Disable confirmation to see raw signal flow
    # This will show us how many setups the strategy identifies
    config.strategy.require_ms_change_confirmation = False
    config.strategy.skip_reconfirmation_in_trends = True
    
    print(f"\n📊 DIAGNOSTIC CONFIG (no confirmation gate):")
    print(f"   require_ms_change_confirmation: {config.strategy.require_ms_change_confirmation}")
    print(f"   ms_confirmation_candles: {config.strategy.ms_confirmation_candles}")
    print(f"   skip_reconfirmation_in_trends: {config.strategy.skip_reconfirmation_in_trends}")
    print(f"   entry_zone_tolerance_pct: {config.strategy.entry_zone_tolerance_pct}")
    print(f"   adx_threshold: {config.strategy.adx_threshold}")
    print(f"   min_score_tight_smc_aligned: {config.strategy.min_score_tight_smc_aligned}")
    print(f"   min_score_wide_structure_aligned: {config.strategy.min_score_wide_structure_aligned}")
    
    # Set backtest parameters - EXTENDED to 90 days for more samples
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=90)
    
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
            return_pct = float(m.total_pnl / Decimal("10000") * 100) if m.total_pnl else 0
            print(f"  Total Return: {return_pct:.2f}%")
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
