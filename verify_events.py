
from src.storage.db import get_db
from src.storage.repository import SystemEventModel
from datetime import datetime, timedelta, timezone

def check_traces():
    db = get_db()
    with db.get_session() as session:
        # Check for events in the last 2 minutes
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=2)
        events = session.query(SystemEventModel).filter(
            SystemEventModel.event_type == 'DECISION_TRACE',
            SystemEventModel.timestamp > cutoff
        ).all()
        
        print(f"Found {len(events)} DECISION_TRACE events in last 2m")
        if events:
            print(f"Sample: {events[0].symbol} - {events[0].details}")

if __name__ == "__main__":
    check_traces()
