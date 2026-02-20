#!/usr/bin/env python3
"""
Live trading readiness check.

Validates API connection, account state, position sync, and safety gates.

Usage:
    python -m src.tools.check_live_readiness     # read-only check
    python -m src.tools.check_live_readiness -v   # verbose output
"""
import sys
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.tools._safety_guard import parse_tool_args, guard_live_keys


def main():
    args = parse_tool_args("Check live trading readiness")
    guard_live_keys()
    print("[READ-ONLY] Running live readiness checks...")

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
    try:
        from check_live_readiness import main as original_main
        asyncio.run(original_main())
    except ImportError:
        print("Original script not found at scripts/check_live_readiness.py")
    except TypeError:
        from check_live_readiness import main as original_main
        original_main()


if __name__ == "__main__":
    main()
