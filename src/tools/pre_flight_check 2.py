#!/usr/bin/env python3
"""
Pre-flight check for live trading.

Validates configuration, safety gates, and system health before live start.

Usage:
    python -m src.tools.pre_flight_check     # read-only check
    python -m src.tools.pre_flight_check -v   # verbose output
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.tools._safety_guard import parse_tool_args, guard_live_keys


def main():
    args = parse_tool_args("Pre-flight check for live trading")
    guard_live_keys()
    print("[READ-ONLY] Running pre-flight checks...")

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
    try:
        from pre_flight_check import PreFlightCheck
        checker = PreFlightCheck()
        result = checker.run_all_checks()
        sys.exit(0 if result else 1)
    except ImportError:
        print("Original script not found at scripts/pre_flight_check.py")
        sys.exit(1)


if __name__ == "__main__":
    main()
