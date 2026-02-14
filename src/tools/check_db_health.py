#!/usr/bin/env python3
"""
Check database health and integrity.

Reports table sizes, stale data, orphaned records, and schema issues.

Usage:
    python -m src.tools.check_db_health     # read-only check
    python -m src.tools.check_db_health -v   # verbose output
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.tools._safety_guard import parse_tool_args, guard_live_keys


def main():
    args = parse_tool_args("Check database health and integrity")
    guard_live_keys()
    print("[READ-ONLY] Checking database health...")

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
    try:
        from check_db_health import main as original_main
        original_main()
    except ImportError:
        print("Original script not found at scripts/check_db_health.py")
        sys.exit(1)


if __name__ == "__main__":
    main()
