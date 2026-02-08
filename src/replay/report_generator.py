"""
Replay report generator.

Produces two structured reports from replay backtest accumulators:

1. **Coverage report** -- per-symbol and global data quality metrics.
2. **Delta report** -- comparison of gate-enabled vs gate-disabled passes.

Each report is written as JSON + a short human-readable `_summary.txt`.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.monitoring.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Per-symbol accumulator (filled during replay Pass 1)
# ---------------------------------------------------------------------------

@dataclass
class SymbolCoverageAccumulator:
    """Mutable counters for one symbol during gate-enabled replay."""

    cycles_total: int = 0
    cycles_sanity_pass: int = 0
    sanity_fail_count: int = 0
    sanity_fail_reasons_counts: Dict[str, int] = field(default_factory=dict)
    skipped_count: int = 0            # scheduler skips (DEGRADED / SUSPENDED)

    # State time tracking (seconds)
    time_in_state_seconds: Dict[str, float] = field(
        default_factory=lambda: {"HEALTHY": 0.0, "DEGRADED": 0.0, "SUSPENDED": 0.0}
    )
    last_state: str = "HEALTHY"
    last_state_ts: float = 0.0

    # Transition counters
    transitions_counts: Dict[str, int] = field(default_factory=dict)
    last_recorded_state: str = "HEALTHY"

    # Suspension
    suspensions_count: int = 0
    total_suspension_seconds: float = 0.0

    # Probes & analysis
    probe_checks_count: int = 0
    analyze_calls_count: int = 0

    # Ticker availability
    ticker_present_count: int = 0
    ticker_missing_count: int = 0

    def record_cycle(
        self,
        passed: bool,
        reason: str,
        skipped: bool,
        state: str,
        tick_ts: float,
        ticker_present: bool,
        is_probe: bool = False,
    ) -> None:
        """Record the outcome of one cycle for this symbol."""
        self.cycles_total += 1

        # State-time accounting
        if self.last_state_ts > 0:
            elapsed = tick_ts - self.last_state_ts
            if elapsed > 0:
                self.time_in_state_seconds[self.last_state] = (
                    self.time_in_state_seconds.get(self.last_state, 0.0) + elapsed
                )

        # Detect transitions
        if state != self.last_recorded_state:
            key = f"{self.last_recorded_state}_to_{state}"
            self.transitions_counts[key] = self.transitions_counts.get(key, 0) + 1
            if state == "SUSPENDED":
                self.suspensions_count += 1
            self.last_recorded_state = state

        self.last_state = state
        self.last_state_ts = tick_ts

        # Ticker tracking
        if ticker_present:
            self.ticker_present_count += 1
        else:
            self.ticker_missing_count += 1

        if skipped:
            self.skipped_count += 1
            return

        if is_probe:
            self.probe_checks_count += 1

        if passed:
            self.cycles_sanity_pass += 1
            self.analyze_calls_count += 1
        else:
            self.sanity_fail_count += 1
            # Classify failure reason
            rkey = _classify_reason(reason)
            self.sanity_fail_reasons_counts[rkey] = (
                self.sanity_fail_reasons_counts.get(rkey, 0) + 1
            )

    @property
    def ticker_missing_rate(self) -> float:
        total = self.ticker_present_count + self.ticker_missing_count
        return self.ticker_missing_count / total if total else 0.0

    @property
    def avg_suspension_duration_seconds(self) -> float:
        if self.suspensions_count == 0:
            return 0.0
        return self.total_suspension_seconds / self.suspensions_count

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "cycles_total": self.cycles_total,
            "cycles_sanity_pass": self.cycles_sanity_pass,
            "sanity_fail_count": self.sanity_fail_count,
            "sanity_fail_reasons_counts": dict(self.sanity_fail_reasons_counts),
            "skipped_count": self.skipped_count,
            "time_in_state_seconds": dict(self.time_in_state_seconds),
            "transitions_counts": dict(self.transitions_counts),
            "suspensions_count": self.suspensions_count,
            "avg_suspension_duration_seconds": self.avg_suspension_duration_seconds,
            "probe_checks_count": self.probe_checks_count,
            "analyze_calls_count": self.analyze_calls_count,
            "ticker_missing_rate": round(self.ticker_missing_rate, 4),
        }
        return d


def _classify_reason(reason: str) -> str:
    """Map a sanity failure reason string to a canonical bucket."""
    r = reason.lower()
    if "spread" in r:
        return "spread"
    if "volume" in r:
        return "volume"
    if "candle_age" in r or "candles_stale" in r or "stale" in r:
        return "candles_stale"
    if "candles_" in r and "count" not in r:
        return "candles_count"
    if "candles_" in r:
        return "candles_count"
    if "no_futures_ticker" in r:
        return "no_futures_ticker"
    return reason[:32] if reason else "unknown"


# ---------------------------------------------------------------------------
# Coverage report builder
# ---------------------------------------------------------------------------

def build_coverage_report(
    per_symbol: Dict[str, SymbolCoverageAccumulator],
    total_cycles: int,
    recording_start: str,
    recording_end: str,
) -> Dict[str, Any]:
    """Build the full coverage report dict."""
    symbols_data: Dict[str, Any] = {}
    for sym, acc in sorted(per_symbol.items()):
        symbols_data[sym] = acc.to_dict()

    # Global aggregates
    total_analyze = sum(a.analyze_calls_count for a in per_symbol.values())
    avg_analyzed_per_cycle = total_analyze / total_cycles if total_cycles else 0.0

    # Top 10 most-suspended
    suspended_list = [
        {
            "symbol": sym,
            "suspension_count": a.suspensions_count,
            "total_suspended_seconds": a.time_in_state_seconds.get("SUSPENDED", 0.0),
        }
        for sym, a in per_symbol.items()
        if a.suspensions_count > 0
    ]
    suspended_list.sort(key=lambda x: x["suspension_count"], reverse=True)

    # Top 10 reasons
    reason_totals: Dict[str, int] = {}
    for a in per_symbol.values():
        for rkey, cnt in a.sanity_fail_reasons_counts.items():
            reason_totals[rkey] = reason_totals.get(rkey, 0) + cnt
    top_reasons = sorted(
        [{"reason": k, "count": v} for k, v in reason_totals.items()],
        key=lambda x: x["count"],
        reverse=True,
    )

    return {
        "recording_start": recording_start,
        "recording_end": recording_end,
        "total_cycles": total_cycles,
        "symbols": symbols_data,
        "global": {
            "avg_symbols_analyzed_per_cycle": round(avg_analyzed_per_cycle, 2),
            "top_10_most_suspended": suspended_list[:10],
            "top_10_reasons": top_reasons[:10],
        },
    }


# ---------------------------------------------------------------------------
# Delta report builder
# ---------------------------------------------------------------------------

def build_delta_report(
    enabled_per_symbol: Dict[str, SymbolCoverageAccumulator],
    disabled_analyze_calls_per_symbol: Dict[str, int],
    total_cycles: int,
) -> Dict[str, Any]:
    """Build the delta report comparing enabled vs disabled passes."""
    enabled_total = sum(a.analyze_calls_count for a in enabled_per_symbol.values())
    disabled_total = sum(disabled_analyze_calls_per_symbol.values())

    wasted_work = disabled_total - enabled_total
    wasted_pct = (wasted_work / disabled_total * 100) if disabled_total > 0 else 0.0

    symbols_ever_degraded = [
        sym for sym, a in enabled_per_symbol.items()
        if a.transitions_counts.get("HEALTHY_to_DEGRADED", 0) > 0
    ]
    symbols_ever_suspended = [
        sym for sym, a in enabled_per_symbol.items()
        if a.suspensions_count > 0
    ]

    per_symbol_delta: Dict[str, Any] = {}
    all_symbols = set(enabled_per_symbol.keys()) | set(disabled_analyze_calls_per_symbol.keys())
    for sym in sorted(all_symbols):
        en = enabled_per_symbol.get(sym)
        en_calls = en.analyze_calls_count if en else 0
        dis_calls = disabled_analyze_calls_per_symbol.get(sym, 0)
        per_symbol_delta[sym] = {
            "enabled_analyze_calls": en_calls,
            "disabled_analyze_calls": dis_calls,
            "delta": dis_calls - en_calls,
        }

    return {
        "total_cycles": total_cycles,
        "enabled": {
            "analyze_calls": enabled_total,
            "signals_generated": None,  # requires SMC engine (future work)
        },
        "disabled": {
            "analyze_calls": disabled_total,
            "signals_generated": None,
        },
        "wasted_work_prevented": wasted_work,
        "wasted_work_pct": round(wasted_pct, 2),
        "enabled_symbols_ever_degraded": sorted(symbols_ever_degraded),
        "enabled_symbols_ever_suspended": sorted(symbols_ever_suspended),
        "per_symbol": per_symbol_delta,
        "note": (
            "Full trade-level PnL metrics (trades, pnl, winrate, max_dd, avg_hold) "
            "require --with-signals mode (future work). Currently null."
        ),
    }


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------

def write_reports(
    coverage: Dict[str, Any],
    delta: Dict[str, Any],
    output_dir: str,
    start_str: str,
    end_str: str,
) -> List[str]:
    """Write coverage + delta reports as JSON + summary.txt.

    Returns list of written file paths.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    written: List[str] = []

    # Coverage JSON
    cov_json = out / f"coverage_{start_str}_{end_str}.json"
    cov_json.write_text(json.dumps(coverage, indent=2, default=str))
    written.append(str(cov_json))

    # Coverage summary
    cov_txt = out / f"coverage_{start_str}_{end_str}_summary.txt"
    cov_txt.write_text(_coverage_summary_text(coverage))
    written.append(str(cov_txt))

    # Delta JSON
    delta_json = out / f"delta_{start_str}_{end_str}.json"
    delta_json.write_text(json.dumps(delta, indent=2, default=str))
    written.append(str(delta_json))

    # Delta summary
    delta_txt = out / f"delta_{start_str}_{end_str}_summary.txt"
    delta_txt.write_text(_delta_summary_text(delta))
    written.append(str(delta_txt))

    logger.info("reports_written", files=written)
    return written


def _coverage_summary_text(cov: Dict[str, Any]) -> str:
    lines = [
        "=" * 60,
        "DATA QUALITY COVERAGE REPORT",
        "=" * 60,
        f"Recording period: {cov['recording_start']} -> {cov['recording_end']}",
        f"Total cycles: {cov['total_cycles']}",
        f"Avg symbols analyzed per cycle: {cov['global']['avg_symbols_analyzed_per_cycle']}",
        "",
        "--- Top 10 Most-Suspended Symbols ---",
    ]
    for item in cov["global"]["top_10_most_suspended"]:
        lines.append(f"  {item['symbol']}: {item['suspension_count']} suspensions, {item['total_suspended_seconds']:.0f}s total")
    if not cov["global"]["top_10_most_suspended"]:
        lines.append("  (none)")

    lines.append("")
    lines.append("--- Top 10 Failure Reasons ---")
    for item in cov["global"]["top_10_reasons"]:
        lines.append(f"  {item['reason']}: {item['count']}")
    if not cov["global"]["top_10_reasons"]:
        lines.append("  (none)")

    lines.append("")
    lines.append("--- Per-Symbol Summary ---")
    for sym, data in sorted(cov.get("symbols", {}).items()):
        lines.append(
            f"  {sym}: pass={data['cycles_sanity_pass']}/{data['cycles_total']} "
            f"fail={data['sanity_fail_count']} skip={data['skipped_count']} "
            f"analyze={data['analyze_calls_count']} "
            f"ticker_missing={data['ticker_missing_rate']:.1%}"
        )

    lines.append("")
    return "\n".join(lines)


def _delta_summary_text(delta: Dict[str, Any]) -> str:
    lines = [
        "=" * 60,
        "TRADING DELTA REPORT",
        "=" * 60,
        f"Total cycles: {delta['total_cycles']}",
        f"Enabled analyze calls:  {delta['enabled']['analyze_calls']}",
        f"Disabled analyze calls: {delta['disabled']['analyze_calls']}",
        f"Wasted work prevented:  {delta['wasted_work_prevented']}",
        f"Wasted work %:          {delta['wasted_work_pct']:.1f}%",
        "",
        f"Symbols ever degraded:  {len(delta['enabled_symbols_ever_degraded'])}",
        f"Symbols ever suspended: {len(delta['enabled_symbols_ever_suspended'])}",
        "",
    ]

    if delta["enabled_symbols_ever_suspended"]:
        lines.append("Suspended symbols:")
        for sym in delta["enabled_symbols_ever_suspended"]:
            lines.append(f"  - {sym}")
        lines.append("")

    lines.append("--- Per-Symbol Delta ---")
    for sym, d in sorted(delta.get("per_symbol", {}).items()):
        if d["delta"] > 0:
            lines.append(f"  {sym}: enabled={d['enabled_analyze_calls']} disabled={d['disabled_analyze_calls']} saved={d['delta']}")

    lines.append("")
    lines.append(f"Note: {delta.get('note', '')}")
    lines.append("")
    return "\n".join(lines)
