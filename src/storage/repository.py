"""
Persistence functions for storing and retrieving trading data.

Provides repository pattern for clean data access.
"""
from sqlalchemy import Column, String, Numeric, DateTime, Integer, Boolean
from datetime import datetime
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
                timestamp=cm.timestamp,
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
