#!/usr/bin/env python3
"""
Check what the worker is actually saving to the database.
Run this on the server to see recent account_state entries.
"""
import sys
import os
sys.path.append(os.getcwd())

from src.storage.repository import get_db, AccountStateModel
from sqlalchemy import desc
from datetime import datetime, timezone

def check():
    print("=== Recent Account States (Last 10) ===\n")
    db = get_db()
    with db.get_session() as session:
        states = session.query(AccountStateModel).order_by(
            desc(AccountStateModel.timestamp)
        ).limit(10).all()
        
        if not states:
            print("No account states found in database!")
            return
            
        now = datetime.now(timezone.utc)
        for s in states:
            age_seconds = (now - s.timestamp.replace(tzinfo=timezone.utc)).total_seconds()
            age_minutes = int(age_seconds / 60)
            print(f"Time: {s.timestamp} UTC ({age_minutes}m ago)")
            print(f"  Equity: ${s.equity}")
            print(f"  Balance: ${s.balance}")
            print(f"  Margin Used: ${s.margin_used}")
            print(f"  Available: ${s.available_margin}")
            print()

if __name__ == "__main__":
    check()
