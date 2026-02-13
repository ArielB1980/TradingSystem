#!/usr/bin/env python3
"""
Fetch Kraken Futures trade/fill history for comparison with our logs.

Uses KRAKEN_FUTURES_API_KEY and KRAKEN_FUTURES_API_SECRET from .env.local.
Output: one line per fill (time, symbol, side, amount, price, cost, fee, order_id).

Usage:
  python scripts/fetch_kraken_futures_trades.py [--hours 48] [--symbol ZRO]
  # Default: last 48 hours, all symbols (filter with --symbol for ZRO only).
"""

import argparse
import os
import sys
from datetime import datetime, timezone, timedelta

# Load .env.local (local) or .env (server) before any project imports that might validate env
def _load_env():
    from dotenv import load_dotenv
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in (".env.local", ".env"):
        path = os.path.join(repo, name)
        if os.path.isfile(path):
            load_dotenv(path, override=False)
            break


def main():
    _load_env()
    parser = argparse.ArgumentParser(description="Fetch Kraken Futures trades for comparison")
    parser.add_argument("--hours", type=float, default=48, help="Fetch trades from last N hours (default 48)")
    parser.add_argument("--symbol", type=str, default="", help="Filter by symbol (e.g. ZRO or PF_ZROUSD). Empty = all.")
    args = parser.parse_args()

    api_key = os.getenv("KRAKEN_FUTURES_API_KEY", "").strip()
    api_secret = os.getenv("KRAKEN_FUTURES_API_SECRET", "").strip()
    if not api_key or not api_secret:
        print("Error: KRAKEN_FUTURES_API_KEY and KRAKEN_FUTURES_API_SECRET must be set (e.g. in .env or .env.local)", file=sys.stderr)
        sys.exit(1)

    import ccxt
    since_dt = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    since_ts = int(since_dt.timestamp() * 1000)

    exchange = ccxt.krakenfutures({
        "apiKey": api_key,
        "secret": api_secret,
        "enableRateLimit": True,
        "options": {"defaultType": "future"},
    })

    symbol_filter = (args.symbol.upper().replace("/", "") if args.symbol else "")

    print(f"# Kraken Futures fills since {since_dt.isoformat()} UTC (last {args.hours}h)", flush=True)
    print("# Columns: datetime_utc | symbol | side | amount | price | cost | fee | order_id", flush=True)
    print("-" * 100, flush=True)

    try:
        # fetch_my_trades: optional symbol (CCXT unified e.g. ZRO/USD:USD), since (ms), limit
        trades = exchange.fetch_my_trades(since=since_ts, limit=500)
    except Exception as e:
        print(f"Error fetching trades: {e}", file=sys.stderr)
        sys.exit(1)

    shown = 0
    for t in sorted(trades, key=lambda x: x["timestamp"]):
        sym = (t.get("symbol") or "").upper()
        if symbol_filter and symbol_filter not in sym.replace("/", "").replace(":", "").replace("-", ""):
            continue
        shown += 1
        ts = datetime.fromtimestamp(t["timestamp"] / 1000, tz=timezone.utc)
        side = (t.get("side") or "").lower()
        amount = t.get("amount") or 0
        price = t.get("price") or 0
        cost = t.get("cost") or 0
        fee = (t.get("fee") or {}).get("cost") or 0
        order_id = t.get("order") or t.get("id") or ""
        print(f"{ts.isoformat()} | {sym} | {side} | {amount} | {price} | {cost} | {fee} | {order_id}", flush=True)

    print("-" * 100, flush=True)
    print(f"# Total fills shown: {shown}", flush=True)


if __name__ == "__main__":
    main()
