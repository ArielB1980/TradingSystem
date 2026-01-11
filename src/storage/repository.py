"""
Persistence functions for storing and retrieving trading data.

Provides repository pattern for clean data access.
"""
from sqlalchemy import Column, String, Numeric, DateTime, Integer, Boolean
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional
from src.storage.db import Base, get_db
from src.domain.models import Candle, Trade, Position


# ORM Models
class CandleModel(Base):
    """ORM model for OHLCV candles."""
    __tablename__ = "candles"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, nullable=False, index=True)
    symbol = Column(String, nullable=False, index=True)
    timeframe = Column(String, nullable=False)
    open = Column(Numeric(precision=20, scale=8), nullable=False)
    high = Column(Numeric(precision=20, scale=8), nullable=False)
    low = Column(Numeric(precision=20, scale=8), nullable=False)
    close = Column(Numeric(precision=20, scale=8), nullable=False)
    volume = Column(Numeric(precision=20, scale=8), nullable=False)


class TradeModel(Base):
    """ORM model for completed trades."""
    __tablename__ = "trades"
    
    trade_id = Column(String, primary_key=True)
    symbol = Column(String, nullable=False, index=True)
    side = Column(String, nullable=False)
    entry_price = Column(Numeric(precision=20, scale=8), nullable=False)
    exit_price = Column(Numeric(precision=20, scale=8), nullable=False)
    size_notional = Column(Numeric(precision=20, scale=2), nullable=False)
    leverage = Column(Numeric(precision=10, scale=2), nullable=False)
    
    gross_pnl = Column(Numeric(precision=20, scale=2), nullable=False)
    fees = Column(Numeric(precision=20, scale=2), nullable=False)
    funding = Column(Numeric(precision=20, scale=2), nullable=False)
    net_pnl = Column(Numeric(precision=20, scale=2), nullable=False)
    
    entered_at = Column(DateTime, nullable=False, index=True)
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
    
    opened_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class SystemEventModel(Base):
    """ORM model for system events (audit trail)."""
    __tablename__ = "system_events"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, nullable=False, index=True)
    event_type = Column(String, nullable=False, index=True)
    symbol = Column(String, nullable=False, index=True)
    decision_id = Column(String, nullable=True, index=True)
    details = Column(String, nullable=False) # JSON string


class AccountStateModel(Base):
    """ORM model for account balance tracking."""
    __tablename__ = "account_state"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, nullable=False, index=True)
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


def save_candles_bulk(candles: List[Candle]) -> int:
    """
    Save multiple candles to the database efficiently.
    Skips duplicates based on timestamp/symbol/timeframe.
    
    Args:
        candles: List of Candle objects
        
    Returns:
        Number of candles inserted
    """
    if not candles:
        return 0
        
    db = get_db()
    with db.get_session() as session:
        # 1. Identify scope
        symbol = candles[0].symbol
        timeframe = candles[0].timeframe
        min_ts = min(c.timestamp for c in candles)
        max_ts = max(c.timestamp for c in candles)
        
        # 2. Get existing timestamps in this range to avoid duplicates
        # optimizing to avoid individual checks
        existing_query = session.query(CandleModel.timestamp).filter(
            CandleModel.symbol == symbol,
            CandleModel.timeframe == timeframe,
            CandleModel.timestamp >= min_ts,
            CandleModel.timestamp <= max_ts
        ).all()
        
        existing_timestamps = {r[0] for r in existing_query}
        
        # 3. Filter out existing
        new_candles = []
        for c in candles:
            if c.timestamp not in existing_timestamps:
                new_candles.append(CandleModel(
                    timestamp=c.timestamp,
                    symbol=c.symbol,
                    timeframe=c.timeframe,
                    open=c.open,
                    high=c.high,
                    low=c.low,
                    close=c.close,
                    volume=c.volume,
                ))
        
        # 4. Bulk insert
        if new_candles:
            session.bulk_save_objects(new_candles)
            
        return len(new_candles)



def get_candles(
    symbol: str,
    timeframe: str,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    limit: Optional[int] = None,
) -> List[Candle]:
    """
    Retrieve candles from the database.
    
    Args:
        symbol: Symbol (spot)
        timeframe: Timeframe string
        start_time: Optional start time (inclusive)
        end_time: Optional end time (inclusive)
        limit: Optional limit on number of candles
    
    Returns:
        List of Candle objects
    """
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
                opened_at=position.opened_at,
            )
            session.add(position_model)


def delete_position(symbol: str) -> None:
    """Delete a position (when closed)."""
    db = get_db()
    with db.get_session() as session:
        session.query(PositionModel).filter(PositionModel.symbol == symbol).delete()


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
            stop_loss_order_id=pm.stop_loss_order_id,
            take_profit_order_id=pm.take_profit_order_id,
            opened_at=pm.opened_at.replace(tzinfo=timezone.utc)
        )


def get_all_trades() -> List[Trade]:
    """Retrieve all trades from the database."""
    db = get_db()
    with db.get_session() as session:
        trade_models = session.query(TradeModel).order_by(TradeModel.entered_at.desc()).all()
        
        return [
            Trade(
                trade_id=tm.trade_id,
                symbol=tm.symbol,
                side=tm.side,
                entry_price=Decimal(str(tm.entry_price)),
                exit_price=Decimal(str(tm.exit_price)),
                size_notional=Decimal(str(tm.size_notional)),
                leverage=Decimal(str(tm.leverage)),
                gross_pnl=Decimal(str(tm.gross_pnl)),
                fees=Decimal(str(tm.fees)),
                funding=Decimal(str(tm.funding)),
                net_pnl=Decimal(str(tm.net_pnl)),
                entered_at=tm.entered_at,
                exited_at=tm.exited_at,
                holding_period_hours=Decimal(str(tm.holding_period_hours)),
                exit_reason=tm.exit_reason,
            )
            for tm in trade_models
        ]


import json
from typing import Optional, Dict

def record_event(
    event_type: str,
    symbol: str,
    details: Dict,
    decision_id: Optional[str] = None,
    timestamp: Optional[datetime] = None
) -> None:
    """
    Record a system event for the audit trail.
    
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
