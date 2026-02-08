"""
Database maintenance service.

Handles pruning of old data and database optimization to prevent bloat.

Retention policies:
  - 15m candles: 30 days
  - 1h candles:  90 days
  - 4h candles:  365 days (1 year)
  - 1d candles:  kept indefinitely
  - DECISION_TRACE events: 3 days
"""
from datetime import datetime, timedelta, timezone
from typing import Dict

from sqlalchemy import text, func

from src.monitoring.logger import get_logger
from src.storage.db import get_db

logger = get_logger(__name__)

# Candle retention policies: timeframe -> max age in days.
# 1d candles are intentionally omitted (kept forever).
CANDLE_RETENTION_DAYS: Dict[str, int] = {
    "15m": 30,
    "1h": 90,
    "4h": 365,
}


class DatabasePruner:
    """Service for cleaning up old database records."""

    def __init__(self):
        """Initialize pruner. Database connection is lazy-loaded when needed."""
        self._db = None

    @property
    def db(self):
        """Lazy-load database connection when first accessed."""
        if self._db is None:
            self._db = get_db()
        return self._db

    def prune_old_traces(self, days_to_keep: int = 3) -> int:
        """
        Delete DECISION_TRACE events older than *days_to_keep* days.
        Retains signals and critical errors, only deletes high-frequency traces.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_to_keep)

        with self.db.get_session() as session:
            try:
                from src.storage.repository import SystemEventModel

                query = session.query(SystemEventModel).filter(
                    SystemEventModel.event_type == "DECISION_TRACE",
                    SystemEventModel.timestamp < cutoff,
                )

                count = query.delete(synchronize_session=False)
                session.commit()

                logger.info(
                    "Pruned old decision traces",
                    count=count,
                    cutoff=cutoff.isoformat(),
                )
                return count

            except Exception as e:
                session.rollback()
                logger.error("Failed to prune decision traces", error=str(e))
                return 0

    def prune_old_candles(self) -> int:
        """
        Delete candles older than their timeframe-specific retention period.

        Retention policies (defined in ``CANDLE_RETENTION_DAYS``):
          - 15m: 30 days
          - 1h:  90 days
          - 4h:  365 days (1 year)
          - 1d:  kept indefinitely

        Returns the total number of rows deleted across all timeframes.
        """
        from src.storage.repository import CandleModel

        total_deleted = 0
        now = datetime.now(timezone.utc)

        with self.db.get_session() as session:
            for timeframe, max_days in CANDLE_RETENTION_DAYS.items():
                cutoff = now - timedelta(days=max_days)
                try:
                    count = (
                        session.query(CandleModel)
                        .filter(
                            CandleModel.timeframe == timeframe,
                            CandleModel.timestamp < cutoff,
                        )
                        .delete(synchronize_session=False)
                    )
                    if count > 0:
                        logger.info(
                            "Pruned old candles",
                            timeframe=timeframe,
                            count=count,
                            retention_days=max_days,
                            cutoff=cutoff.isoformat(),
                        )
                    total_deleted += count
                except Exception as e:
                    logger.error(
                        "Failed to prune candles",
                        timeframe=timeframe,
                        error=str(e),
                    )

            if total_deleted > 0:
                try:
                    session.commit()
                except Exception as e:
                    session.rollback()
                    logger.error(
                        "Failed to commit candle pruning", error=str(e)
                    )
                    return 0

        return total_deleted

    def log_table_stats(self) -> Dict[str, int]:
        """
        Log row counts for each major table.
        Useful for monitoring database growth over time.
        """
        from src.storage.repository import (
            CandleModel,
            TradeModel,
            PositionModel,
            SystemEventModel,
            AccountStateModel,
        )

        stats: Dict[str, int] = {}
        with self.db.get_session() as session:
            try:
                for model, name in [
                    (CandleModel, "candles"),
                    (TradeModel, "trades"),
                    (PositionModel, "positions"),
                    (SystemEventModel, "system_events"),
                    (AccountStateModel, "account_state"),
                ]:
                    stats[name] = session.query(func.count()).select_from(model).scalar() or 0

                # Candle breakdown by timeframe
                candle_tf_counts = (
                    session.query(
                        CandleModel.timeframe,
                        func.count(),
                    )
                    .group_by(CandleModel.timeframe)
                    .all()
                )
                for tf, cnt in candle_tf_counts:
                    stats[f"candles_{tf}"] = cnt

                logger.info("DB_TABLE_STATS", **stats)
            except Exception as e:
                logger.warning("Failed to gather table stats", error=str(e))

        return stats

    def run_maintenance(self) -> dict:
        """Run all maintenance tasks and log table stats."""
        logger.info("Starting database maintenance...")

        traces_deleted = self.prune_old_traces(days_to_keep=3)
        candles_deleted = self.prune_old_candles()

        # Log table sizes for monitoring
        self.log_table_stats()

        return {
            "traces_deleted": traces_deleted,
            "candles_deleted": candles_deleted,
        }
