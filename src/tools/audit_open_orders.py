"""
Audit open futures orders: total count, breakdown by type (stop vs take_profit vs limit),
per-symbol summary, and flags for multiple stops per symbol (suspicious).

Use --cancel-redundant-stops to keep one stop per symbol (most protective) and cancel the rest.
Use --cancel-orphaned-stops to cancel all stop orders when there are 0 positions (orphaned).
"""
import argparse
import asyncio
import os
import sys
from collections import defaultdict
from decimal import Decimal

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from src.data.kraken_client import KrakenClient
from src.config.config import load_config as get_config


def _order_type(o: dict) -> str:
    t = (o.get("info") or {}).get("orderType") or o.get("type") or o.get("order_type") or "unknown"
    return str(t).lower()


def _pf_to_unified(s: str) -> str:
    """PF_ADAUSD -> ADA/USD:USD."""
    if not s or not s.startswith("PF_") or not s.endswith("USD"):
        return s
    base = s[3:-3]
    return f"{base}/USD:USD"


def _stop_price(o: dict) -> Decimal | None:
    v = o.get("stopPrice") or o.get("price") or o.get("triggerPrice")
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except Exception:
        return None


async def audit_open_orders(cancel_redundant: bool = False, cancel_orphaned: bool = False):
    try:
        config = get_config()
    except Exception as e:
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
        print("Fetching open futures orders and positions...")
        orders, positions = await asyncio.gather(
            client.get_futures_open_orders(),
            client.get_all_futures_positions(),
        )
        n_positions = len([p for p in positions if float(p.get("size", 0)) != 0])
        print(f"Found {len(orders)} open orders, {n_positions} non-zero positions.\n")

        by_type: dict[str, int] = defaultdict(int)
        by_symbol: dict[str, list[dict]] = defaultdict(list)
        stops_per_symbol: dict[str, list[dict]] = defaultdict(list)

        for o in orders:
            t = _order_type(o)
            by_type[t] += 1
            sym = o.get("symbol") or "?"
            by_symbol[sym].append(o)
            if "stop" in t and "take_profit" not in t and "take-profit" not in t:
                stops_per_symbol[sym].append(o)

        print("=== BY TYPE ===")
        for t, n in sorted(by_type.items(), key=lambda x: -x[1]):
            print(f"  {t}: {n}")

        print("\n=== PER SYMBOL (total) ===")
        for sym in sorted(by_symbol.keys()):
            n = len(by_symbol[sym])
            stops = len(stops_per_symbol.get(sym) or [])
            tail = f"  [stops: {stops}]" if stops else ""
            print(f"  {sym}: {n}{tail}")

        print("\n=== MULTIPLE STOPS (suspicious) ===")
        multi_stops = {s: lst for s, lst in stops_per_symbol.items() if len(lst) > 1}
        if not multi_stops:
            print("  None. Each symbol has 0 or 1 stop.")
        else:
            for sym, lst in sorted(multi_stops.items()):
                print(f"  [!] {sym}: {len(lst)} stop orders")
                for o in lst:
                    print(f"      id={o.get('id')} stop={o.get('stopPrice') or o.get('price')} size={o.get('amount')}")

        all_stops: list[tuple[str, dict]] = []
        for sym, lst in stops_per_symbol.items():
            for o in lst:
                all_stops.append((sym, o))

        if n_positions == 0 and all_stops:
            print("\n=== ORPHANED STOPS (0 positions) ===")
            print(f"  {len(all_stops)} stop(s) with no position to protect. Orphaned.")
            if cancel_orphaned:
                print("  Cancelling orphaned stops...")
                cancelled = 0
                for sym, o in all_stops:
                    try:
                        await client.cancel_futures_order(o["id"], sym)
                        print(f"  Cancelled {o['id']} ({sym})")
                        cancelled += 1
                    except Exception as e:
                        print(f"  Failed to cancel {o['id']} ({sym}): {e}")
                print(f"  Cancelled {cancelled} orphaned stop(s).")
            else:
                print("  Run with --cancel-orphaned-stops to cancel them.")

        if cancel_redundant and multi_stops:
            unified_to_side = {}
            for p in positions:
                if float(p.get("size", 0)) == 0:
                    continue
                u = _pf_to_unified(p.get("symbol") or "")
                if u:
                    unified_to_side[u] = (p.get("side") or "long").lower()
            print("\n=== CANCEL REDUNDANT STOPS ===")
            cancelled = 0
            for sym, lst in sorted(multi_stops.items()):
                side = unified_to_side.get(sym, "long")
                with_price = [(o, _stop_price(o)) for o in lst]
                with_price = [(o, p) for o, p in with_price if p is not None]
                if len(with_price) < len(lst):
                    keep = lst[0]
                else:
                    if side == "long":
                        keep = max(with_price, key=lambda x: x[1])[0]
                    else:
                        keep = min(with_price, key=lambda x: x[1])[0]
                for o in lst:
                    if o is keep:
                        continue
                    try:
                        await client.cancel_futures_order(o["id"], sym)
                        print(f"  Cancelled {o['id']} ({sym})")
                        cancelled += 1
                    except Exception as e:
                        print(f"  Failed to cancel {o['id']} ({sym}): {e}")
            print(f"  Cancelled {cancelled} redundant stop(s).")

        print("\n=== EXPECTED RANGE ===")
        lo, hi = n_positions, n_positions * 4
        print(f"  With {n_positions} positions: 1 SL + up to 3 TPs each -> ~{lo}–{hi} orders. Yours: {len(orders)}.")
    except Exception as e:
        print(f"Audit failed: {e}")
        import traceback

        traceback.print_exc()
    finally:
        await client.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Audit open futures orders.")
    ap.add_argument("--cancel-redundant-stops", action="store_true", help="Keep one stop per symbol, cancel the rest.")
    ap.add_argument("--cancel-orphaned-stops", action="store_true", help="Cancel all stops when 0 positions (orphaned).")
    args = ap.parse_args()
    asyncio.run(audit_open_orders(cancel_redundant=args.cancel_redundant_stops, cancel_orphaned=args.cancel_orphaned_stops))
