#!/usr/bin/env python3
"""
Recover stop-loss order IDs for positions missing them.

Queries exchange open orders, matches SL orders to positions by symbol/side,
and updates the position registry.

Usage:
    python -m src.tools.recover_sl_order_ids          # dry-run
    python -m src.tools.recover_sl_order_ids --execute # actually update
"""
import sys
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.tools._safety_guard import parse_tool_args, guard_live_keys


def main():
    args = parse_tool_args("Recover stop-loss order IDs for positions missing them")
    guard_live_keys()
    dry_run = not args.execute

    if dry_run:
        print("[DRY RUN] Would recover SL order IDs. Use --execute to perform.")
    
    # Delegate to original script
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
    try:
        import importlib
        mod = importlib.import_module("recover_sl_order_ids")
        if hasattr(mod, "main"):
            if dry_run:
                print("  (Original script would run here with --execute)")
            else:
                asyncio.run(mod.main()) if asyncio.iscoroutinefunction(getattr(mod, "main", None)) else mod.main()
        else:
            print("  Original script has no main() entry point; run directly: scripts/recover_sl_order_ids.py")
    except ImportError as e:
        print(f"  Could not import original script: {e}")


if __name__ == "__main__":
    main()
