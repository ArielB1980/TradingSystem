"""
Portfolio Backtest: Full Universe with Concurrent Position Tracking

Runs a synchronized multi-symbol backtest to measure:
- Max concurrent positions actually filled
- PnL per unit of drawdown (Calmar ratio)
- Correlation of losses across positions

Usage:
  python scripts/backtest/run_portfolio_backtest.py --months 6
  python scripts/backtest/run_portfolio_backtest.py --months 12 --tier all
"""
import asyncio
import argparse
import os
import sys
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from collections import defaultdict

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from src.config.config import load_config
from src.backtest.backtest_engine import BacktestEngine, BacktestMetrics
from src.monitoring.logger import setup_logging, get_logger

logger = get_logger(__name__)

# Output directory for results
RESULTS_DIR = Path(__file__).parent.parent.parent / "data" / "backtest_results"


@dataclass
class PortfolioMetrics:
    """Aggregate portfolio-level metrics."""
    # Basic metrics
    total_symbols: int = 0
    successful_symbols: int = 0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: Decimal = Decimal("0")
    total_fees: Decimal = Decimal("0")
    
    # Risk metrics
    max_drawdown: Decimal = Decimal("0")
    calmar_ratio: float = 0.0
    
    # Portfolio metrics
    max_concurrent_positions: int = 0
    avg_concurrent_positions: float = 0.0
    
    # Loss analysis
    loss_correlation: float = 0.0
    max_consecutive_losses: int = 0
    loss_streak_dates: List[str] = field(default_factory=list)
    
    # Per-symbol breakdown
    symbol_results: Dict = field(default_factory=dict)
    
    # Time series for analysis
    all_trade_results: List[Tuple[datetime, str, Decimal]] = field(default_factory=list)
    concurrent_positions_series: List[Tuple[datetime, int]] = field(default_factory=list)
    
    def calculate_derived_metrics(self):
        """Calculate derived metrics from raw data."""
        if self.total_trades > 0:
            self.win_rate = (self.winning_trades / self.total_trades) * 100
        else:
            self.win_rate = 0.0
        
        # Calmar ratio
        if self.max_drawdown > 0:
            self.calmar_ratio = float(self.total_pnl) / (float(self.max_drawdown) * 100)
        elif float(self.total_pnl) > 0:
            self.calmar_ratio = float("inf")
        
        # Loss correlation across portfolio
        self._calculate_portfolio_loss_correlation()
        
        # Max consecutive losses
        self._calculate_max_consecutive_losses()
        
        # Avg concurrent positions
        if self.concurrent_positions_series:
            self.avg_concurrent_positions = sum(c[1] for c in self.concurrent_positions_series) / len(self.concurrent_positions_series)
    
    def _calculate_portfolio_loss_correlation(self):
        """Calculate correlation of losses across the portfolio timeline."""
        if len(self.all_trade_results) < 3:
            self.loss_correlation = 0.0
            return
        
        # Sort by timestamp
        sorted_trades = sorted(self.all_trade_results, key=lambda x: x[0])
        
        # Create binary loss sequence
        loss_sequence = [1 if t[2] < 0 else 0 for t in sorted_trades]
        
        n = len(loss_sequence)
        mean = sum(loss_sequence) / n
        
        if mean == 0 or mean == 1:
            self.loss_correlation = 0.0
            return
        
        variance = sum((x - mean) ** 2 for x in loss_sequence) / n
        if variance == 0:
            self.loss_correlation = 0.0
            return
        
        covariance = sum((loss_sequence[i] - mean) * (loss_sequence[i+1] - mean) for i in range(n-1)) / (n-1)
        self.loss_correlation = covariance / variance
    
    def _calculate_max_consecutive_losses(self):
        """Find longest losing streak."""
        if not self.all_trade_results:
            return
        
        sorted_trades = sorted(self.all_trade_results, key=lambda x: x[0])
        
        max_streak = 0
        current_streak = 0
        streak_start = None
        
        for ts, symbol, pnl in sorted_trades:
            if pnl < 0:
                if current_streak == 0:
                    streak_start = ts
                current_streak += 1
                if current_streak > max_streak:
                    max_streak = current_streak
                    if streak_start:
                        self.loss_streak_dates = [streak_start.isoformat()]
            else:
                current_streak = 0
        
        self.max_consecutive_losses = max_streak


# Tier A symbols (high liquidity)
TIER_A_SYMBOLS = [
    "BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "ADA/USD",
    "AVAX/USD", "DOT/USD", "LINK/USD", "UNI/USD", "LTC/USD",
    "BCH/USD", "ATOM/USD", "NEAR/USD", "APT/USD", "OP/USD",
]

# Tier B symbols (medium liquidity)
TIER_B_SYMBOLS = [
    "ARB/USD", "FIL/USD", "INJ/USD", "AAVE/USD", "SNX/USD",
    "CRV/USD", "LDO/USD", "GRT/USD", "SAND/USD", "MANA/USD",
    "AXS/USD", "GALA/USD", "CHZ/USD", "FLOW/USD", "ALGO/USD",
    "DOGE/USD", "ETC/USD", "HBAR/USD", "XLM/USD", "COMP/USD",
    "BAT/USD", "ZRX/USD", "SUSHI/USD", "DYDX/USD", "RUNE/USD",
    "KAVA/USD", "ENS/USD",
]


async def run_symbol_backtest(
    symbol: str,
    config,
    start_date: datetime,
    end_date: datetime,
) -> Optional[BacktestMetrics]:
    """Run backtest for a single symbol."""
    engine = BacktestEngine(config, symbol=symbol)
    try:
        metrics = await engine.run(start_date=start_date, end_date=end_date)
        return metrics
    except Exception as e:
        error_msg = str(e)
        if "does not have market symbol" not in error_msg:
            logger.error(f"Backtest failed for {symbol}: {error_msg}")
        return None
    finally:
        if getattr(engine, "client", None):
            await engine.client.close()


def aggregate_results(
    all_results: Dict[str, BacktestMetrics],
    start_date: datetime,
    end_date: datetime,
) -> PortfolioMetrics:
    """
    Aggregate individual symbol results into portfolio metrics.
    
    Simulates portfolio-level behavior by merging trade timelines.
    """
    portfolio = PortfolioMetrics()
    portfolio.total_symbols = len(all_results)
    portfolio.successful_symbols = sum(1 for m in all_results.values() if m is not None)
    
    # Aggregate basic metrics
    for symbol, metrics in all_results.items():
        if metrics is None:
            continue
        
        portfolio.total_trades += metrics.total_trades
        portfolio.winning_trades += metrics.winning_trades
        portfolio.losing_trades += metrics.losing_trades
        portfolio.total_pnl += metrics.total_pnl
        portfolio.total_fees += metrics.total_fees
        
        # Track max drawdown (simplified - take max across symbols)
        if metrics.max_drawdown > portfolio.max_drawdown:
            portfolio.max_drawdown = metrics.max_drawdown
        
        # Collect trade results with timestamps
        for i, result in enumerate(metrics.trade_results):
            ts = metrics.trade_timestamps[i] if i < len(metrics.trade_timestamps) else datetime.now(timezone.utc)
            sym = metrics.trade_symbols[i] if i < len(metrics.trade_symbols) else symbol
            portfolio.all_trade_results.append((ts, sym, result))
        
        # Store symbol breakdown
        portfolio.symbol_results[symbol] = {
            'trades': metrics.total_trades,
            'win_rate': metrics.win_rate,
            'pnl': float(metrics.total_pnl),
            'max_dd': float(metrics.max_drawdown),
            'calmar': metrics.calmar_ratio,
        }
    
    # Calculate concurrent positions over time
    # This is a simplified simulation - track position opens/closes
    position_events = []  # (timestamp, delta: +1 or -1)
    
    for symbol, metrics in all_results.items():
        if metrics is None:
            continue
        
        # For each trade, we have an open and close
        # Simplified: assume trade opens at first timestamp, closes at recorded timestamp
        for i, close_ts in enumerate(metrics.trade_timestamps):
            # Estimate open time (assume avg 24h hold for 4H strategy)
            open_ts = close_ts - timedelta(hours=24)
            position_events.append((open_ts, 1, symbol))
            position_events.append((close_ts, -1, symbol))
    
    # Sort events and calculate concurrent positions
    position_events.sort(key=lambda x: x[0])
    current_positions = 0
    max_positions = 0
    
    for ts, delta, sym in position_events:
        current_positions += delta
        current_positions = max(0, current_positions)  # Can't go negative
        if current_positions > max_positions:
            max_positions = current_positions
        portfolio.concurrent_positions_series.append((ts, current_positions))
    
    portfolio.max_concurrent_positions = max_positions
    
    # Calculate derived metrics
    portfolio.calculate_derived_metrics()
    
    return portfolio


async def run_portfolio_backtest(months: int, tier: str) -> PortfolioMetrics:
    """Run full portfolio backtest."""
    config = load_config("src/config/config.yaml")
    
    # Verify 4H decision authority is locked
    decision_tf = config.strategy.decision_timeframes[0] if config.strategy.decision_timeframes else "4h"
    if decision_tf != "4h":
        logger.warning(f"Decision timeframe is {decision_tf}, expected 4h")
    
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=months * 30)
    
    # Select symbols
    if tier == "a":
        symbols = TIER_A_SYMBOLS
    elif tier == "b":
        symbols = TIER_B_SYMBOLS
    else:
        symbols = TIER_A_SYMBOLS + TIER_B_SYMBOLS
    
    print(f"\n{'='*80}")
    print(f"PORTFOLIO BACKTEST - 4H DECISION AUTHORITY (LOCKED)")
    print(f"{'='*80}")
    print(f"Period: {start_date.date()} to {end_date.date()} ({months} months)")
    print(f"Symbols: {len(symbols)} ({tier.upper()} tier)")
    print(f"Decision TF: {decision_tf}")
    print(f"-"*80)
    
    all_results = {}
    
    for i, symbol in enumerate(symbols, 1):
        print(f"[{i:2d}/{len(symbols)}] {symbol}...", end=" ", flush=True)
        metrics = await run_symbol_backtest(symbol, config, start_date, end_date)
        all_results[symbol] = metrics
        
        if metrics:
            pnl = float(metrics.total_pnl)
            trades = metrics.total_trades
            win_rate = metrics.win_rate
            calmar = metrics.calmar_ratio
            print(f"✓ {trades} trades, {win_rate:.0f}% win, ${pnl:+,.2f}, Calmar: {calmar:.2f}")
        else:
            print("✗ Failed")
        
        # Rate limit pause
        await asyncio.sleep(1.5)
    
    # Aggregate results
    portfolio = aggregate_results(all_results, start_date, end_date)
    
    return portfolio


def print_portfolio_summary(portfolio: PortfolioMetrics, months: int):
    """Print formatted portfolio summary."""
    print(f"\n{'='*80}")
    print(f"PORTFOLIO RESULTS - {months} MONTHS")
    print(f"{'='*80}")
    print(f"Symbols: {portfolio.successful_symbols}/{portfolio.total_symbols} successful")
    print(f"-"*80)
    
    print(f"\n📊 BASIC METRICS")
    print(f"  Total Trades:         {portfolio.total_trades}")
    print(f"  Win Rate:             {portfolio.win_rate:.1f}% ({portfolio.winning_trades}W / {portfolio.losing_trades}L)")
    print(f"  Total PnL:            ${float(portfolio.total_pnl):,.2f}")
    print(f"  Total Fees:           ${float(portfolio.total_fees):,.2f}")
    if portfolio.total_trades > 0:
        print(f"  Avg PnL/Trade:        ${float(portfolio.total_pnl)/portfolio.total_trades:,.2f}")
    
    print(f"\n📈 RISK METRICS")
    print(f"  Max Drawdown:         {float(portfolio.max_drawdown):.1%}")
    print(f"  Calmar Ratio:         {portfolio.calmar_ratio:.2f} (PnL / MaxDD)")
    
    print(f"\n🎯 PORTFOLIO METRICS")
    print(f"  Max Concurrent Pos:   {portfolio.max_concurrent_positions}")
    print(f"  Avg Concurrent Pos:   {portfolio.avg_concurrent_positions:.1f}")
    
    print(f"\n⚠️ LOSS ANALYSIS")
    print(f"  Loss Correlation:     {portfolio.loss_correlation:.3f}")
    print(f"    (>0.3 = clustered losses, <-0.1 = alternating, ~0 = independent)")
    print(f"  Max Consecutive Loss: {portfolio.max_consecutive_losses}")
    if portfolio.loss_streak_dates:
        print(f"  Worst Streak Started: {portfolio.loss_streak_dates[0][:10]}")
    
    # Interpretation
    print(f"\n📋 INTERPRETATION")
    if portfolio.calmar_ratio > 2:
        print(f"  ✅ Calmar > 2: Excellent risk-adjusted returns")
    elif portfolio.calmar_ratio > 1:
        print(f"  ✓ Calmar > 1: Acceptable risk-adjusted returns")
    else:
        print(f"  ⚠️ Calmar < 1: Returns not justifying drawdown risk")
    
    if portfolio.loss_correlation > 0.3:
        print(f"  ⚠️ High loss correlation: Losses tend to cluster (systemic risk)")
    elif portfolio.loss_correlation < -0.1:
        print(f"  ✅ Negative loss correlation: Strategy recovers well from losses")
    else:
        print(f"  ✓ Neutral loss correlation: Losses are independent")
    
    if portfolio.max_concurrent_positions > 20:
        print(f"  ✅ High position utilization: Strategy generates sufficient signals")
    elif portfolio.max_concurrent_positions < 5:
        print(f"  ⚠️ Low position utilization: May need more symbols or relaxed filters")
    
    print(f"{'='*80}")
    
    # Top performers
    print(f"\n🏆 TOP 5 PERFORMERS")
    sorted_symbols = sorted(
        [(s, r) for s, r in portfolio.symbol_results.items() if r['trades'] > 0],
        key=lambda x: x[1]['pnl'],
        reverse=True
    )[:5]
    for symbol, result in sorted_symbols:
        print(f"  {symbol:<12} ${result['pnl']:>10,.2f}  ({result['trades']} trades, {result['win_rate']:.0f}% win)")
    
    # Worst performers
    print(f"\n💀 BOTTOM 5 PERFORMERS")
    worst = sorted(
        [(s, r) for s, r in portfolio.symbol_results.items() if r['trades'] > 0],
        key=lambda x: x[1]['pnl']
    )[:5]
    for symbol, result in worst:
        print(f"  {symbol:<12} ${result['pnl']:>10,.2f}  ({result['trades']} trades, {result['win_rate']:.0f}% win)")


def save_results(portfolio: PortfolioMetrics, months: int):
    """Save results to JSON file."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    
    # Convert to serializable format
    data = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'months': months,
        'total_symbols': portfolio.total_symbols,
        'successful_symbols': portfolio.successful_symbols,
        'total_trades': portfolio.total_trades,
        'winning_trades': portfolio.winning_trades,
        'losing_trades': portfolio.losing_trades,
        'win_rate': portfolio.win_rate,
        'total_pnl': float(portfolio.total_pnl),
        'total_fees': float(portfolio.total_fees),
        'max_drawdown': float(portfolio.max_drawdown),
        'calmar_ratio': portfolio.calmar_ratio,
        'max_concurrent_positions': portfolio.max_concurrent_positions,
        'avg_concurrent_positions': portfolio.avg_concurrent_positions,
        'loss_correlation': portfolio.loss_correlation,
        'max_consecutive_losses': portfolio.max_consecutive_losses,
        'symbol_results': portfolio.symbol_results,
    }
    
    filename = RESULTS_DIR / f"portfolio_backtest_{months}mo_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(filename, 'w') as f:
        json.dump(data, f, indent=2, default=str)
    print(f"\nResults saved to: {filename}")
    return filename


async def main():
    parser = argparse.ArgumentParser(description="Portfolio backtest with concurrent position tracking")
    parser.add_argument("--months", type=int, default=6, help="Backtest period in months (default: 6)")
    parser.add_argument("--tier", choices=["a", "b", "all"], default="all",
                        help="Symbol tier to test (default: all)")
    args = parser.parse_args()
    
    setup_logging("INFO", "json")
    
    print(f"\n🔬 PORTFOLIO BACKTEST")
    print(f"Months: {args.months}")
    print(f"Tier: {args.tier.upper()}")
    
    portfolio = await run_portfolio_backtest(args.months, args.tier)
    print_portfolio_summary(portfolio, args.months)
    save_results(portfolio, args.months)


if __name__ == "__main__":
    asyncio.run(main())
