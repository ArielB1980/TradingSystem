import sys
import os
import json
from datetime import datetime
import argparse

sys.path.append(os.getcwd())
from src.storage.repository import get_db, SystemEventModel
from sqlalchemy import desc

def print_trace(symbol):
    print(f"Querying for {symbol}...", flush=True)
    db = get_db()
    with db.get_session() as session:
        event = session.query(SystemEventModel).filter(
            SystemEventModel.symbol == symbol,
            SystemEventModel.event_type == "DECISION_TRACE"
        ).order_by(desc(SystemEventModel.timestamp)).first()
        
        if not event:
            print(f"No decision trace found for {symbol}", flush=True)
            return

        try:
            d = json.loads(event.details)
        except:
            d = {}
        
        print(f"\n--- Analysis for {symbol} at {event.timestamp} ---", flush=True)
        print(f"Signal:   {d.get('signal')}", flush=True)
        print(f"Regime:   {d.get('regime')}", flush=True)
        print(f"Bias:     {d.get('bias')}", flush=True)
        print(f"Quality:  {d.get('setup_quality', 0)}", flush=True)
        print(f"ADX:      {d.get('adx', 0)}", flush=True)
        
        print("\n[Reasoning]", flush=True)
        reasoning = d.get('reasoning', [])
        if isinstance(reasoning, list):
            print("\n".join(reasoning), flush=True)
        else:
            print(reasoning, flush=True)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python get_trace.py SYMBOL")
        sys.exit(1)
        
    sym = sys.argv[1].upper()
    if "/" not in sym:
        sym += "/USD"
        
    print_trace(sym)
