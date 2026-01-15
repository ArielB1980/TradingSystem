import os
import sys
import asyncio
from datetime import datetime

# Robustly find project root relative to this script
# Script is in /scripts/, so root is one level up
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)

print(f"DEBUG: Script Dir: {script_dir}")
print(f"DEBUG: Project Root: {project_root}")

if project_root not in sys.path:
    sys.path.insert(0, project_root)

try:
    from src.storage.db import get_db_pool
except ImportError as e:
    print(f"❌ Could not import src modules: {e}")
    print(f"   Contents of {project_root}: {os.listdir(project_root) if os.path.exists(project_root) else 'DIR NOT FOUND'}")
    sys.exit(1)

async def check_freshness():
    print("Connecting to Database...")
    try:
        pool = await get_db_pool()
    except Exception as e:
        print(f"❌ Connection Failed: {e}")
        return

    try:
        async with pool.acquire() as conn:
            print("\n--- GLOBAL FRESHNESS CHECK ---")
            
            # 1. Latest Heartbeat
            row = await conn.fetchrow("""
                SELECT timestamp, symbol, details 
                FROM system_events 
                WHERE event_type = 'DECISION_TRACE' 
                ORDER BY timestamp DESC 
                LIMIT 1
            """)
            
            if row:
                timestamp = row['timestamp']
                symbol = row['symbol']
                # Ensure timezone awareness for diff
                if timestamp.tzinfo is None:
                    # Assume UTC if naive, or use system time naive
                    pass 
                
                # Check server time vs DB time
                db_now = await conn.fetchval("SELECT NOW()")
                age = (db_now - timestamp).total_seconds() / 60
                
                print(f"Latest Trace: {timestamp}")
                print(f"Server Time:  {db_now}")
                print(f"Age:          {age:.1f} minutes")
                print(f"Symbol:       {symbol}")
                
                if age > 15:
                    print(f"❌ CRITICAL: System appears STALLED (No traces in {age:.1f} mins)")
                else:
                    print("✅ System appears RUNNING")
            else:
                print("❌ CRITICAL: No traces found in database whatsoever.")

            # 2. Check Trace Volume
            count = await conn.fetchval("""
                SELECT COUNT(*) 
                FROM system_events 
                WHERE event_type = 'DECISION_TRACE' 
                AND timestamp > NOW() - INTERVAL '1 hour'
            """)
            print(f"Traces in last hour: {count}")
            
            # 3. Check Recent Errors
            print("\n--- RECENT ERRORS (Last 2 Hours) ---")
            errors = await conn.fetch("""
                SELECT timestamp, details 
                FROM system_events 
                WHERE event_type IN ('ERROR', 'CRITICAL') 
                AND timestamp > NOW() - INTERVAL '2 hours'
                ORDER BY timestamp DESC
                LIMIT 5
            """)
            
            if errors:
                for e in errors:
                    print(f"[{e['timestamp']}] {e['details']}")
            else:
                print("✅ No database errors logged in last 2 hours.")

    except Exception as e:
        print(f"❌ Database Query Error: {e}")
    finally:
        await pool.close()

if __name__ == "__main__":
    asyncio.run(check_freshness())
