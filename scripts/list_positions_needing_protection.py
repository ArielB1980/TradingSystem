#!/usr/bin/env python3
"""
Extract symbols from run.log for "TP backfill skipped: position not protected"
or "Positions needing protection (TP backfill skipped)" and print them plus
the place-missing-stops command.

Use when server logs show many "TP backfill skipped: position not protected"
and you want a compact list and the exact command to run.

Usage:
  python scripts/list_positions_needing_protection.py [--log PATH] [--lines N]

Default log: logs/run.log (relative to repo root).
Default lines: 5000 (only scan last N lines).
"""
from __future__ import annotations

import argparse
import json
import os


def main() -> None:
    ap = argparse.ArgumentParser(
        description="List symbols needing protection from run.log and print place-missing-stops command."
    )
    ap.add_argument(
        "--log",
        default="logs/run.log",
        help="Path to run.log (default: logs/run.log)",
    )
    ap.add_argument(
        "--lines",
        type=int,
        default=5000,
        help="Scan last N lines (default: 5000)",
    )
    args = ap.parse_args()

    repo_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..")
    )
    log_path = os.path.join(repo_root, args.log) if not os.path.isabs(args.log) else args.log

    if not os.path.isfile(log_path):
        print(f"Log file not found: {log_path}")
        return

    seen: set[str] = set()
    with open(log_path) as f:
        lines = f.readlines()

    # Only scan last N lines
    tail = lines[-args.lines :] if len(lines) > args.lines else lines

    for line in tail:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        event = (d.get("event") or "").strip()
        # Consolidated event: "Positions needing protection (TP backfill skipped)" with symbols=[...]
        if "Positions needing protection" in event and "symbols" in d:
            for s in d.get("symbols") or []:
                if s and isinstance(s, str):
                    seen.add(s)
            continue
        # Per-symbol: "TP backfill skipped: position not protected"
        if "TP backfill skipped: position not protected" in event:
            s = d.get("symbol")
            if s and isinstance(s, str):
                seen.add(s)

    symbols = sorted(seen)
    if not symbols:
        print("No symbols needing protection found in recent log lines.")
        print(f"Scanned last {len(tail)} lines of {log_path}")
        return

    print(f"Symbols needing protection ({len(symbols)}) from last {len(tail)} lines of {log_path}:")
    for s in symbols:
        print(f"  {s}")
    print()
    print("To protect (dry-run first, then live):")
    print("  make place-missing-stops")
    print("  make place-missing-stops-live")
    print()
    print("Or with custom stop distance (e.g. 1.5%%):")
    print("  make place-missing-stops STOP_PCT=1.5")
    print("  make place-missing-stops-live STOP_PCT=1.5")


if __name__ == "__main__":
    main()
