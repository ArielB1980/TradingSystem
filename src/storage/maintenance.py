"""
Database maintenance service.

Handles pruning of old data and database optimization to prevent bloat.
"""
from datetime import datetime, timedelta, timezone
from sqlalchemy import text
from src.monitoring.logger import get_logger
from src.storage.db import get_db

logger = get_logger(__name__)

class DatabasePruner:
    """Service for cleaning up old database records."""
    
    def __init__(self):
        self.db = get_db()
        
    def prune_old_traces(self, days_to_keep: int = 3) -> int:
        """
        Delete DECISION_TRACE events older than X days.
        Retains signals and critical errors, only deletes high-frequency traces.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_to_keep)
        
        with self.db.get_session() as session:
            try:
                # Execute deletion
                # Note: We use raw SQL for bulk delete performance or ORM
                # Using ORM filter delete for safety
                from src.storage.repository import SystemEventModel
                
                query = session.query(SystemEventModel).filter(
                    SystemEventModel.event_type == "DECISION_TRACE",
                    SystemEventModel.timestamp < cutoff
                )
                
                count = query.delete(synchronize_session=False)
                session.commit()
                
                logger.info("Pruned old decision traces", count=count, cutoff=cutoff.isoformat())
                return count
                
            except Exception as e:
                session.rollback()
                logger.error("Failed to prune decision traces", error=str(e))
                return 0

    def prune_old_candles(self, days_to_keep: int = 14) -> int:
        """
        Delete 15m candles older than X days.
        Higher timeframes (1h, 4h, 1d) are kept for longer history.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_to_keep)
        
        with self.db.get_session() as session:
            try:
                from src.storage.repository import CandleModel
                
                query = session.query(CandleModel).filter(
                    CandleModel.timeframe == "15m",
                    CandleModel.timestamp < cutoff
                )
                
                count = query.delete(synchronize_session=False)
                session.commit()
                
                logger.info("Pruned old 15m candles", count=count, cutoff=cutoff.isoformat())
                return count
                
            except Exception as e:
                session.rollback()
                logger.error("Failed to prune old candles", error=str(e))
                return 0
    
    def optimize_db(self):
        """Run database specific optimization (VACUUM for SQLite)."""
        # Vacuum is specific to SQLite, but harmless to try if we detect sqlite
        if "sqlite" in str(self.db.engine.url):
            try:
                with self.db.engine.connect() as conn:
                    # connection.execute(text("VACUUM")) automatically commits in some versions, 
                    # but VACUUM cannot run inside a transaction block usually.
                    # For sqlalchemy with sqlite, we need isolation_level="AUTOCOMMIT"
                    conn.execution_options(isolation_level="AUTOCOMMIT").execute(text("VACUUM"))
                    
                logger.info("Database VACUUM completed successfully")
            except Exception as e:
                logger.error("Database VACUUM failed", error=str(e))

    def run_maintenance(self) -> dict:
        """Run all maintenance tasks."""
        logger.info("Starting database maintenance...")
        
        traces_deleted = self.prune_old_traces(days_to_keep=3)
        candles_deleted = self.prune_old_candles(days_to_keep=14)
        
        if traces_deleted > 0 or candles_deleted > 0:
            self.optimize_db()
            
        return {
            "traces_deleted": traces_deleted,
            "candles_deleted": candles_deleted
        }
