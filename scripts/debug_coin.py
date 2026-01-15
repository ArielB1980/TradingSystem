import os
import sys
import json
from datetime import datetime
from sqlalchemy import create_engine, text

def debug_coin(symbol_fragment):
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("Error: DATABASE_URL not set")
        return

    try:
        engine = create_engine(db_url)
        with engine.connect() as conn:
            print(f"Searching for symbol like '%{symbol_fragment}%' in decision_traces...")
            
            # Find exact symbol name
            query = text("""
                SELECT DISTINCT symbol 
                FROM decision_traces 
                WHERE symbol LIKE :sym
            """)
            result = conn.execute(query, {"sym": f"%{symbol_fragment}%"}).fetchall()
            
            if not result:
                print(f"❌ No traces found for any symbol matching '{symbol_fragment}'")
                return
            
            symbols = [r[0] for r in result]
            print(f"Found symbols: {symbols}")
            
            for sym in symbols:
                print(f"\n--- Analysis for {sym} ---")
                
                # Get latest trace
                trace_query = text("""
                    SELECT timestamp, details 
                    FROM decision_traces 
                    WHERE symbol = :sym 
                    ORDER BY timestamp DESC 
                    LIMIT 1
                """)
                trace = conn.execute(trace_query, {"sym": sym}).fetchone()
                
                if trace:
                    ts = trace[0]
                    details = trace[1]
                    if isinstance(details, str):
                        details = json.loads(details)
                    
                    print(f"Latest Trace: {ts}")
                    print(f"Regime: {details.get('regime', 'N/A')}")
                    print(f"Signal: {details.get('signal', 'N/A')}")
                    
                    # Check age
                    now = datetime.utcnow()
                    if isinstance(ts, str):
                        ts = datetime.fromisoformat(ts.replace('Z', ''))
                    
                    age = (now - ts).total_seconds() / 60
                    print(f"Age: {age:.1f} minutes")
                    
                    if age > 60:
                        print("⚠️  Metric: DEAD/STALE (> 60 mins)")
                    else:
                        print("✅  Metric: ACTIVE")
                else:
                    print("❌ No traces found")

    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python debug_coin.py <SYMBOL>")
        sys.exit(1)
    
    debug_coin(sys.argv[1])
