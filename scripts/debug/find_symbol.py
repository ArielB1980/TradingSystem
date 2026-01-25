import sys
import os
sys.path.append(os.getcwd())
from src.storage.repository import get_db, SystemEventModel
from sqlalchemy import text

db = get_db()
with db.get_session() as session:
    # Find active symbols matching CHZ
    sql = text("SELECT DISTINCT symbol FROM system_events WHERE symbol LIKE '%CHZ%' LIMIT 5")
    result = session.execute(sql)
    for row in result:
        print(f"Found symbol: {row[0]}")
