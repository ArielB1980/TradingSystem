#!/usr/bin/env python3
"""
Backfill historical OHLCV data from Kraken.

Fills gaps in the candle database for backtesting and analysis.

Usage:
    python -m src.tools.backfill_historical_data          # dry-run (show what WOULD backfill)
    python -m src.tools.backfill_historical_data --execute # actually perform backfill
"""
import sys
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.tools._safety_guard import parse_tool_args, guard_live_keys


def main():
    args = parse_tool_args("Backfill historical OHLCV data from Kraken")
    guard_live_keys()
    dry_run = not args.execute

    if dry_run:
        print("[DRY RUN] Would backfill historical data. Use --execute to perform.")
    else:
        print("[EXECUTE] Backfilling historical data...")

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
    try:
        from backfill_historical_data import main as original_main
        if dry_run:
            print("  (Original script would run here with --execute)")
        else:
            asyncio.run(original_main()) if asyncio.iscoroutinefunction(original_main) else original_main()
    except ImportError:
        print("Original script not found at scripts/backfill_historical_data.py")
        sys.exit(1)


if __name__ == "__main__":
    main()
