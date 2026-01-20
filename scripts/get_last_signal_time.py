#!/usr/bin/env python3
"""
Find Last Signal Time
Connects to the database and finds the timestamp of the last generated signal.
"""
import os
import sys
import json
from datetime import datetime
from sqlalchemy import create_engine, text, desc

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

def get_last_signal():
    db_url = os.environ.get('DATABASE_URL')
    if not db_url:
        print("Error: DATABASE_URL not set")
        return

    try:
        engine = create_engine(db_url)
        with engine.connect() as conn:
            # Query system_events for latest DECISION_TRACE with a signal
            query = text("""
                SELECT timestamp, details 
                FROM system_events 
                WHERE event_type = 'DECISION_TRACE' 
                ORDER BY timestamp DESC 
                LIMIT 50
            """)
            
            result = conn.execute(query)
            
            print(f"Checking last 50 decision events for signals...")
            found_signal = False
            
            for row in result:
                timestamp = row[0]
                details = row[1]
                
                try:
                    if isinstance(details, str):
                        data = json.loads(details)
                    else:
                        data = details
                        
                    signal = data.get('signal')
                    if signal and signal.upper() in ['LONG', 'SHORT']:
                        print(f"\n✅ FOUND LAST SIGNAL:")
                        print(f"Time: {timestamp}")
                        print(f"Symbol: {data.get('symbol', 'Unknown')}")
                        print(f"Type: {signal}")
                        print(f"Quality: {data.get('setup_quality', 'N/A')}")
                        found_signal = True
                        break
                except Exception as e:
                    continue
            
            if not found_signal:
                print("\n❌ No signals found in the last 50 decision events.")
                
            # Also check if we can just query for any event that contains "signal"
            # This is a fallback if event_type usage changed
            
    except Exception as e:
        print(f"Database error: {e}")

if __name__ == "__main__":
    get_last_signal()
