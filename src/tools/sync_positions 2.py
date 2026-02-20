#!/usr/bin/env python3
"""
Sync positions from Kraken exchange to local registry/database.

Usage:
    python -m src.tools.sync_positions          # dry-run (show what WOULD sync)
    python -m src.tools.sync_positions --execute # actually perform sync
"""
import sys
import asyncio
from pathlib import Path

# Ensure project root on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.tools._safety_guard import parse_tool_args, guard_live_keys


def main():
    args = parse_tool_args("Sync positions from Kraken to local registry/database")
    guard_live_keys()
    dry_run = not args.execute

    if dry_run:
        print("[DRY RUN] Would sync positions from Kraken. Use --execute to perform.")
    else:
        print("[EXECUTE] Syncing positions from Kraken...")

    # Delegate to the original script's logic
    from src.config.config import load_config
    from src.data.kraken_client import KrakenClient

    async def _run():
        config = load_config()
        client = KrakenClient(
            api_key=config.exchange.api_key,
            api_secret=config.exchange.api_secret,
            futures_api_key=config.exchange.futures_api_key,
            futures_api_secret=config.exchange.futures_api_secret,
        )
        await client.initialize()
        positions = await client.get_all_futures_positions()
        print(f"\nFound {len(positions)} open positions on exchange:")
        for p in positions:
            print(f"  {p['symbol']}: size={p['size']} side={p['side']} entry={p['entry_price']}")

        if not dry_run and positions:
            # Import sync logic from original script
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
            try:
                from sync_positions_simple import sync_positions
                await sync_positions()
                print("\nSync complete.")
            except ImportError:
                print("\nOriginal sync script not found. Positions displayed above.")

        await client.close()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
