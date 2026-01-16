"""
Persistence functions for storing and retrieving trading data.

Provides repository pattern for clean data access.
"""
from sqlalchemy import Column, String, Numeric, DateTime, Integer, Boolean, Index, UniqueConstraint
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import List, Optional, Dict, Tuple, Any
import json
from src.storage.db import Base, get_db
from src.domain.models import Candle, Trade, Position


# Query Cache
class QueryCache:
    """Simple time-based query cache."""
    
    def __init__(self, ttl_seconds: int = 60):
        self.cache: Dict[Tuple, Tuple[Any, datetime]] = {}
        self.ttl = timedelta(seconds=ttl_seconds)
        self.max_size = 1000
    
    def get(self, key: Tuple) -> Optional[Any]:
        """Get cached result if not expired."""
        if key in self.cache:
            value, timestamp = self.cache[key]
            if datetime.now(timezone.utc) - timestamp < self.ttl:
                return value
            else:
                del self.cache[key]
        return None
    
    def set(self, key: Tuple, value: Any):
        """Cache a query result."""
        self.cache[key] = (value, datetime.now(timezone.utc))
        
        # Periodic cleanup
        if len(self.cache) >= self.max_size:
            self._cleanup()
    
    def _cleanup(self):
        """Remove expired entries."""
        now = datetime.now(timezone.utc)
        cutoff = now - self.ttl
        
        expired_keys = [
            k for k, (v, ts) in self.cache.items()
            if now - ts >= self.ttl
        ]
        for k in expired_keys:
            del self.cache[k]
        
        # If still too large, remove oldest
        if len(self.cache) >= self.max_size:
            sorted_items = sorted(
                self.cache.items(),
                key=lambda x: x[1][1]
            )
            self.cache = dict(sorted_items[-self.max_size//2:])
    
    def clear(self):
        """Clear all cached entries."""
        self.cache.clear()


# Global cache instance
_query_cache = QueryCache(ttl_seconds=60)


# ORM Models
class CandleModel(Base):
    """ORM model for OHLCV candles - OPTIMIZED with composite indexes."""
    __tablename__ = "candles"
    __table_args__ = (
        # Composite index for common query pattern - 5-10x faster queries
        Index('idx_candle_lookup', 'symbol', 'timeframe', 'timestamp'),
        # Unique constraint to prevent duplicates at database level
        UniqueConstraint('symbol', 'timeframe', 'timestamp', name='uq_candle_key'),
    )
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, nullable=False)
    symbol = Column(String, nullable=False)
    timeframe = Column(String, nullable=False)
    open = Column(Numeric(precision=20, scale=8), nullable=False)
    high = Column(Numeric(precision=20, scale=8), nullable=False)
    low = Column(Numeric(precision=20, scale=8), nullable=False)
    close = Column(Numeric(precision=20, scale=8), nullable=False)
    volume = Column(Numeric(precision=20, scale=8), nullable=False)


class TradeModel(Base):
    """ORM model for completed trades - OPTIMIZED with indexes."""
    __tablename__ = "trades"
    __table_args__ = (
        # Indexes for common query patterns
        Index('idx_trade_symbol_date', 'symbol', 'entered_at'),
        Index('idx_trade_exit_reason', 'exit_reason'),
        Index('idx_trade_pnl', 'net_pnl'),
    )
    
    trade_id = Column(String, primary_key=True)
    symbol = Column(String, nullable=False)
    side = Column(String, nullable=False)
    entry_price = Column(Numeric(precision=20, scale=8), nullable=False)
    exit_price = Column(Numeric(precision=20, scale=8), nullable=False)
    size_notional = Column(Numeric(precision=20, scale=2), nullable=False)
    leverage = Column(Numeric(precision=10, scale=2), nullable=False)
    
    gross_pnl = Column(Numeric(precision=20, scale=2), nullable=False)
    fees = Column(Numeric(precision=20, scale=2), nullable=False)
    funding = Column(Numeric(precision=20, scale=2), nullable=False)
    net_pnl = Column(Numeric(precision=20, scale=2), nullable=False)
    
    entered_at = Column(DateTime, nullable=False)
    exited_at = Column(DateTime, nullable=False)
    holding_period_hours = Column(Numeric(precision=10, scale=2), nullable=False)
    exit_reason = Column(String, nullable=False)


class PositionModel(Base):
    """ORM model for open positions (state tracking)."""
    __tablename__ = "positions"
    
    symbol = Column(String, primary_key=True)
    side = Column(String, nullable=False)
    size = Column(Numeric(precision=20, scale=8), nullable=False)
    size_notional = Column(Numeric(precision=20, scale=2), nullable=False)
    entry_price = Column(Numeric(precision=20, scale=8), nullable=False)
    current_mark_price = Column(Numeric(precision=20, scale=8), nullable=False)
    liquidation_price = Column(Numeric(precision=20, scale=8), nullable=False)
    unrealized_pnl = Column(Numeric(precision=20, scale=2), nullable=False)
    leverage = Column(Numeric(precision=10, scale=2), nullable=False)
    margin_used = Column(Numeric(precision=20, scale=2), nullable=False)
    
    stop_loss_order_id = Column(String, nullable=True)
    take_profit_order_id = Column(String, nullable=True)
    
    # Active Trade Management fields
    initial_stop_price = Column(Numeric(precision=20, scale=8), nullable=True)
    tp1_price = Column(Numeric(precision=20, scale=8), nullable=True)
    tp2_price = Column(Numeric(precision=20, scale=8), nullable=True)
    final_target_price = Column(Numeric(precision=20, scale=8), nullable=True)
    
    opened_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class SystemEventModel(Base):
    """ORM model for system events (audit trail) - OPTIMIZED with indexes."""
    __tablename__ = "system_events"
    __table_args__ = (
        Index('idx_event_type_time', 'event_type', 'timestamp'),
        Index('idx_event_decision', 'decision_id'),
        Index('idx_event_symbol', 'symbol', 'timestamp'),
    )
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, nullable=False)
    event_type = Column(String, nullable=False)
    symbol = Column(String, nullable=False)
    decision_id = Column(String, nullable=True)
    details = Column(String, nullable=False) # JSON string


class AccountStateModel(Base):
    """ORM model for account balance tracking - OPTIMIZED with index."""
    __tablename__ = "account_state"
    __table_args__ = (
        Index('idx_account_timestamp', 'timestamp'),
    )
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, nullable=False)
    equity = Column(Numeric(precision=20, scale=2), nullable=False)
    balance = Column(Numeric(precision=20, scale=2), nullable=False)
    margin_used = Column(Numeric(precision=20, scale=2), nullable=False)
    available_margin = Column(Numeric(precision=20, scale=2), nullable=False)
    unrealized_pnl = Column(Numeric(precision=20, scale=2), nullable=False)


# Repository Functions
def save_candle(candle: Candle) -> None:
    """Save a candle to the database."""
    db = get_db()
    with db.get_session() as session:
        candle_model = CandleModel(
            timestamp=candle.timestamp,
            symbol=candle.symbol,
            timeframe=candle.timeframe,
            open=candle.open,
            high=candle.high,
            low=candle.low,
            close=candle.close,
            volume=candle.volume,
        )
        session.add(candle_model)


from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy import insert as generic_insert

def save_candles_bulk(candles: List[Candle]) -> int:
    """
    Save multiple candles to the database using atomic Upsert.
    Database-agnostic: works with both PostgreSQL and SQLite.
    
    Args:
        candles: List of Candle objects
        
    Returns:
        Number of candles processed
    """
    if not candles:
        return 0
        
    db = get_db()
    
    # Prepare data for bulk insert
    values = [
        {
            "timestamp": c.timestamp,
            "symbol": c.symbol,
            "timeframe": c.timeframe,
            "open": c.open,
            "high": c.high,
            "low": c.low,
            "close": c.close,
            "volume": c.volume
        }
        for c in candles
    ]
    
    # Detect database type
    is_postgres = db.database_url.startswith("postgresql")
    
    with db.get_session() as session:
        if is_postgres:
            # PostgreSQL: Use ON CONFLICT DO UPDATE
            stmt = pg_insert(CandleModel).values(values)
            stmt = stmt.on_conflict_do_update(
                constraint='uq_candle_key',
                set_={
                    "open": stmt.excluded.open,
                    "high": stmt.excluded.high,
                    "low": stmt.excluded.low,
                    "close": stmt.excluded.close,
                    "volume": stmt.excluded.volume
                }
            )
            session.execute(stmt)
        else:
            # SQLite: Use INSERT OR REPLACE (upsert equivalent)
            # Note: SQLite doesn't support ON CONFLICT with partial updates well
            # So we do individual upserts for each candle
            for value in values:
                # Check if exists
                existing = session.query(CandleModel).filter(
                    CandleModel.symbol == value["symbol"],
                    CandleModel.timeframe == value["timeframe"],
                    CandleModel.timestamp == value["timestamp"]
                ).first()
                
                if existing:
                    # Update
                    existing.open = value["open"]
                    existing.high = value["high"]
                    existing.low = value["low"]
                    existing.close = value["close"]
                    existing.volume = value["volume"]
                else:
                    # Insert
                    candle_model = CandleModel(**value)
                    session.add(candle_model)
            
    return len(candles)




def get_candles(
    symbol: str,
    timeframe: str,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    limit: Optional[int] = None,
) -> List[Candle]:
    """
    Retrieve candles from the database - OPTIMIZED with caching.
    
    Args:
        symbol: Symbol (spot)
        timeframe: Timeframe string
        start_time: Optional start time (inclusive)
        end_time: Optional end time (inclusive)
        limit: Optional limit on number of candles
    
    Returns:
        List of Candle objects
    """
    # Check cache first
    cache_key = (symbol, timeframe, start_time, end_time, limit)
    cached_result = _query_cache.get(cache_key)
    if cached_result is not None:
        return cached_result
    
    # Query database
    result = _get_candles_from_db(symbol, timeframe, start_time, end_time, limit)
    
    # Cache result
    _query_cache.set(cache_key, result)
    
    return result


def get_latest_candle_timestamp(symbol: str, timeframe: str) -> Optional[datetime]:
    """
    Get timestamp of the most recent candle for a symbol/timeframe.
    Optimized for incremental fetching.
    """
    db = get_db()
    with db.get_session() as session:
        # Use simple query with order_by desc
        result = session.query(CandleModel.timestamp).filter(
            CandleModel.symbol == symbol,
            CandleModel.timeframe == timeframe
        ).order_by(CandleModel.timestamp.desc()).first()
        
        if result:
            return result[0].replace(tzinfo=timezone.utc)
        return None
    
    
def count_candles(symbol: str, timeframe: str) -> int:
    """
    Count candles for a symbol/timeframe.
    Used for dashboard data depth verification.
    """
    try:
        session = get_db()
        try:
            return session.query(CandleModel).filter(
                CandleModel.symbol == symbol,
                CandleModel.timeframe == timeframe
            ).count()
        finally:
            session.close()
    except Exception as e:
        # Don't log error here to avoid spam, just return 0
        return 0


def _get_candles_from_db(
    symbol: str,
    timeframe: str,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    limit: Optional[int] = None,
) -> List[Candle]:
    """Internal function to query candles from database."""
    db = get_db()
    with db.get_session() as session:
        query = session.query(CandleModel).filter(
            CandleModel.symbol == symbol,
            CandleModel.timeframe == timeframe,
        )
        
        if start_time:
            query = query.filter(CandleModel.timestamp >= start_time)
        if end_time:
            query = query.filter(CandleModel.timestamp <= end_time)
        
        query = query.order_by(CandleModel.timestamp.asc())
        
        if limit:
            query = query.limit(limit)
        
        candle_models = query.all()
        
        return [
            Candle(
                timestamp=cm.timestamp.replace(tzinfo=timezone.utc),
                symbol=cm.symbol,
                timeframe=cm.timeframe,
                open=Decimal(str(cm.open)),
                high=Decimal(str(cm.high)),
                low=Decimal(str(cm.low)),
                close=Decimal(str(cm.close)),
                volume=Decimal(str(cm.volume)),
            )
            for cm in candle_models
        ]


def load_candles_map(
    symbols: List[str],
    timeframe: str,
    days: int = 30
) -> dict:
    """
    Bulk load candles for multiple symbols into a map.
    Optimized for startup hydration.
    
    Args:
        symbols: List of symbols to load
        timeframe: Timeframe string (e.g. "15m")
        days: Number of days of history to load
        
    Returns:
        Dict[symbol, List[Candle]]
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    db = get_db()
    results = {}
    
    # Pre-initialize dict
    for s in symbols:
        results[s] = []
        
    with db.get_session() as session:
        # Query efficient: Get all candles for these symbols/tf since cutoff
        # Chunking symbols to prevent query overflow if list is huge
        chunk_size = 50
        
        for i in range(0, len(symbols), chunk_size):
            chunk = symbols[i:i + chunk_size]
            
            models = session.query(CandleModel).filter(
                CandleModel.symbol.in_(chunk),
                CandleModel.timeframe == timeframe,
                CandleModel.timestamp >= cutoff
            ).order_by(CandleModel.timestamp.asc()).all()
            
            for cm in models:
                c = Candle(
                    timestamp=cm.timestamp.replace(tzinfo=timezone.utc),
                    symbol=cm.symbol,
                    timeframe=cm.timeframe,
                    open=Decimal(str(cm.open)),
                    high=Decimal(str(cm.high)),
                    low=Decimal(str(cm.low)),
                    close=Decimal(str(cm.close)),
                    volume=Decimal(str(cm.volume)),
                )
                if c.symbol in results:
                    results[c.symbol].append(c)
                    
    return results

def save_trade(trade: Trade) -> None:
    """Save a completed trade to the database."""
    db = get_db()
    with db.get_session() as session:
        trade_model = TradeModel(
            trade_id=trade.trade_id,
            symbol=trade.symbol,
            side=trade.side.value,
            entry_price=trade.entry_price,
            exit_price=trade.exit_price,
            size_notional=trade.size_notional,
            leverage=trade.leverage,
            gross_pnl=trade.gross_pnl,
            fees=trade.fees,
            funding=trade.funding,
            net_pnl=trade.net_pnl,
            entered_at=trade.entered_at,
            exited_at=trade.exited_at,
            holding_period_hours=trade.holding_period_hours,
            exit_reason=trade.exit_reason,
        )
        session.add(trade_model)


def save_position(position: Position) -> None:
    """Save or update position state."""
    db = get_db()
    with db.get_session() as session:
        position_model = session.query(PositionModel).filter(
            PositionModel.symbol == position.symbol
        ).first()
        
        if position_model:
            # Update existing
            position_model.side = position.side.value
            position_model.size = position.size
            position_model.size_notional = position.size_notional
            position_model.entry_price = position.entry_price
            position_model.current_mark_price = position.current_mark_price
            position_model.liquidation_price = position.liquidation_price
            position_model.unrealized_pnl = position.unrealized_pnl
            position_model.leverage = position.leverage
            position_model.margin_used = position.margin_used
            position_model.stop_loss_order_id = position.stop_loss_order_id
            position_model.take_profit_order_id = position.take_profit_order_id
            # Management fields
            position_model.initial_stop_price = position.initial_stop_price
            position_model.tp1_price = position.tp1_price
            position_model.tp2_price = position.tp2_price
            position_model.final_target_price = position.final_target_price
            position_model.updated_at = datetime.utcnow()
        else:
            # Create new
            position_model = PositionModel(
                symbol=position.symbol,
                side=position.side.value,
                size=position.size,
                size_notional=position.size_notional,
                entry_price=position.entry_price,
                current_mark_price=position.current_mark_price,
                liquidation_price=position.liquidation_price,
                unrealized_pnl=position.unrealized_pnl,
                leverage=position.leverage,
                margin_used=position.margin_used,
                stop_loss_order_id=position.stop_loss_order_id,
                take_profit_order_id=position.take_profit_order_id,
                # Management fields
                initial_stop_price=position.initial_stop_price,
                tp1_price=position.tp1_price,
                tp2_price=position.tp2_price,
                final_target_price=position.final_target_price,
                opened_at=position.opened_at,
            )
            session.add(position_model)


def delete_position(symbol: str) -> None:
    """Delete a position (when closed)."""
    db = get_db()
    with db.get_session() as session:
        session.query(PositionModel).filter(PositionModel.symbol == symbol).delete()


def sync_active_positions(positions: List[Position]) -> None:
    """
    Sync database with current active positions.
    Updates existing, creates new, and removes closed (missing) positions.
    """
    db = get_db()
    with db.get_session() as session:
        # 1. Get all DB positions
        db_positions = session.query(PositionModel).all()
        db_symbols = {p.symbol for p in db_positions}
        active_symbols = {p.symbol for p in positions}
        
        # 2. Identify positions to remove (in DB but not in active list)
        to_remove = db_symbols - active_symbols
        if to_remove:
            session.query(PositionModel).filter(PositionModel.symbol.in_(to_remove)).delete(synchronize_session=False)
            
        # 3. Update/Create active positions
        for pos in positions:
            # We can reuse save_position logic but optimized within this session
            pm = session.query(PositionModel).filter(PositionModel.symbol == pos.symbol).first()
            
            if pm:
                # Update
                pm.side = pos.side.value
                pm.size = pos.size
                pm.size_notional = pos.size_notional
                pm.entry_price = pos.entry_price
                pm.current_mark_price = pos.current_mark_price
                pm.liquidation_price = pos.liquidation_price
                pm.unrealized_pnl = pos.unrealized_pnl
                pm.leverage = pos.leverage
                pm.margin_used = pos.margin_used
                pm.stop_loss_order_id = pos.stop_loss_order_id
                pm.take_profit_order_id = pos.take_profit_order_id
                # Management fields
                pm.initial_stop_price = pos.initial_stop_price
                pm.tp1_price = pos.tp1_price
                pm.tp2_price = pos.tp2_price
                pm.final_target_price = pos.final_target_price
                pm.updated_at = datetime.utcnow()
            else:
                # Create
                pm = PositionModel(
                    symbol=pos.symbol,
                    side=pos.side.value,
                    size=pos.size,
                    size_notional=pos.size_notional,
                    entry_price=pos.entry_price,
                    current_mark_price=pos.current_mark_price,
                    liquidation_price=pos.liquidation_price,
                    unrealized_pnl=pos.unrealized_pnl,
                    leverage=pos.leverage,
                    margin_used=pos.margin_used,
                    stop_loss_order_id=pos.stop_loss_order_id,
                    take_profit_order_id=pos.take_profit_order_id,
                    # Management fields
                    initial_stop_price=pos.initial_stop_price,
                    tp1_price=pos.tp1_price,
                    tp2_price=pos.tp2_price,
                    final_target_price=pos.final_target_price,
                    opened_at=pos.opened_at,
                )
                session.add(pm)


def get_active_position(symbol: str = "BTC/USD") -> Optional[Position]:
    """
    Get active position for symbol.
    
    Args:
        symbol: Symbol to check
        
    Returns:
        Position object if exists, else None
    """
    db = get_db()
    with db.get_session() as session:
        pm = session.query(PositionModel).filter(PositionModel.symbol == symbol).first()
        
        if not pm:
            return None
            
        return Position(
            symbol=pm.symbol,
            side=Side(pm.side),
            size=Decimal(str(pm.size)),
            size_notional=Decimal(str(pm.size_notional)),
            entry_price=Decimal(str(pm.entry_price)),
            current_mark_price=Decimal(str(pm.current_mark_price)),
            liquidation_price=Decimal(str(pm.liquidation_price)),
            unrealized_pnl=Decimal(str(pm.unrealized_pnl)),
            leverage=Decimal(str(pm.leverage)),
            margin_used=Decimal(str(pm.margin_used)),
            opened_at=pm.opened_at.replace(tzinfo=timezone.utc),
            updated_at=pm.updated_at.replace(tzinfo=timezone.utc),
            # Management Params
            initial_stop_price=Decimal(str(pm.initial_stop_price)) if pm.initial_stop_price else None,
            trade_type=pm.trade_type,
            tp1_price=Decimal(str(pm.tp1_price)) if pm.tp1_price else None,
            tp2_price=Decimal(str(pm.tp2_price)) if pm.tp2_price else None,
            final_target_price=Decimal(str(pm.final_target_price)) if pm.final_target_price else None,
            partial_close_pct=Decimal(str(pm.partial_close_pct)) if pm.partial_close_pct else Decimal("0.5"),
            original_size=Decimal(str(pm.original_size)) if pm.original_size else None,
            stop_loss_order_id=pm.stop_loss_order_id,
            tp_order_ids=json.loads(pm.tp_order_ids) if pm.tp_order_ids else None,
            basis_at_entry=Decimal(str(pm.basis_at_entry)) if pm.basis_at_entry else None,
            basis_current=Decimal(str(pm.basis_current)) if pm.basis_current else None,
            funding_rate=Decimal(str(pm.funding_rate)) if pm.funding_rate else None,
            cumulative_funding=Decimal(str(pm.cumulative_funding)) if pm.cumulative_funding else Decimal("0")
        )


def get_active_positions() -> List[Position]:
    """Retrieve all active positions."""
    db = get_db()
    with db.get_session() as session:
        position_models = session.query(PositionModel).all()
        
        return [
            Position(
                symbol=pm.symbol,
                side=Side(pm.side),
                size=Decimal(str(pm.size)),
                size_notional=Decimal(str(pm.size_notional)),
                entry_price=Decimal(str(pm.entry_price)),
                current_mark_price=Decimal(str(pm.current_mark_price)),
                liquidation_price=Decimal(str(pm.liquidation_price)),
                unrealized_pnl=Decimal(str(pm.unrealized_pnl)),
                leverage=Decimal(str(pm.leverage)),
                margin_used=Decimal(str(pm.margin_used)),
                opened_at=pm.opened_at.replace(tzinfo=timezone.utc),
                updated_at=pm.updated_at.replace(tzinfo=timezone.utc),
                # V3 Params
                initial_stop_price=Decimal(str(pm.initial_stop_price)) if pm.initial_stop_price else None,
                trade_type=pm.trade_type,
                tp1_price=Decimal(str(pm.tp1_price)) if pm.tp1_price else None,
                tp2_price=Decimal(str(pm.tp2_price)) if pm.tp2_price else None,
                final_target_price=Decimal(str(pm.final_target_price)) if pm.final_target_price else None,
                partial_close_pct=Decimal(str(pm.partial_close_pct)) if pm.partial_close_pct else Decimal("0.5"),
                original_size=Decimal(str(pm.original_size)) if pm.original_size else None,
                stop_loss_order_id=pm.stop_loss_order_id,
                tp_order_ids=json.loads(pm.tp_order_ids) if pm.tp_order_ids else None,
                basis_at_entry=Decimal(str(pm.basis_at_entry)) if pm.basis_at_entry else None,
                basis_current=Decimal(str(pm.basis_current)) if pm.basis_current else None,
                funding_rate=Decimal(str(pm.funding_rate)) if pm.funding_rate else None,
                cumulative_funding=Decimal(str(pm.cumulative_funding)) if pm.cumulative_funding else Decimal("0")
            )
            for pm in position_models
        ]


def get_all_trades() -> List[Trade]:
    """Retrieve all trades from the database."""
    db = get_db()
    with db.get_session() as session:
        trade_models = session.query(TradeModel).order_by(TradeModel.exited_at.desc()).all()
        
        return [
            Trade(
                trade_id=tm.trade_id,
                symbol=tm.symbol,
                side=Side(tm.side),
                entry_price=Decimal(str(tm.entry_price)),
                exit_price=Decimal(str(tm.exit_price)),
                size_notional=Decimal(str(tm.size_notional)),
                leverage=Decimal(str(tm.leverage)),
                gross_pnl=Decimal(str(tm.gross_pnl)),
                fees=Decimal(str(tm.fees)),
                funding=Decimal(str(tm.funding)),
                net_pnl=Decimal(str(tm.net_pnl)),
                entered_at=tm.entered_at.replace(tzinfo=timezone.utc),
                exited_at=tm.exited_at.replace(tzinfo=timezone.utc),
                holding_period_hours=Decimal(str(tm.holding_period_hours)),
                exit_reason=tm.exit_reason,
            )
            for tm in trade_models
        ]


def get_trades_since(since: datetime) -> List[Trade]:
    """
    Retrieve trades closed since a specific time.
    
    Args:
        since: Datetime to filter from (inclusive)
    
    Returns:
        List of Trade objects closed since the given time
    """
    db = get_db()
    with db.get_session() as session:
        trade_models = session.query(TradeModel).filter(
            TradeModel.exited_at >= since
        ).order_by(TradeModel.exited_at.desc()).all()
        
        return [
            Trade(
                trade_id=tm.trade_id,
                symbol=tm.symbol,
                side=Side(tm.side),
                entry_price=Decimal(str(tm.entry_price)),
                exit_price=Decimal(str(tm.exit_price)),
                size_notional=Decimal(str(tm.size_notional)),
                leverage=Decimal(str(tm.leverage)),
                gross_pnl=Decimal(str(tm.gross_pnl)),
                fees=Decimal(str(tm.fees)),
                funding=Decimal(str(tm.funding)),
                net_pnl=Decimal(str(tm.net_pnl)),
                entered_at=tm.entered_at.replace(tzinfo=timezone.utc),
                exited_at=tm.exited_at.replace(tzinfo=timezone.utc),
                holding_period_hours=Decimal(str(tm.holding_period_hours)),
                exit_reason=tm.exit_reason,
            )
            for tm in trade_models
        ]


def clear_cache():
    """Clear the query cache. Useful after bulk updates."""
    _query_cache.clear()


def record_event(
    event_type: str,
    symbol: str,
    details: Dict,
    decision_id: Optional[str] = None,
    timestamp: Optional[datetime] = None
) -> None:
    """
    Record a system event for the audit trail (synchronous version).
    
    Args:
        event_type: Type of event (e.g. SIGNAL, DECISION, RISK)
        symbol: Related symbol
        details: Dictionary of details (will be JSON serialized)
        decision_id: Optional ID to link related events
        timestamp: Optional explicit timestamp
    """
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)
        
    # Serialize details
    # Handle Decimals for JSON
    def decimal_default(obj):
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        if hasattr(obj, "value"): # Enum
            return obj.value
        raise TypeError
        
    try:
        details_json = json.dumps(details, default=decimal_default)
    except Exception as e:
        details_json = json.dumps({"error": str(e), "original_type": str(type(details))})
    
    db = get_db()
    with db.get_session() as session:
        event = SystemEventModel(
            timestamp=timestamp,
            event_type=event_type,
            symbol=symbol,
            decision_id=decision_id,
            details=details_json
        )
        session.add(event)


async def async_record_event(
    event_type: str,
    symbol: str,
    details: Dict,
    decision_id: Optional[str] = None,
    timestamp: Optional[datetime] = None
) -> None:
    """
    Record a system event for the audit trail (async version - non-blocking).
    
    Offloads the synchronous DB write to a thread pool to prevent blocking
    the main event loop during live trading.
    
    Args:
        event_type: Type of event (e.g. SIGNAL, DECISION, RISK)
        symbol: Related symbol
        details: Dictionary of details (will be JSON serialized)
        decision_id: Optional ID to link related events
        timestamp: Optional explicit timestamp
    """
    import asyncio
    
    # Run the synchronous record_event in a thread pool
    await asyncio.to_thread(
        record_event,
        event_type=event_type,
        symbol=symbol,
        details=details,
        decision_id=decision_id,
        timestamp=timestamp
    )


def get_recent_events(limit: int = 50, event_type: Optional[str] = None, symbol: Optional[str] = None) -> List[Dict]:
    """Get recent system events."""
    db = get_db()
    with db.get_session() as session:
        query = session.query(SystemEventModel)
        
        if event_type:
            query = query.filter(SystemEventModel.event_type == event_type)
        
        if symbol:
            query = query.filter(SystemEventModel.symbol == symbol)
            
        events = query.order_by(SystemEventModel.timestamp.desc()).limit(limit).all()
        
        return [
            {
                "id": e.id,
                "timestamp": e.timestamp.replace(tzinfo=timezone.utc).isoformat(),
                "type": e.event_type,
                "symbol": e.symbol,
                "decision_id": e.decision_id,
                "details": json.loads(e.details)
            }
            for e in events
        ]

def get_event_stats(symbol: str) -> Dict[str, Any]:
    """Get statistics for system events for a specific symbol."""
    db = get_db()
    with db.get_session() as session:
        from sqlalchemy import func
        
        # Count events for this symbol
        count = session.query(func.count(SystemEventModel.id)).filter(
            SystemEventModel.symbol == symbol
        ).scalar() or 0
        
        # Get timestamp of the most recent event
        last_ts = session.query(func.max(SystemEventModel.timestamp)).filter(
            SystemEventModel.symbol == symbol
        ).scalar()
        
        return {
            "count": count,
            "last_event": last_ts.replace(tzinfo=timezone.utc) if last_ts else None
        }

def get_decision_chain(decision_id: str) -> List[Dict]:
    """Get all events related to a decision ID."""
    db = get_db()
    with db.get_session() as session:
        events = session.query(SystemEventModel).filter(
            SystemEventModel.decision_id == decision_id
        ).order_by(SystemEventModel.timestamp.asc()).all()
        
        return [
            {
                "id": e.id,
                "timestamp": e.timestamp.replace(tzinfo=timezone.utc).isoformat(),
                "type": e.event_type,
                "details": json.loads(e.details)
            }
            for e in events
        ]


def save_account_state(
    equity: Decimal,
    balance: Decimal,
    margin_used: Decimal,
    available_margin: Decimal,
    unrealized_pnl: Decimal
) -> None:
    """Save account snapshot."""
    db = get_db()
    with db.get_session() as session:
        state = AccountStateModel(
            timestamp=datetime.now(timezone.utc),
            equity=equity,
            balance=balance,
            margin_used=margin_used,
            available_margin=available_margin,
            unrealized_pnl=unrealized_pnl
        )
        session.add(state)


def get_latest_account_state() -> Optional[Dict[str, Decimal]]:
    """Get latest account snapshot."""
    db = get_db()
    with db.get_session() as session:
        state = session.query(AccountStateModel).order_by(
            AccountStateModel.timestamp.desc()
        ).first()
        
        if not state:
            return None
            
        return {
            "timestamp": state.timestamp.replace(tzinfo=timezone.utc),
            "equity": Decimal(str(state.equity)),
            "balance": Decimal(str(state.balance)),
            "margin_used": Decimal(str(state.margin_used)),
            "available_margin": Decimal(str(state.available_margin)),
            "unrealized_pnl": Decimal(str(state.unrealized_pnl))
        }


def get_latest_traces(limit: int = 300) -> List[Dict]:
    """
    Get the latest DECISION_TRACE event for each symbol.
    
    Args:
        limit: Maximum number of symbols to return
        
    Returns:
        List of dicts with symbol, timestamp, and details
    """
    db = get_db()
    with db.get_session() as session:
        from sqlalchemy import func
        
        # Subquery to get latest timestamp per symbol
        subq = session.query(
            SystemEventModel.symbol,
            func.max(SystemEventModel.timestamp).label('max_ts')
        ).filter(
            SystemEventModel.event_type == 'DECISION_TRACE'
        ).group_by(SystemEventModel.symbol).subquery()
        
        # Join to get full records
        events = session.query(SystemEventModel).join(
            subq,
            (SystemEventModel.symbol == subq.c.symbol) &
            (SystemEventModel.timestamp == subq.c.max_ts)
        ).limit(limit).all()
        
        results = []
        for e in events:
            results.append({
                'symbol': e.symbol,
                'timestamp': e.timestamp.replace(tzinfo=timezone.utc),
                'details': json.loads(e.details)
            })
        
        return results

