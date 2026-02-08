"""
Data sanity gate -- two-stage stateless checks for per-symbol data quality.

Stage A (pre-I/O):  Futures spread + volume from already-fetched tickers.
Stage B (post-I/O): Candle count + freshness from CandleManager cache.

These checks apply to ALL tiers, including pinned Tier A.  They do NOT
use OI or funding (Kraken misreports both).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from src.data.candle_manager import CandleManager
    from src.data.kraken_client import FuturesTicker

# Timeframe duration in hours -- used for freshness calculation.
TF_DURATION_HOURS: Dict[str, float] = {
    "15m": 0.25,
    "1h": 1.0,
    "4h": 4.0,
    "1d": 24.0,
}


def _max_candle_age_hours(tf: str) -> float:
    """Derive max acceptable candle age from the timeframe.

    Formula: ``max(2 * tf_hours, tf_hours + 1)``

    Examples:
        4h  -> 8.0 hours
        1h  -> 2.0 hours
        1d  -> 48.0 hours
        15m -> 1.25 hours
    """
    d = TF_DURATION_HOURS.get(tf, 4.0)
    return max(2 * d, d + 1.0)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class SanityThresholds:
    """Configurable thresholds for the data sanity gate."""

    max_spread_pct: Decimal = Decimal("0.10")       # 10%
    min_volume_24h_usd: Decimal = Decimal("10000")   # $10k dead-market floor
    min_decision_tf_candles: int = 250                # 4H bars (EMA200 + buffer)
    decision_tf: str = "4h"
    allow_spot_fallback: bool = False                  # spot only if futures missing


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class SanityResult:
    """Outcome of a single sanity check."""

    passed: bool
    reason: str = ""


# ---------------------------------------------------------------------------
# Stage A -- ticker sanity (pre-I/O, no disk / network)
# ---------------------------------------------------------------------------

def check_ticker_sanity(
    symbol: str,
    futures_ticker: Optional["FuturesTicker"],
    spot_ticker: Optional[Dict[str, Any]],
    thresholds: SanityThresholds,
) -> SanityResult:
    """Check spread and volume from **futures** ticker data.

    Falls back to spot ticker only when ``futures_ticker`` is ``None``
    AND ``thresholds.allow_spot_fallback`` is True.

    Returns ``SanityResult(passed=True)`` when both checks pass.
    """
    # --- resolve data source ---
    spread: Optional[Decimal] = None
    volume: Optional[Decimal] = None

    if futures_ticker is not None:
        spread = futures_ticker.spread_pct
        volume = futures_ticker.volume_24h
    elif thresholds.allow_spot_fallback and spot_ticker is not None:
        bid = Decimal(str(spot_ticker.get("bid", 0) or 0))
        ask = Decimal(str(spot_ticker.get("ask", 0) or 0))
        if bid > 0 and ask > 0:
            spread = (ask - bid) / bid
        volume = Decimal(str(spot_ticker.get("quoteVolume", 0) or 0))
    else:
        # No usable ticker data at all
        return SanityResult(
            passed=False,
            reason="no_futures_ticker" if futures_ticker is None else "ticker_data_missing",
        )

    # --- spread check ---
    if spread is None or spread >= thresholds.max_spread_pct:
        spread_str = f"{spread:.2%}" if spread is not None else "N/A"
        return SanityResult(
            passed=False,
            reason=f"spread={spread_str} >= {thresholds.max_spread_pct:.0%}",
        )

    # --- volume check ---
    if volume is None or volume < thresholds.min_volume_24h_usd:
        vol_str = f"${volume:,.0f}" if volume is not None else "N/A"
        return SanityResult(
            passed=False,
            reason=f"volume={vol_str} < ${thresholds.min_volume_24h_usd:,.0f}",
        )

    return SanityResult(passed=True)


# ---------------------------------------------------------------------------
# Stage B -- candle sanity (post-I/O, reads CandleManager cache)
# ---------------------------------------------------------------------------

def check_candle_sanity(
    symbol: str,
    candle_manager: "CandleManager",
    thresholds: SanityThresholds,
) -> SanityResult:
    """Check candle count and freshness for the decision timeframe.

    Must be called **after** ``_update_candles()`` so the cache is populated.
    """
    tf = thresholds.decision_tf
    candles = candle_manager.get_candles(symbol, tf)
    count = len(candles)

    # --- count check ---
    if count < thresholds.min_decision_tf_candles:
        return SanityResult(
            passed=False,
            reason=f"candles_{tf}={count} < {thresholds.min_decision_tf_candles}",
        )

    # --- freshness check ---
    newest = candles[-1].timestamp
    if newest.tzinfo is None:
        newest = newest.replace(tzinfo=timezone.utc)
    age_hours = (datetime.now(timezone.utc) - newest).total_seconds() / 3600
    max_age = _max_candle_age_hours(tf)
    if age_hours > max_age:
        return SanityResult(
            passed=False,
            reason=f"candle_age_{tf}={age_hours:.1f}h > {max_age:.1f}h",
        )

    return SanityResult(passed=True)
