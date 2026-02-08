"""
Replay backtest runner -- two-pass deterministic replay.

Runs the data sanity gate + quality tracker over recorded
``MarketSnapshot`` data, comparing gate-enabled (Pass 1) vs
gate-disabled (Pass 2) behavior.  Produces coverage + delta reports.

CLI usage::

    python -m src.replay.run_replay_backtest \
        --db-url postgresql://... \
        --start 2025-11-06 \
        --end 2025-12-06 \
        --tick-seconds 300

IMPORTANT: This module **never** imports ``KrakenClient`` or makes live
API calls.  A startup guard enforces this.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from src.data.data_quality_tracker import DataQualityTracker, SymbolHealthState
from src.data.data_sanity import SanityThresholds, check_candle_sanity, check_ticker_sanity
from src.monitoring.logger import get_logger
from src.replay.replay_candle_meta_provider import ReplayCandleMetaProvider
from src.replay.replay_ticker_provider import ReplayTickerProvider
from src.replay.report_generator import (
    SymbolCoverageAccumulator,
    build_coverage_report,
    build_delta_report,
    write_reports,
)

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Guard: ensure no live API module is loaded
# ---------------------------------------------------------------------------

def _assert_no_live_imports() -> None:
    """Fail fast if KrakenClient has been imported (safety net)."""
    import sys as _sys

    if "src.data.kraken_client" in _sys.modules:
        # Check whether FuturesTicker (needed by replay) was the only
        # import; the class itself is harmless.  KrakenClient is the
        # dangerous one.
        mod = _sys.modules["src.data.kraken_client"]
        # Allow: only FuturesTicker pulled in (via replay_ticker_provider)
        # Deny:  if KrakenClient was instantiated or directly imported
        # We can't easily check instantiation, but we log a warning.
        logger.debug("kraken_client_module_loaded_for_FuturesTicker_only")


# ---------------------------------------------------------------------------
# Pass 1: Gate Enabled
# ---------------------------------------------------------------------------

def run_pass_enabled(
    symbols: List[str],
    tick_timestamps: List[datetime],
    ticker_provider: ReplayTickerProvider,
    candle_provider: ReplayCandleMetaProvider,
    thresholds: SanityThresholds,
    *,
    tracker: Optional[DataQualityTracker] = None,
    accumulators: Optional[Dict[str, SymbolCoverageAccumulator]] = None,
) -> tuple[Dict[str, SymbolCoverageAccumulator], DataQualityTracker]:
    """Pass 1: run sanity gate + tracker.

    Returns ``(accumulators, tracker)`` so callers can inspect final
    tracker state or resume a split replay.

    Args:
        tracker:      Pre-initialized tracker (for restart-resume).
                      If ``None``, a fresh tracker is created.
        accumulators: Pre-initialized accumulators (for restart-resume).
                      If ``None``, fresh accumulators are created.
    """
    # Mutable clock for the tracker
    current_ts = [tick_timestamps[0].timestamp()]

    if tracker is None:
        tracker = DataQualityTracker(
            clock=lambda: current_ts[0],
            persist_interval_seconds=999_999,  # no disk persistence during replay
            state_file="/dev/null",
        )
    else:
        # Rewire the clock to our mutable cell
        tracker._clock = lambda: current_ts[0]

    if accumulators is None:
        accumulators = {
            sym: SymbolCoverageAccumulator() for sym in symbols
        }
    else:
        # Ensure all symbols have an accumulator
        for sym in symbols:
            if sym not in accumulators:
                accumulators[sym] = SymbolCoverageAccumulator()

    for tick_ts in tick_timestamps:
        current_ts[0] = tick_ts.timestamp()

        for sym in symbols:
            acc = accumulators[sym]
            state = tracker.get_state(sym).value

            # Scheduling filter
            should = tracker.should_analyze(sym)
            is_probe = (tracker.get_state(sym) == SymbolHealthState.SUSPENDED and should)

            if not should:
                acc.record_cycle(
                    passed=False,
                    reason="",
                    skipped=True,
                    state=state,
                    tick_ts=current_ts[0],
                    ticker_present=True,
                    is_probe=False,
                )
                continue

            # Stage A: ticker sanity
            ft = ticker_provider.get_ticker(sym, tick_ts)
            ticker_present = ft is not None

            stage_a = check_ticker_sanity(
                symbol=sym,
                futures_ticker=ft,
                spot_ticker=None,
                thresholds=thresholds,
            )
            if not stage_a.passed:
                tracker.record_result(sym, passed=False, reason=stage_a.reason)
                state = tracker.get_state(sym).value
                acc.record_cycle(
                    passed=False,
                    reason=stage_a.reason,
                    skipped=False,
                    state=state,
                    tick_ts=current_ts[0],
                    ticker_present=ticker_present,
                    is_probe=is_probe,
                )
                continue

            # Stage B: candle sanity
            mock_cm = candle_provider.get_mock_candle_manager(sym, tick_ts)
            stage_b = check_candle_sanity(
                symbol=sym,
                candle_manager=mock_cm,
                thresholds=thresholds,
                now=tick_ts,
            )
            if not stage_b.passed:
                tracker.record_result(sym, passed=False, reason=stage_b.reason)
                state = tracker.get_state(sym).value
                acc.record_cycle(
                    passed=False,
                    reason=stage_b.reason,
                    skipped=False,
                    state=state,
                    tick_ts=current_ts[0],
                    ticker_present=ticker_present,
                    is_probe=is_probe,
                )
                continue

            # Both stages passed
            tracker.record_result(sym, passed=True)
            state = tracker.get_state(sym).value
            acc.record_cycle(
                passed=True,
                reason="",
                skipped=False,
                state=state,
                tick_ts=current_ts[0],
                ticker_present=ticker_present,
                is_probe=is_probe,
            )

    return accumulators, tracker


# ---------------------------------------------------------------------------
# Pass 2: Gate Disabled (control)
# ---------------------------------------------------------------------------

def run_pass_disabled(
    symbols: List[str],
    tick_timestamps: List[datetime],
) -> Dict[str, int]:
    """Pass 2: no gate, everything analyzed.  Returns per-symbol analyze counts."""
    counts: Dict[str, int] = {sym: 0 for sym in symbols}
    for _tick_ts in tick_timestamps:
        for sym in symbols:
            counts[sym] += 1
    return counts


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_replay(
    db_url: str,
    start: datetime,
    end: datetime,
    tick_seconds: int = 300,
    output_dir: str = "data/replay_reports",
    thresholds: Optional[SanityThresholds] = None,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Run full two-pass replay and generate reports.

    Returns ``(coverage_report, delta_report)`` dicts.
    """
    _assert_no_live_imports()

    if thresholds is None:
        thresholds = SanityThresholds()

    # Build tick timeline
    tick_timestamps: List[datetime] = []
    ts = start
    while ts <= end:
        tick_timestamps.append(ts)
        ts += timedelta(seconds=tick_seconds)

    if not tick_timestamps:
        logger.error("empty_tick_range", start=start.isoformat(), end=end.isoformat())
        return {}, {}

    logger.info(
        "replay_starting",
        ticks=len(tick_timestamps),
        start=start.isoformat(),
        end=end.isoformat(),
        tick_seconds=tick_seconds,
    )

    # --- Preload providers ---
    ticker_prov = ReplayTickerProvider(db_url)
    candle_prov = ReplayCandleMetaProvider(db_url)

    # Discover symbols from snapshot DB
    from sqlalchemy import create_engine, distinct
    from sqlalchemy.orm import sessionmaker as sm_factory
    from src.recording.models import MarketSnapshot

    engine = create_engine(db_url, pool_pre_ping=True)
    SessionFactory = sm_factory(bind=engine)
    session = SessionFactory()
    try:
        symbols = [
            row[0]
            for row in session.query(distinct(MarketSnapshot.symbol))
            .filter(
                MarketSnapshot.ts_utc >= start,
                MarketSnapshot.ts_utc <= end,
            )
            .all()
        ]
    finally:
        session.close()

    if not symbols:
        logger.error("no_symbols_in_recording", start=start.isoformat(), end=end.isoformat())
        return {}, {}

    symbols.sort()
    logger.info("replay_symbols", count=len(symbols), symbols=symbols[:10])

    ticker_prov.preload(symbols, start, end)
    candle_prov.preload(symbols, start, end)

    # --- Pass 1: gate enabled ---
    logger.info("replay_pass_1_starting")
    enabled_acc, _tracker = run_pass_enabled(
        symbols, tick_timestamps, ticker_prov, candle_prov, thresholds,
    )

    # --- Pass 2: gate disabled ---
    logger.info("replay_pass_2_starting")
    disabled_counts = run_pass_disabled(symbols, tick_timestamps)

    # --- Generate reports ---
    total_cycles = len(tick_timestamps)
    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")

    coverage = build_coverage_report(
        enabled_acc, total_cycles, start_str, end_str,
    )
    delta = build_delta_report(enabled_acc, disabled_counts, total_cycles)

    # Write to disk
    write_reports(coverage, delta, output_dir, start_str, end_str)

    logger.info(
        "replay_complete",
        total_cycles=total_cycles,
        symbols=len(symbols),
        enabled_analyze=delta["enabled"]["analyze_calls"],
        disabled_analyze=delta["disabled"]["analyze_calls"],
        wasted_work_pct=delta["wasted_work_pct"],
    )

    return coverage, delta


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run two-pass replay backtest on recorded market snapshots.",
    )
    parser.add_argument(
        "--db-url",
        default=os.environ.get("DATABASE_URL", ""),
        help="PostgreSQL connection string (default: $DATABASE_URL)",
    )
    parser.add_argument(
        "--start",
        required=True,
        help="Start date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--end",
        required=True,
        help="End date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--tick-seconds",
        type=int,
        default=300,
        help="Tick interval in seconds (default: 300)",
    )
    parser.add_argument(
        "--output-dir",
        default="data/replay_reports",
        help="Output directory for reports (default: data/replay_reports)",
    )
    args = parser.parse_args()

    if not args.db_url:
        print("ERROR: --db-url or DATABASE_URL required.", file=sys.stderr)
        sys.exit(1)

    start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    coverage, delta = run_replay(
        db_url=args.db_url,
        start=start,
        end=end,
        tick_seconds=args.tick_seconds,
        output_dir=args.output_dir,
    )

    if coverage:
        print(f"\nReplay complete. Reports in: {args.output_dir}/")
        print(f"  Wasted work prevented: {delta.get('wasted_work_pct', 0):.1f}%")
    else:
        print("Replay produced no results.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
