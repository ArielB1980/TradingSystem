"""
Comprehensive backtest for 4H decision authority validation.
Runs 90-day backtest on 15+ symbols to validate the new timeframe hierarchy.
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


async def run_comprehensive_backtest():
    """Run 90-day backtest on multiple symbols."""
    
    # Load config
    config = load_config("src/config/config.yaml")
    setup_logging("INFO", "json")
    
    # Match production settings
    config.strategy.require_ms_change_confirmation = False
    config.strategy.skip_reconfirmation_in_trends = True
    config.strategy.adx_threshold = 25.0
    config.strategy.entry_zone_tolerance_pct = 0.02
    config.strategy.min_score_tight_smc_aligned = 65.0
    config.strategy.min_score_wide_structure_aligned = 60.0
    
    print(f"\n📊 COMPREHENSIVE BACKTEST - 4H Decision Authority")
    print(f"="*80)
    print(f"Configuration:")
    print(f"   Decision TF: 4H (SMC patterns, ATR, structure)")
    print(f"   Regime TF: 1D (EMA200 bias)")
    print(f"   Refinement TF: 1H/15m (entry timing, swing points)")
    print(f"   ms_confirmation_candles: {config.strategy.ms_confirmation_candles}")
    print(f"   ADX threshold: {config.strategy.adx_threshold}")
    print(f"   Entry zone tolerance: {config.strategy.entry_zone_tolerance_pct*100}%")
    
    # 90-day backtest
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=90)
    
    # Extended symbol list - major perps available on Kraken Futures
    symbols = [
        # Major coins
        "BTC/USD", "ETH/USD", "SOL/USD",
        # DeFi / L1
        "AVAX/USD", "LINK/USD", "DOT/USD", "ATOM/USD",
        # Altcoins
        "XRP/USD", "ADA/USD", "DOGE/USD", "MATIC/USD",
        # Additional liquidity
        "LTC/USD", "BCH/USD", "UNI/USD", "AAVE/USD",
    ]
    
    print(f"\n📅 Period: {start_date.date()} to {end_date.date()} (90 days)")
    print(f"📈 Symbols: {len(symbols)}")
    print("-"*80)
    
    # Run backtests sequentially to respect rate limits
    results = []
    for i, symbol in enumerate(symbols, 1):
        print(f"\n[{i}/{len(symbols)}] Running backtest for {symbol}...")
        result = await backtest_coin(symbol, config, start_date, end_date)
        results.append(result)
        
        if result['success']:
            m = result['metrics']
            return_pct = float(m.total_pnl / Decimal("10000") * 100) if m.total_pnl else 0
            print(f"   ✅ {m.total_trades} trades | {m.win_rate:.1f}% win | ${float(m.total_pnl):,.2f} PnL ({return_pct:+.2f}%)")
        else:
            print(f"   ❌ FAILED: {result['error'][:50]}...")
        
        # Rate limit pause between symbols
        await asyncio.sleep(2)
    
    # Summary
    print("\n" + "="*80)
    print("COMPREHENSIVE BACKTEST RESULTS - 4H Decision Authority")
    print("="*80)
    
    total_trades = 0
    total_pnl = Decimal(0)
    winning_symbols = 0
    losing_symbols = 0
    total_wins = 0
    total_losses = 0
    
    # Detailed results
    print("\n📊 Per-Symbol Results:")
    print("-"*80)
    print(f"{'Symbol':<12} {'Trades':>8} {'Win%':>8} {'PnL':>12} {'Return':>10}")
    print("-"*80)
    
    for r in results:
        if r['success']:
            m = r['metrics']
            total_trades += m.total_trades
            total_pnl += m.total_pnl
            total_wins += m.winning_trades
            total_losses += m.losing_trades
            
            return_pct = float(m.total_pnl / Decimal("10000") * 100) if m.total_pnl else 0
            pnl_str = f"${float(m.total_pnl):,.2f}"
            
            if m.total_pnl > 0:
                winning_symbols += 1
                status = "🟢"
            elif m.total_pnl < 0:
                losing_symbols += 1
                status = "🔴"
            else:
                status = "⚪"
            
            print(f"{status} {r['symbol']:<10} {m.total_trades:>8} {m.win_rate:>7.1f}% {pnl_str:>12} {return_pct:>+9.2f}%")
        else:
            print(f"❌ {r['symbol']:<10} {'FAILED':>8} {'-':>8} {'-':>12} {'-':>10}")
    
    print("-"*80)
    
    # Aggregate stats
    overall_win_rate = (total_wins / total_trades * 100) if total_trades > 0 else 0
    total_return_pct = float(total_pnl / Decimal("10000") * 100) if total_pnl else 0
    
    print(f"\n📈 AGGREGATE METRICS:")
    print(f"   Total Trades: {total_trades}")
    print(f"   Overall Win Rate: {overall_win_rate:.1f}% ({total_wins}W / {total_losses}L)")
    print(f"   Total PnL: ${float(total_pnl):,.2f}")
    print(f"   Total Return: {total_return_pct:+.2f}%")
    print(f"   Winning Symbols: {winning_symbols}/{len([r for r in results if r['success']])}")
    print(f"   Losing Symbols: {losing_symbols}/{len([r for r in results if r['success']])}")
    
    if total_trades > 0:
        avg_pnl_per_trade = total_pnl / total_trades
        print(f"   Avg PnL/Trade: ${float(avg_pnl_per_trade):,.2f}")
    
    print("\n" + "="*80)
    
    if total_trades == 0:
        print("\n⚠️  NO TRADES EXECUTED during backtest period!")
        print("   This suggests either:")
        print("   - Strategy filters are too strict")
        print("   - 4H structure guard is rejecting all setups")
        print("   - ADX/score thresholds need adjustment")
    elif total_pnl > 0:
        print("\n✅ 4H Decision Authority shows positive results!")
    else:
        print("\n⚠️  4H Decision Authority shows negative results - review needed")


if __name__ == "__main__":
    asyncio.run(run_comprehensive_backtest())
