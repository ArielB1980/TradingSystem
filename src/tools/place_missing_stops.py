"""
Place missing stop-loss orders for naked futures positions.

Fetches positions and open orders from Kraken Futures, identifies positions with
no matching stop, and places a reduce-only stop per naked position using a
configurable % distance from entry (default 2%).

With --cancel-all-first: cancels ALL open orders, then places one SL per open
position. Use for "reset: cancel everything, then protect all positions."

Use --dry-run to only print what would be done (no cancels, no placements).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from src.exceptions import OperationalError, DataError
from src.config.config import load_config as get_config
from src.data.kraken_client import KrakenClient
from src.data.symbol_utils import pf_to_unified, position_symbol_matches_order


def _order_is_stop(o: dict, side: str) -> bool:
    """True if order is a reduce-only stop (not TP) for the given position side."""
    t = (o.get("info") or {}).get("orderType") or o.get("type") or o.get("order_type") or ""
    t = str(t).lower()
    if "take_profit" in t or "take-profit" in t:
        return False
    if "stop" not in t and "stop_loss" not in t and t != "stop":
        return False
    ro = o.get("reduceOnly", o.get("reduce_only", False))
    if not ro:
        return False
    order_side = (o.get("side") or "").lower()
    expect = "sell" if side == "long" else "buy"
    return order_side == expect


async def place_missing_stops(
    stop_pct: float = 2.0,
    dry_run: bool = False,
    cancel_all_first: bool = False,
) -> None:
    try:
        config = get_config()
    except (OperationalError, DataError, OSError, ValueError, TypeError, KeyError) as e:
        print(f"Config load failed: {e}")
        return

    client = KrakenClient(
        api_key=config.exchange.api_key,
        api_secret=config.exchange.api_secret,
        futures_api_key=config.exchange.futures_api_key,
        futures_api_secret=config.exchange.futures_api_secret,
        use_testnet=config.exchange.use_testnet,
    )
    await client.initialize()

    try:
        print("Fetching positions and open orders...")
        orders, positions = await asyncio.gather(
            client.get_futures_open_orders(),
            client.get_all_futures_positions(),
        )
        positions = [p for p in positions if float(p.get("size", 0)) != 0]
        print(f"Found {len(orders)} open orders, {len(positions)} non-zero positions.\n")

        if cancel_all_first:
            if dry_run:
                print("[DRY-RUN] Would cancel ALL open orders, then place stops for every position.")
                naked = list(positions)
            else:
                print("Cancelling ALL open orders...")
                cancelled = await client.cancel_all_orders()
                n_ok = sum(1 for r in cancelled if r.get("status") == "cancelled")
                print(f"Cancelled {n_ok} order(s).\n")
                print("Re-fetching positions...")
                positions = await client.get_all_futures_positions()
                positions = [p for p in positions if float(p.get("size", 0)) != 0]
                naked = list(positions)
        else:
            naked = []
            for p in positions:
                pos_sym = p.get("symbol") or ""
                side = (p.get("side") or "long").lower()
                has_stop = False
                for o in orders:
                    if not position_symbol_matches_order(pos_sym, o.get("symbol") or ""):
                        continue
                    if _order_is_stop(o, side):
                        has_stop = True
                        break
                if not has_stop:
                    naked.append(p)

        if not naked:
            print("No naked positions. All positions have a matching stop.")
            return

        print(f"=== NAKED POSITIONS ({len(naked)}) ===")
        pct = Decimal(str(stop_pct))
        for p in naked:
            pos_sym = p.get("symbol") or "?"
            unified = pf_to_unified(pos_sym)
            size = Decimal(str(p.get("size", 0)))
            entry = Decimal(str(p.get("entry_price", 0)))
            side = (p.get("side") or "long").lower()
            if side == "long":
                stop_price = entry * (Decimal("1") - pct / Decimal("100"))
            else:
                stop_price = entry * (Decimal("1") + pct / Decimal("100"))
            close_side = "sell" if side == "long" else "buy"
            print(f"  {unified}  side={side}  size={size}  entry={entry}  stop@ {stop_price} ({stop_pct}%)")

        if dry_run:
            print("\n[DRY-RUN] Would place stop orders as above. Run without --dry-run to place.")
            return

        print("\nPlacing missing stops...")
        placed = 0
        for p in naked:
            pos_sym = p.get("symbol") or "?"
            unified = pf_to_unified(pos_sym)
            if not unified or unified == pos_sym:
                print(f"  Skip {pos_sym}: could not resolve unified symbol")
                continue
            size = Decimal(str(p.get("size", 0)))
            entry = Decimal(str(p.get("entry_price", 0)))
            side = (p.get("side") or "long").lower()
            if side == "long":
                stop_price = entry * (Decimal("1") - pct / Decimal("100"))
            else:
                stop_price = entry * (Decimal("1") + pct / Decimal("100"))
            close_side = "sell" if side == "long" else "buy"
            try:
                await client.place_futures_order(
                    symbol=unified,
                    side=close_side,
                    order_type="stop",
                    size=size,
                    stop_price=stop_price,
                    reduce_only=True,
                )
                print(f"  Placed stop {unified} @ {stop_price}")
                placed += 1
            except (OperationalError, DataError) as e:
                print(f"  Failed {unified}: {e}")
        print(f"\nPlaced {placed} stop(s).")
    except (OperationalError, DataError, OSError) as e:
        print(f"place_missing_stops failed: {e}")
        raise
    finally:
        await client.close()


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Place missing stop-loss orders for naked futures positions."
    )
    ap.add_argument(
        "--stop-pct",
        type=float,
        default=2.0,
        help="Stop distance from entry in %% (default: 2.0). Long: entry*(1-pct/100), short: entry*(1+pct/100)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Only print what would be done; do not place orders.",
    )
    ap.add_argument(
        "--cancel-all-first",
        action="store_true",
        help="Cancel ALL open orders, then place one SL per open position.",
    )
    args = ap.parse_args()
    asyncio.run(
        place_missing_stops(
            stop_pct=args.stop_pct,
            dry_run=args.dry_run,
            cancel_all_first=args.cancel_all_first,
        )
    )


if __name__ == "__main__":
    main()
