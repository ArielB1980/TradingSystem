import sys
import os
sys.path.append(os.getcwd())
from src.storage.repository import get_db, SystemEventModel
from datetime import datetime, timezone, timedelta

cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
print(f"Checking count since {cutoff}...", flush=True)
db = get_db()
with db.get_session() as session:
    count = session.query(SystemEventModel).filter(
        SystemEventModel.event_type == "DECISION_TRACE",
        SystemEventModel.timestamp >= cutoff
    ).count()
    print(f"DECISION_TRACE events in last 24h: {count}", flush=True)
