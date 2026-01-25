#!/usr/bin/env python3
"""Check TP order coverage for all open positions."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.storage.db import get_db
from src.storage.repository import PositionModel
from decimal import Decimal

def main():
    db = get_db()
    with db.get_session() as session:
        # Get all positions (no status field, just get all)
        positions = session.query(PositionModel).all()
        
        print(f"\n=== TP Coverage Report ===")
        print(f"Total open positions: {len(positions)}\n")
        
        missing_sl = []
        missing_tp = []
        has_tp = []
        
        for p in positions:
            has_sl = bool(p.stop_loss_order_id or p.initial_stop_price)
            has_tp_plan = bool(p.tp1_price or p.tp2_price)
            has_tp_ids = bool(p.tp_order_ids and len(p.tp_order_ids) > 0)
            
            if not has_sl:
                missing_sl.append(p.symbol)
            elif not (has_tp_plan or has_tp_ids):
                missing_tp.append(p.symbol)
            else:
                has_tp.append(p.symbol)
        
        print(f"Positions with TP coverage: {len(has_tp)}")
        print(f"Positions missing TP: {len(missing_tp)}")
        print(f"Positions missing SL: {len(missing_sl)}\n")
        
        if missing_sl:
            print(f"Missing SL ({len(missing_sl)}):")
            for s in missing_sl[:10]:
                print(f"  - {s}")
            if len(missing_sl) > 10:
                print(f"  ... and {len(missing_sl) - 10} more")
        
        if missing_tp:
            print(f"\nMissing TP ({len(missing_tp)}):")
            for s in missing_tp[:10]:
                print(f"  - {s}")
            if len(missing_tp) > 10:
                print(f"  ... and {len(missing_tp) - 10} more")

if __name__ == "__main__":
    main()
