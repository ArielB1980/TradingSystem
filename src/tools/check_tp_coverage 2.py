#!/usr/bin/env python3
"""
Check take-profit order coverage for all open positions.

Reports positions missing TP orders, partial coverage, and coverage gaps.

Usage:
    python -m src.tools.check_tp_coverage     # read-only check (always safe)
    python -m src.tools.check_tp_coverage -v   # verbose output
"""
import sys
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.tools._safety_guard import parse_tool_args, guard_live_keys


def main():
    args = parse_tool_args("Check TP order coverage for all open positions")
    guard_live_keys()
    print("[READ-ONLY] Checking TP coverage...")

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
    try:
        from check_tp_coverage import main as original_main
        asyncio.run(original_main())
    except ImportError:
        print("Original script not found at scripts/check_tp_coverage.py")
    except TypeError:
        # original_main might not be async
        from check_tp_coverage import main as original_main
        original_main()


if __name__ == "__main__":
    main()
