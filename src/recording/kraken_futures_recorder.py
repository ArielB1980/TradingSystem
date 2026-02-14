"""
Kraken Futures market snapshot recorder.

Captures ticker + candle-freshness metadata at regular intervals
for later deterministic replay.  Production-safe: read-only API
calls, rate-limited, no trading side effects.

CLI usage::

    python -m src.recording.kraken_futures_recorder \
        --symbols-file data/discovered_markets.json \
        --interval-seconds 300

Environment:
    DATABASE_URL  -- PostgreSQL connection string (required)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.exceptions import OperationalError, DataError
from src.monitoring.logger import get_logger
from src.recording.models import MarketSnapshot
from src.storage.db import Base

logger = get_logger(__name__)

# Timeframes whose candle metadata we record.
RECORDED_TIMEFRAMES = ["4h", "1d", "1h", "15m"]


# ---------------------------------------------------------------------------
# Symbol list loading
# ---------------------------------------------------------------------------

def load_symbols(path: str) -> List[str]:
    """Load symbol list from a discovered-markets JSON file.

    Expected format::

        {"markets": ["BTC/USD", "ETH/USD", ...], ...}
    """
    p = Path(path)
    if not p.exists():
        logger.error("symbols_file_not_found", path=path)
        sys.exit(1)

    data = json.loads(p.read_text())

    if isinstance(data, dict):
        # Standard format: {"markets": [...]}
        markets = data.get("markets")
        if isinstance(markets, list) and markets:
            return markets
        # Fallback: mapping keys
        mapping = data.get("mapping")
        if isinstance(mapping, dict):
            return list(mapping.keys())
    elif isinstance(data, list):
        return data

    logger.error("symbols_file_invalid", path=path, keys=list(data.keys()) if isinstance(data, dict) else type(data).__name__)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Candle metadata helpers
# ---------------------------------------------------------------------------

def _query_candle_meta(symbol: str) -> tuple[Dict[str, Optional[str]], Dict[str, int]]:
    """Query candle DB for latest timestamp + count per timeframe.

    Returns ``(last_ts_dict, count_dict)`` with ISO strings and ints.
    """
    from src.storage.repository import get_latest_candle_timestamp, count_candles

    last_ts: Dict[str, Optional[str]] = {}
    counts: Dict[str, int] = {}

    for tf in RECORDED_TIMEFRAMES:
        try:
            ts = get_latest_candle_timestamp(symbol, tf)
            last_ts[tf] = ts.isoformat() if ts else None
        except (OperationalError, DataError, OSError):
            last_ts[tf] = None

        try:
            counts[tf] = count_candles(symbol, tf)
        except (OperationalError, DataError, OSError):
            counts[tf] = 0

    return last_ts, counts


# ---------------------------------------------------------------------------
# Single recording cycle
# ---------------------------------------------------------------------------

async def _record_cycle(
    symbols: List[str],
    session_factory: sessionmaker,
    db_url: str,
) -> int:
    """Run one recording cycle.  Returns number of snapshots written."""
    from src.data.kraken_client import KrakenClient

    now_utc = datetime.now(timezone.utc)

    # --- 1. Fetch all futures tickers in one API call ---
    client = KrakenClient(
        api_key="",
        api_secret="",
    )
    try:
        tickers = await client.get_futures_tickers_bulk_full()
    except (OperationalError, DataError) as exc:
        logger.warning("bulk_ticker_fetch_failed", error=str(exc), error_type=type(exc).__name__)
        tickers = {}
    finally:
        await client.close()

    # --- 2. Build snapshots per symbol ---
    snapshots: List[MarketSnapshot] = []
    for symbol in symbols:
        try:
            snap = _build_snapshot(symbol, now_utc, tickers)
            snapshots.append(snap)
        except (OperationalError, DataError, ValueError, TypeError, KeyError) as exc:
            # Record error snapshot so we have continuity
            snapshots.append(MarketSnapshot(
                ts_utc=now_utc,
                symbol=symbol,
                error_code=f"{type(exc).__name__}: {exc}"[:128],
            ))

    # --- 3. Batch insert ---
    session = session_factory()
    try:
        session.bulk_save_objects(snapshots)
        session.commit()
        logger.info(
            "recording_cycle_complete",
            snapshots=len(snapshots),
            ts=now_utc.isoformat(),
            tickers_available=len(tickers),
        )
    except (OperationalError, DataError, OSError) as exc:
        session.rollback()
        logger.error("recording_batch_insert_failed", error=str(exc), error_type=type(exc).__name__)
    finally:
        session.close()

    return len(snapshots)


def _build_snapshot(
    symbol: str,
    ts: datetime,
    tickers: Dict[str, Any],
) -> MarketSnapshot:
    """Build a single MarketSnapshot from ticker + candle DB data."""
    from src.data.kraken_client import FuturesTicker

    # --- resolve futures ticker ---
    # The bulk result is keyed by multiple formats; try common ones.
    base = symbol.split("/")[0].upper()
    if base == "BTC":
        base = "XBT"  # Kraken convention

    futures_keys = [
        f"PF_{base}USD",         # Perpetual
        symbol,                   # Spot symbol as key
        f"{base}/USD",           # BASE/USD
        f"{base}/USD:USD",       # CCXT unified
    ]

    ft: Optional[FuturesTicker] = None
    for key in futures_keys:
        ft = tickers.get(key)
        if ft is not None:
            break

    # --- extract ticker fields ---
    bid = ask = spread = volume = oi = funding = None
    error_code = None

    if ft is not None:
        bid = ft.bid
        ask = ft.ask
        spread = ft.spread_pct
        volume = ft.volume_24h
        oi = ft.open_interest
        funding = ft.funding_rate
    else:
        error_code = "no_futures_ticker"

    # --- candle metadata (from DB) ---
    last_ts_dict, count_dict = _query_candle_meta(symbol)

    return MarketSnapshot(
        ts_utc=ts,
        symbol=symbol,
        futures_bid=bid,
        futures_ask=ask,
        futures_spread_pct=spread,
        futures_volume_usd_24h=volume,
        open_interest_usd=oi,
        funding_rate=funding,
        last_candle_ts_json=json.dumps(last_ts_dict),
        candle_count_json=json.dumps(count_dict),
        error_code=error_code,
    )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def run_recorder(
    symbols_file: str,
    interval_seconds: int,
    db_url: str,
    max_cycles: Optional[int] = None,
) -> None:
    """Run the recording loop."""
    symbols = load_symbols(symbols_file)
    logger.info("recorder_starting", symbols=len(symbols), interval=interval_seconds)

    # Create engine + table
    engine = create_engine(db_url, pool_pre_ping=True)
    # Only create the market_snapshots table (don't touch other tables)
    MarketSnapshot.__table__.create(engine, checkfirst=True)
    session_factory = sessionmaker(bind=engine)

    cycle = 0
    backoff = 0
    while True:
        t0 = time.monotonic()
        try:
            written = await _record_cycle(symbols, session_factory, db_url)
            backoff = 0  # reset on success
        except (OperationalError, DataError, OSError) as exc:
            backoff = min(backoff * 2 or 2, 60)
            logger.error("recording_cycle_error", error=str(exc), error_type=type(exc).__name__, backoff=backoff)
            await asyncio.sleep(backoff)
            continue

        cycle += 1
        if max_cycles and cycle >= max_cycles:
            logger.info("recorder_max_cycles_reached", cycles=cycle)
            break

        elapsed = time.monotonic() - t0
        sleep_for = max(0.0, interval_seconds - elapsed)
        if sleep_for > 0:
            await asyncio.sleep(sleep_for)


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    import os

    parser = argparse.ArgumentParser(
        description="Record Kraken Futures market snapshots for replay backtesting.",
    )
    parser.add_argument(
        "--symbols-file",
        default="data/discovered_markets.json",
        help="Path to JSON file with symbol list (default: data/discovered_markets.json)",
    )
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=300,
        help="Recording interval in seconds (default: 300 = 5 min)",
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=None,
        help="Stop after N cycles (default: run forever)",
    )
    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL environment variable is required.", file=sys.stderr)
        sys.exit(1)

    asyncio.run(run_recorder(
        symbols_file=args.symbols_file,
        interval_seconds=args.interval_seconds,
        db_url=db_url,
        max_cycles=args.max_cycles,
    ))


if __name__ == "__main__":
    main()
