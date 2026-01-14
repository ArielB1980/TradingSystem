import sys
import os
from datetime import datetime, timedelta, timezone
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# Add src to path
sys.path.append(os.getcwd())

from src.storage.db import get_db, Base
from src.storage.repository import SystemEventModel, TradeModel

def main():
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("Error: DATABASE_URL environment variable not set.")
        print("Please export DATABASE_URL='...' and run again.")
        return

    print(f"Connecting to database...")
    
    db = get_db()
    
    # Debug Connection
    url_str = str(db.engine.url)
    masked_url = url_str.split("@")[-1] if "@" in url_str else url_str
    print(f"DB Engine: {db.engine.dialect.name}")
    print(f"DB Host/Info: {masked_url}")
    
    cutoff = datetime.now(timezone.utc) - timedelta(hours=10)
    
    with db.get_session() as session:
        # Debug Totals
        total_events = session.query(SystemEventModel).count()
        total_trades = session.query(TradeModel).count()
        print(f"\n--- Total Database State ---")
        print(f"Total System Events: {total_events}")
        print(f"Total Trades: {total_trades}")

        # 1. Count Signals
        # 1. Count Signals
        # Assuming event_type for signals is 'SIGNAL' or 'SIGNAL_GENERATED'
        # Let's check distinct types to be sure if count is 0
        
        signals_query = session.query(SystemEventModel).filter(
            SystemEventModel.timestamp >= cutoff,
            SystemEventModel.event_type.in_(['SIGNAL', 'SIGNAL_GENERATED'])
        )
        signal_count = signals_query.count()
        
        # 2. Count Trades
        trades_query = session.query(TradeModel).filter(
            TradeModel.entered_at >= cutoff
        )
        trade_count = trades_query.count()
        
        print(f"\n--- Statistics for last 10 hours (since {cutoff.strftime('%Y-%m-%d %H:%M:%S UTC')}) ---")
        print(f"Signals Found: {signal_count}")
        print(f"Trades Executed: {trade_count}")
        
        # 3. Market Coverage
        unique_symbols = session.query(SystemEventModel.symbol).distinct().count()
        print(f"Unique Symbols Active: {unique_symbols}")
        
        if signal_count == 0:
            print("\nDebug: No signals found. Checking recent event types:")
            recent = session.query(SystemEventModel.event_type).distinct().limit(10).all()
            print([r[0] for r in recent])

if __name__ == "__main__":
    main()
