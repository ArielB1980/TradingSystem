"""
Run comprehensive 6-month backtest across all coins with full analytics.

Outputs: portfolio metrics, runner distribution, regime breakdown, loss analysis.
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


def _runner_distribution(all_r_multiples: List[float]) -> Dict[str, int]:
    """Bucket runner R-multiples into a distribution."""
    buckets = {"<0R (loss)": 0, "0-1R": 0, "1-2R": 0, "2-3R": 0,
               "3-5R": 0, "5-8R": 0, "8-12R": 0, "12R+": 0}
    for r in all_r_multiples:
        if r < 0:
            buckets["<0R (loss)"] += 1
        elif r < 1:
            buckets["0-1R"] += 1
        elif r < 2:
            buckets["1-2R"] += 1
        elif r < 3:
            buckets["2-3R"] += 1
        elif r < 5:
            buckets["3-5R"] += 1
        elif r < 8:
            buckets["5-8R"] += 1
        elif r < 12:
            buckets["8-12R"] += 1
        else:
            buckets["12R+"] += 1
    return buckets


async def run_full_backtest():
    """Run 6-month backtest across all coins with full analytics."""

    # Check for comparative mode (old TP3 behavior)
    comparative_mode = os.environ.get("BACKTEST_TP3_MODE", "0") == "1"

    config = load_config("src/config/config.yaml")
    setup_logging("INFO", "json")

    if comparative_mode and config.multi_tp:
        config.multi_tp.runner_has_fixed_tp = True
        config.multi_tp.runner_tp_r_multiple = 3.0

    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=180)

    print("\n" + "=" * 80)
    print("MULTI-ASSET BACKTEST - 6 MONTHS")
    if comparative_mode:
        print("  ** COMPARATIVE MODE: TP3 FIXED (old behavior) **")
    else:
        print("  ** RUNNER MODE: no TP3 (trend-following) **")
    print("=" * 80)
    print(f"Period: {start_date.date()} to {end_date.date()}")
    print(f"Max Concurrent Positions: {config.risk.max_concurrent_positions}")

    all_coins = config.coin_universe.get_all_candidates()
    print(f"Total Coins: {len(all_coins)}")
    print("\nRunning backtests...")
    print("-" * 80)

    batch_size = 10
    all_results = []

    for i in range(0, len(all_coins), batch_size):
        batch = all_coins[i:i + batch_size]
        batch_num = i // batch_size + 1
        total_batches = (len(all_coins) - 1) // batch_size + 1
        print(f"Processing batch {batch_num}/{total_batches} ({len(batch)} coins)...")

        tasks = [backtest_single_coin(s, config, start_date, end_date) for s in batch]
        batch_results = await asyncio.gather(*tasks)
        all_results.extend(batch_results)

    # ── Aggregate ──────────────────────────────────────────────────────
    print("\n" + "-" * 80)
    print("Aggregating results...")
    print("-" * 80)

    successful = [r for r in all_results if r['success']]
    failed = [r for r in all_results if not r['success']]

    print(f"Successful: {len(successful)}/{len(all_results)}")
    print(f"Failed: {len(failed)}/{len(all_results)}")

    if failed:
        print("\nFailed coins:")
        for r in failed[:10]:
            print(f"  - {r['symbol']}: {r['error'][:60]}")

    starting_equity = Decimal(str(config.backtest.initial_capital)) if hasattr(config.backtest, 'initial_capital') else Decimal("10000")

    total_trades = 0
    winning_trades = 0
    losing_trades = 0
    total_pnl = Decimal(0)
    total_fees = Decimal(0)
    all_trade_pnls: List[float] = []
    all_runner_r_multiples: List[float] = []
    all_exit_reasons: List[str] = []
    all_holding_hours: List[float] = []
    regime_stats: Dict[str, Dict] = defaultdict(lambda: {'trades': 0, 'pnl': 0.0, 'wins': 0, 'losses': 0})
    side_stats: Dict[str, Dict] = defaultdict(lambda: {'trades': 0, 'pnl': 0.0, 'wins': 0})
    coin_performance = []

    for result in successful:
        m = result['metrics']
        total_trades += m.total_trades
        winning_trades += m.winning_trades
        losing_trades += m.losing_trades
        net_pnl = m.total_pnl - m.total_fees
        total_pnl += net_pnl
        total_fees += m.total_fees

        return_pct = float(net_pnl / starting_equity * 100) if starting_equity > 0 else 0.0

        coin_performance.append({
            'symbol': result['symbol'],
            'trades': m.total_trades,
            'pnl': float(net_pnl),
            'win_rate': m.win_rate if m.total_trades > 0 else 0,
            'return_pct': return_pct,
            'profit_factor': m.profit_factor,
            'sharpe': m.sharpe_ratio,
            'max_drawdown': float(m.max_drawdown),
            'runner_exits': getattr(m, 'runner_exits', 0),
            'runner_avg_r': getattr(m, 'runner_avg_r', 0.0),
            'runner_exits_beyond_3r': getattr(m, 'runner_exits_beyond_3r', 0),
            'runner_max_r': getattr(m, 'runner_max_r', 0.0),
            'max_consec_losses': getattr(m, 'max_consecutive_losses', 0),
            'avg_holding_h': getattr(m, 'avg_holding_hours', 0.0),
            'loss_correlation': getattr(m, 'loss_correlation', 0.0),
        })

        # Collect per-trade data for aggregate analysis
        for pnl_val in m.trade_results:
            all_trade_pnls.append(float(pnl_val))
        all_runner_r_multiples.extend(m.runner_r_multiples)
        all_exit_reasons.extend(m.exit_reasons)

        # Regime + side breakdown
        for i_t in range(len(m.trade_results)):
            regime = m.trade_regimes[i_t] if i_t < len(m.trade_regimes) else 'unknown'
            side = m.trade_sides[i_t] if i_t < len(m.trade_sides) else 'unknown'
            pnl_f = float(m.trade_results[i_t])
            regime_stats[regime]['trades'] += 1
            regime_stats[regime]['pnl'] += pnl_f
            if pnl_f > 0:
                regime_stats[regime]['wins'] += 1
            else:
                regime_stats[regime]['losses'] += 1
            side_stats[side]['trades'] += 1
            side_stats[side]['pnl'] += pnl_f
            if pnl_f > 0:
                side_stats[side]['wins'] += 1

        # Holding times
        if m.trade_entry_times and m.trade_timestamps and len(m.trade_entry_times) == len(m.trade_timestamps):
            for et, xt in zip(m.trade_entry_times, m.trade_timestamps):
                all_holding_hours.append((xt - et).total_seconds() / 3600.0)

    coin_performance.sort(key=lambda x: x['pnl'], reverse=True)

    # ── Aggregate computed metrics ────────────────────────────────────
    win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0
    avg_trade_pnl = (total_pnl / total_trades) if total_trades > 0 else Decimal(0)

    gross_wins = sum(p for p in all_trade_pnls if p > 0)
    gross_losses = abs(sum(p for p in all_trade_pnls if p < 0))
    agg_profit_factor = gross_wins / gross_losses if gross_losses > 0 else float('inf')

    # Max consecutive losses (aggregate)
    max_c_w = max_c_l = cur_w = cur_l = 0
    for p in all_trade_pnls:
        if p > 0:
            cur_w += 1; cur_l = 0
        elif p < 0:
            cur_l += 1; cur_w = 0
        else:
            cur_w = cur_l = 0
        max_c_w = max(max_c_w, cur_w)
        max_c_l = max(max_c_l, cur_l)

    avg_hold_h = sum(all_holding_hours) / len(all_holding_hours) if all_holding_hours else 0
    sorted_hold = sorted(all_holding_hours) if all_holding_hours else []
    median_hold_h = (sorted_hold[len(sorted_hold) // 2] if sorted_hold else 0)

    # ── Print Results ─────────────────────────────────────────────────
    print("\n" + "=" * 80)
    print("BACKTEST RESULTS")
    print("=" * 80)

    print(f"\nPERFORMANCE SUMMARY")
    print("-" * 80)
    print(f"Total Trades:          {total_trades:,}")
    print(f"Winning / Losing:      {winning_trades:,} / {losing_trades:,}")
    print(f"Win Rate:              {win_rate:.1f}%")
    print(f"")
    print(f"Total PnL (net fees):  ${float(total_pnl):>12,.2f}")
    print(f"Total Fees:            ${float(total_fees):>12,.2f}")
    print(f"Average Trade PnL:     ${float(avg_trade_pnl):>12,.2f}")
    print(f"Profit Factor:         {agg_profit_factor:.2f}")
    print(f"")
    print(f"Max Consecutive Wins:  {max_c_w}")
    print(f"Max Consecutive Losses:{max_c_l}")
    print(f"Avg Holding Time:      {avg_hold_h:.1f}h")
    print(f"Median Holding Time:   {median_hold_h:.1f}h")

    # ── Exit Reason Breakdown ─────────────────────────────────────────
    print(f"\n" + "-" * 80)
    print("EXIT REASON BREAKDOWN")
    print("-" * 80)
    reason_counts = defaultdict(int)
    for r in all_exit_reasons:
        reason_counts[r] += 1
    for reason, count in sorted(reason_counts.items(), key=lambda x: -x[1]):
        pct = count / total_trades * 100 if total_trades > 0 else 0
        print(f"  {reason:<25} {count:>5} ({pct:>5.1f}%)")

    # ── Regime Breakdown ──────────────────────────────────────────────
    print(f"\n" + "-" * 80)
    print("PERFORMANCE BY REGIME")
    print("-" * 80)
    print(f"{'Regime':<20}{'Trades':<8}{'Wins':<7}{'Win%':<8}{'PnL':<15}{'Avg PnL'}")
    print("-" * 80)
    for regime in sorted(regime_stats.keys()):
        rs = regime_stats[regime]
        wr = rs['wins'] / rs['trades'] * 100 if rs['trades'] > 0 else 0
        avg = rs['pnl'] / rs['trades'] if rs['trades'] > 0 else 0
        print(f"{regime:<20}{rs['trades']:<8}{rs['wins']:<7}{wr:<8.1f}${rs['pnl']:>12,.2f}  ${avg:>8,.2f}")

    # ── Side Breakdown ────────────────────────────────────────────────
    print(f"\n" + "-" * 80)
    print("PERFORMANCE BY SIDE")
    print("-" * 80)
    for side in sorted(side_stats.keys()):
        ss = side_stats[side]
        wr = ss['wins'] / ss['trades'] * 100 if ss['trades'] > 0 else 0
        avg = ss['pnl'] / ss['trades'] if ss['trades'] > 0 else 0
        print(f"  {side.upper():<10} {ss['trades']:>5} trades, "
              f"{wr:>5.1f}% win, ${ss['pnl']:>12,.2f} PnL, ${avg:>8,.2f} avg")

    # ── Top / Bottom Coins ────────────────────────────────────────────
    print(f"\n" + "-" * 80)
    print("TOP 20 PERFORMING COINS")
    print("-" * 80)
    print(f"{'Rank':<5}{'Symbol':<18}{'Trades':<7}{'PnL':<14}{'Win%':<7}{'PF':<7}{'MaxDD':<8}{'RunnerMax'}")
    print("-" * 80)

    for i, coin in enumerate(coin_performance[:20], 1):
        pf_str = f"{coin['profit_factor']:.1f}" if coin['profit_factor'] < 999 else "inf"
        rm_str = f"{coin['runner_max_r']:.1f}R" if coin['runner_max_r'] > 0 else "-"
        print(f"{i:<5}{coin['symbol']:<18}{coin['trades']:<7}"
              f"${coin['pnl']:>10,.2f}  {coin['win_rate']:>5.1f}%  {pf_str:>5}  "
              f"{coin['max_drawdown']:>5.1f}%  {rm_str:>6}")

    print(f"\n" + "-" * 80)
    print("BOTTOM 10 PERFORMING COINS")
    print("-" * 80)
    print(f"{'Rank':<5}{'Symbol':<18}{'Trades':<7}{'PnL':<14}{'Win%':<7}{'MaxConsecL':<11}{'LossCorr'}")
    print("-" * 80)

    for i, coin in enumerate(coin_performance[-10:], len(coin_performance) - 9):
        lc = f"{coin['loss_correlation']:.2f}"
        print(f"{i:<5}{coin['symbol']:<18}{coin['trades']:<7}"
              f"${coin['pnl']:>10,.2f}  {coin['win_rate']:>5.1f}%  "
              f"{coin['max_consec_losses']:>5}       {lc:>6}")

    # ── Runner Mode Metrics ───────────────────────────────────────────
    total_runner_exits = sum(c.get('runner_exits', 0) for c in coin_performance)
    total_beyond_3r = sum(c.get('runner_exits_beyond_3r', 0) for c in coin_performance)
    runner_avg_rs = [c['runner_avg_r'] for c in coin_performance if c.get('runner_exits', 0) > 0]
    runner_max_rs = [c['runner_max_r'] for c in coin_performance if c.get('runner_max_r', 0) > 0]

    print(f"\n" + "-" * 80)
    print("RUNNER MODE METRICS")
    print("-" * 80)
    print(f"Total runner exits:    {total_runner_exits}")
    print(f"Beyond 3R:             {total_beyond_3r}")
    if runner_avg_rs:
        print(f"Average runner R:      {sum(runner_avg_rs) / len(runner_avg_rs):.2f}")
    if runner_max_rs:
        print(f"Best single runner:    {max(runner_max_rs):.2f}R")

    # R-multiple distribution
    if all_runner_r_multiples:
        dist = _runner_distribution(all_runner_r_multiples)
        print(f"\nRunner R-Multiple Distribution ({len(all_runner_r_multiples)} exits):")
        bar_max = max(dist.values()) if dist.values() else 1
        for bucket, count in dist.items():
            bar = "█" * int(count / max(bar_max, 1) * 30)
            pct = count / len(all_runner_r_multiples) * 100
            print(f"  {bucket:<12} {count:>4} ({pct:>5.1f}%)  {bar}")

    # ── Loss Analysis ─────────────────────────────────────────────────
    losing_trades_pnl = [p for p in all_trade_pnls if p < 0]
    if losing_trades_pnl:
        print(f"\n" + "-" * 80)
        print("LOSS ANALYSIS")
        print("-" * 80)
        print(f"Total losing trades:   {len(losing_trades_pnl)}")
        print(f"Average loss:          ${sum(losing_trades_pnl) / len(losing_trades_pnl):,.2f}")
        print(f"Median loss:           ${sorted(losing_trades_pnl)[len(losing_trades_pnl) // 2]:,.2f}")
        print(f"Worst single loss:     ${min(losing_trades_pnl):,.2f}")
        print(f"Total losses:          ${sum(losing_trades_pnl):,.2f}")

        # Loss buckets
        loss_buckets = {"< $10": 0, "$10-25": 0, "$25-50": 0,
                        "$50-100": 0, "$100-200": 0, "$200+": 0}
        for lp in losing_trades_pnl:
            alp = abs(lp)
            if alp < 10:
                loss_buckets["< $10"] += 1
            elif alp < 25:
                loss_buckets["$10-25"] += 1
            elif alp < 50:
                loss_buckets["$25-50"] += 1
            elif alp < 100:
                loss_buckets["$50-100"] += 1
            elif alp < 200:
                loss_buckets["$100-200"] += 1
            else:
                loss_buckets["$200+"] += 1
        print(f"\nLoss Size Distribution:")
        for bucket, count in loss_buckets.items():
            pct = count / len(losing_trades_pnl) * 100
            bar = "█" * int(count / max(max(loss_buckets.values()), 1) * 25)
            print(f"  {bucket:<12} {count:>4} ({pct:>5.1f}%)  {bar}")

    print("\n" + "=" * 80)

    # ── Save detailed results ─────────────────────────────────────────
    with open('backtest_results_detailed.txt', 'w') as f:
        f.write(f"DETAILED BACKTEST RESULTS\n")
        f.write(f"Period: {start_date.date()} to {end_date.date()}\n")
        f.write(f"Mode: {'TP3 Fixed (comparative)' if comparative_mode else 'Runner (trend-following)'}\n")
        f.write("=" * 100 + "\n\n")

        f.write(f"{'Symbol':<20}{'Trades':<7}{'PnL':>12}  {'Win%':>6}  {'PF':>6}  "
                f"{'MaxDD':>6}  {'RunExits':>8}  {'MaxR':>6}  {'AvgHold':>8}\n")
        f.write("-" * 100 + "\n")
        for coin in coin_performance:
            pf = f"{coin['profit_factor']:.2f}" if coin['profit_factor'] < 999 else "inf"
            rm = f"{coin['runner_max_r']:.1f}R" if coin['runner_max_r'] > 0 else "-"
            f.write(f"{coin['symbol']:<20}{coin['trades']:<7}"
                    f"${coin['pnl']:>10,.2f}  {coin['win_rate']:>5.1f}%  {pf:>6}  "
                    f"{coin['max_drawdown']:>5.1f}%  {coin['runner_exits']:>8}  {rm:>6}  "
                    f"{coin['avg_holding_h']:>6.1f}h\n")

        f.write("\n\nREGIME BREAKDOWN\n" + "-" * 60 + "\n")
        for regime in sorted(regime_stats.keys()):
            rs = regime_stats[regime]
            wr = rs['wins'] / rs['trades'] * 100 if rs['trades'] > 0 else 0
            f.write(f"{regime:<20} {rs['trades']:>5} trades  {wr:>5.1f}% win  ${rs['pnl']:>12,.2f}\n")

    print("\nDetailed results saved to: backtest_results_detailed.txt")

    # Hint for comparative run
    if not comparative_mode:
        print("\nTo compare with old TP3 behavior, run:")
        print("  BACKTEST_TP3_MODE=1 python scripts/backtest/run_full_backtest.py")


if __name__ == "__main__":
    asyncio.run(run_full_backtest())
