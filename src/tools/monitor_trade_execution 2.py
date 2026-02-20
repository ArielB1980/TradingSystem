#!/usr/bin/env python3
"""
Monitor trade execution quality.

Reports fill rates, slippage, latencies, and execution anomalies.

Usage:
    python -m src.tools.monitor_trade_execution     # read-only check
    python -m src.tools.monitor_trade_execution -v   # verbose output
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.tools._safety_guard import parse_tool_args, guard_live_keys


def main():
    args = parse_tool_args("Monitor trade execution quality")
    guard_live_keys()
    print("[READ-ONLY] Monitoring trade execution...")

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
    try:
        from monitor_trade_execution import main as original_main
        original_main()
    except ImportError:
        print("Original script not found at scripts/monitor_trade_execution.py")
        sys.exit(1)


if __name__ == "__main__":
    main()
