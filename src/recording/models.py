"""
SQLAlchemy model for market snapshot recordings.

Append-only table used by the Kraken futures recorder to capture
ticker + candle metadata at regular intervals.  Replay providers
read from this table to drive deterministic backtests.
"""
from sqlalchemy import Column, DateTime, Index, Integer, Numeric, String, Text

from src.storage.db import Base


class MarketSnapshot(Base):
    """One point-in-time snapshot of a single symbol's futures data."""

    __tablename__ = "market_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ts_utc = Column(DateTime(timezone=True), nullable=False)
    symbol = Column(String(64), nullable=False)

    # Futures ticker data
    futures_bid = Column(Numeric(20, 8))
    futures_ask = Column(Numeric(20, 8))
    futures_spread_pct = Column(Numeric(20, 8))
    futures_volume_usd_24h = Column(Numeric(20, 8))

    # Optional enrichment (recorded but not used for gating)
    open_interest_usd = Column(Numeric(20, 8))
    funding_rate = Column(Numeric(20, 10))

    # Candle metadata -- JSON blobs keyed by timeframe
    # e.g. {"4h": "2025-11-06T08:00:00+00:00", "1d": "2025-11-06T00:00:00+00:00"}
    last_candle_ts_json = Column(Text)
    # e.g. {"4h": 260, "1d": 365, "1h": 700, "15m": 1200}
    candle_count_json = Column(Text)

    # Non-null when the snapshot captured an error for this symbol
    error_code = Column(String(128))

    __table_args__ = (
        Index("idx_snapshot_sym_ts", "symbol", "ts_utc"),
    )

    def __repr__(self) -> str:
        return (
            f"<MarketSnapshot symbol={self.symbol} ts={self.ts_utc} "
            f"bid={self.futures_bid} ask={self.futures_ask}>"
        )
