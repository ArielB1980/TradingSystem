import sys
import os
import argparse
from datetime import datetime, timedelta, timezone
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# Add src to path
sys.path.append(os.getcwd())

from src.storage.db import get_db, Base
from src.storage.repository import SystemEventModel, TradeModel, CandleModel

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
    
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=7, help="Lookback hours (default: 7)")
    args = parser.parse_args()

    cutoff = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    
    with db.get_session() as session:
        # Debug Totals
        total_events = session.query(SystemEventModel).count()
        total_trades = session.query(TradeModel).count()
        total_candles = session.query(CandleModel).count()
        print(f"\n--- Total Database State ---")
        print(f"Total System Events: {total_events}")
        print(f"Total Trades: {total_trades}")
        print(f"Total Candles Stored: {total_candles}")
        
        print(f"\n--- Statistics for last {args.hours} hours (since {cutoff.strftime('%Y-%m-%d %H:%M:%S UTC')}) ---")

        # 1. Count Signals
        signals_query = session.query(SystemEventModel).filter(
            SystemEventModel.timestamp >= cutoff,
            SystemEventModel.event_type.in_(['SIGNAL', 'SIGNAL_GENERATED'])
        )
        signal_count = signals_query.count()
        
        # 2. Trades & Performance
        trades_query = session.query(TradeModel).filter(
            TradeModel.exited_at >= cutoff
        ).order_by(TradeModel.exited_at.desc())
        
        trades = trades_query.all()
        trade_count = len(trades)
        
        total_pnl = sum(t.net_pnl for t in trades)
        winners = sum(1 for t in trades if t.net_pnl > 0)
        win_rate = (winners / trade_count * 100) if trade_count > 0 else 0.0

        print(f"Signals Found: {signal_count}")
        print(f"Trades Executed: {trade_count}")
        print(f"Total PnL: ${total_pnl:.2f}")
        print(f"Win Rate: {win_rate:.1f}%")
        
        if trade_count > 0:
            print("\nRecent Trades:")
            for t in trades[:5]:
                print(f"  {t.symbol} ({t.side}): ${t.net_pnl:.2f} [{t.exit_reason}]")

        # 3. Market Coverage
        unique_symbols = session.query(SystemEventModel.symbol).distinct().count()
        print(f"Unique Symbols Active: {unique_symbols}")
        
        if signal_count == 0:
            print("\nDebug: No signals found. Checking recent event types:")
            recent = session.query(SystemEventModel.event_type).distinct().limit(10).all()
            print([r[0] for r in recent])

if __name__ == "__main__":
    main()
