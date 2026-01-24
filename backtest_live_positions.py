#!/usr/bin/env python3
"""
Backtest the coins from live profitable positions: CHZ, TIA, PENGU.
Run over 30 days to capture potential entry signals.
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
        print(f"\n{'='*80}")
        print(f"Running backtest for {symbol}...")
        print("="*80)

        engine = BacktestEngine(config, symbol=symbol)
        metrics = await engine.run(start_date=start_date, end_date=end_date)

        # Close client properly
        await engine.client.close()

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


async def run_live_positions_backtest():
    """Run backtests for CHZ, TIA, PENGU."""

    # Load config
    config = load_config("src/config/config.yaml")
    setup_logging("INFO", "json")

    print("\n" + "="*80)
    print("BACKTEST: Live Profitable Positions")
    print("="*80)
    print(f"Position Sizing: {config.risk.sizing_method}")
    print(f"Risk per Trade: {config.risk.risk_per_trade_pct * 100}%")
    print(f"Target Leverage: {config.risk.target_leverage}x")
    print(f"Starting Equity: $10,000")
    print("="*80)
    print("\nLive Positions (from Kraken screenshot):")
    print("  CHZ: +63.04% ($6.26) - 10x leverage")
    print("  TIA: +14.05% ($0.25) - 7x leverage")
    print("  PENGU: +17.53% ($0.25) - 7x leverage")
    print("="*80)

    # Set backtest parameters - Last 30 days
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=30)

    print(f"\nPeriod: {start_date.date()} to {end_date.date()}")

    # Symbols from live positions
    symbols = ["CHZ/USD", "TIA/USD", "PENGU/USD"]

    print(f"Symbols: {', '.join(symbols)}")
    print("-"*80)

    # Run backtests sequentially with delays to avoid rate limits
    results = []
    for i, symbol in enumerate(symbols):
        if i > 0:
            print(f"\nWaiting 15 seconds to avoid rate limits...")
            await asyncio.sleep(15)

        result = await backtest_coin(symbol, config, start_date, end_date)
        results.append(result)

        if result['success']:
            m = result['metrics']
            print(f"\n{symbol} Results:")
            print(f"  Total Trades: {m.total_trades}")
            print(f"  Win Rate: {m.win_rate:.1f}%")
            print(f"  Total PnL: ${float(m.total_pnl):,.2f}")
            print(f"  Net PnL: ${float(m.total_pnl - m.total_fees):,.2f}")
            if m.total_trades > 0:
                return_pct = float(m.total_pnl / Decimal("10000") * 100)
                print(f"  Return: {return_pct:.2f}%")
        else:
            print(f"\n{symbol} FAILED: {result['error']}")

    # Summary
    print("\n" + "="*80)
    print("PORTFOLIO SUMMARY")
    print("="*80)

    total_trades = 0
    total_pnl = Decimal(0)
    total_fees = Decimal(0)
    winning_trades = 0
    losing_trades = 0

    for r in results:
        if r['success']:
            m = r['metrics']
            total_trades += m.total_trades
            total_pnl += m.total_pnl
            total_fees += m.total_fees
            winning_trades += m.winning_trades
            losing_trades += m.losing_trades

            net_pnl = float(m.total_pnl - m.total_fees)
            print(f"{r['symbol']:10s}: {m.total_trades:3d} trades | "
                  f"Win Rate: {m.win_rate:5.1f}% | "
                  f"Net PnL: ${net_pnl:8.2f}")
        else:
            print(f"{r['symbol']:10s}: FAILED - {r['error'][:50]}")

    print("-"*80)
    net_pnl = total_pnl - total_fees
    win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0
    return_pct = float(total_pnl / Decimal("10000") * 100)

    print(f"TOTAL:      {total_trades:3d} trades | "
          f"Win Rate: {win_rate:5.1f}% | "
          f"Net PnL: ${float(net_pnl):8.2f} | "
          f"Return: {return_pct:.2f}%")
    print("="*80)

    if total_trades == 0:
        print("\n⚠️  NO SIGNALS GENERATED during backtest period!")
        print("This suggests either:")
        print("  1. The live positions were opened earlier than 30 days ago")
        print("  2. Strategy filters are very selective (which is good for quality)")

    return results


if __name__ == "__main__":
    asyncio.run(run_live_positions_backtest())
