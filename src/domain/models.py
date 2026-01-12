"""
Domain models for the trading system.

These are the core business objects used throughout the application.
All timestamps use UTC timezone-aware datetimes.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional, List


class Side(str, Enum):
    """Trade side."""
    LONG = "long"
    SHORT = "short"


class OrderType(str, Enum):
    """Order type."""
    MARKET = "market"
    LIMIT = "limit"
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"


class OrderStatus(str, Enum):
    """Order status."""
    PENDING = "pending"
    SUBMITTED = "submitted"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class SignalType(str, Enum):
    """Signal type."""
    LONG = "long"
    SHORT = "short"
    EXIT_LONG = "exit_long"
    EXIT_SHORT = "exit_short"
    NO_SIGNAL = "no_signal"


class SetupType(str, Enum):
    """
    Setup type for regime classification.
    
    Used to distinguish between tight-stop SMC and wide-stop structure trades.
    """
    OB = "ob"           # Order Block (tight-stop regime: 0.4-1.0%)
    FVG = "fvg"         # Fair Value Gap (tight-stop regime: 0.4-1.0%)
    BOS = "bos"         # Break of Structure (wide-stop regime: 1.5-3.0%)
    TREND = "trend"     # HTF Trend following (wide-stop regime: 1.5-3.0%)


@dataclass(frozen=True)
class Candle:
    """
    OHLCV candle from spot market (used for strategy analysis).
    """
    timestamp: datetime
    symbol: str  # e.g., "BTC/USD" (spot)
    timeframe: str  # e.g., "15m", "1h", "4h", "1d"
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    
    def __post_init__(self):
        """Validate candle data."""
        if self.timestamp.tzinfo is None:
            raise ValueError("Candle timestamp must be timezone-aware (UTC)")
        if self.high < self.low:
            raise ValueError(f"Invalid candle: high ({self.high}) < low ({self.low})")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError(f"Invalid candle: OHLC values inconsistent")


@dataclass
class Signal:
    """
    Trading signal generated from spot market analysis (SMC).
    """
    timestamp: datetime
    symbol: str  # Spot symbol (e.g., "BTC/USD")
    signal_type: SignalType
    entry_price: Decimal  # From spot analysis
    stop_loss: Decimal  # From spot analysis (SMC invalidation level)
    take_profit: Optional[Decimal]  # From spot analysis (next SMC level)
    reasoning: str  # Full reasoning for signal
    
    # Regime classification (NEW)
    setup_type: SetupType  # OB/FVG/BOS/TREND
    regime: str  # "tight_smc" or "wide_structure"
    
    # Metadata
    higher_tf_bias: str  # e.g., "bullish", "bearish", "neutral"
    adx: Decimal
    atr: Decimal
    ema200_slope: str  # e.g., "up", "down", "flat"
    tp_candidates: list[Decimal] = field(default_factory=list)  # Structure-based TP levels
    score_breakdown: dict = field(default_factory=dict)  # Detailed score components (SMC, Fib, Cost, etc.)
    
    def __post_init__(self):
        """Validate signal."""
        if self.timestamp.tzinfo is None:
            raise ValueError("Signal timestamp must be timezone-aware (UTC)")


@dataclass
class OrderIntent:
    """
    Intent to place an order (before conversion to futures pricing).
    """
    timestamp: datetime
    signal: Signal
    side: Side
    size_notional: Decimal  # Position size in USD notional
    leverage: Decimal  # Actual leverage (≤10×)
    
    # Spot-derived levels (to be converted to futures prices)
    entry_price_spot: Decimal
    stop_loss_spot: Decimal
    take_profit_spot: Optional[Decimal]

    # Futures execution levels (converted)
    entry_price_futures: Optional[Decimal] = None
    stop_loss_futures: Optional[Decimal] = None
    take_profit_futures: Optional[Decimal] = None


@dataclass
class Order:
    """
    Actual order on futures exchange.
    """
    order_id: str
    client_order_id: str
    timestamp: datetime
    symbol: str  # Futures symbol (e.g., "BTCUSD-PERP")
    side: Side
    order_type: OrderType
    size: Decimal  # Size in contracts/units
    price: Optional[Decimal]  # Limit price (None for market orders)
    status: OrderStatus
    filled_size: Decimal = Decimal("0")
    filled_price: Optional[Decimal] = None
    filled_at: Optional[datetime] = None
    
    # Parent order (for SL/TP)
    parent_order_id: Optional[str] = None
    reduce_only: bool = False


@dataclass
class Position:
    """
    Open position on futures exchange.
    """
    symbol: str  # Futures symbol (e.g., "BTCUSD-PERP")
    side: Side
    size: Decimal  # Position size in contracts
    size_notional: Decimal  # Position size in USD notional
    entry_price: Decimal  # Average entry price (futures mark price)
    current_mark_price: Decimal  # Current mark price
    liquidation_price: Decimal  # Exchange-reported liquidation price
    unrealized_pnl: Decimal
    leverage: Decimal  # Effective leverage
    margin_used: Decimal
    
    # Associated orders
    stop_loss_order_id: Optional[str] = None
    take_profit_order_id: Optional[str] = None
    tp_order_ids: list[str] = field(default_factory=list)  # Multi-TP ladder
    
    # Execution State
    trailing_active: bool = False
    break_even_active: bool = False
    peak_price: Optional[Decimal] = None  # Highest/Lowest mark price since trail activation
    
    # V3 Active Trade Management
    # V3 Immutable Parameters (set at entry, never changed)
    initial_stop_price: Optional[Decimal] = None  # Original stop loss level
    trade_type: Optional[str] = None  # "breakout", "pullback", "reversal"
    tp1_price: Optional[Decimal] = None  # First TP target
    tp2_price: Optional[Decimal] = None  # Second TP target
    final_target_price: Optional[Decimal] = None  # Final TP target
    partial_close_pct: Decimal = Decimal("0.5")  # % to close at TP1
    original_size: Optional[Decimal] = None  # Original position size before any closes
    
    # Order IDs for tracking
    stop_loss_order_id: Optional[str] = None
    tp_order_ids: Optional[list[str]] = None
    
    # Basis and Funding Tracking
    basis_at_entry: Optional[Decimal] = None  # Futures - Spot at entry (bps)
    basis_current: Optional[Decimal] = None  # Current basis (bps)
    funding_rate: Optional[Decimal] = None  # Current funding rate
    cumulative_funding: Decimal = Decimal("0")  # Total funding paid/received
    
    # State Flags
    intent_confirmed: bool = False
    premise_invalidated: bool = False
    tp1_hit: bool = False
    tp2_hit: bool = False
    
    # Metadata
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    # V2.1 Metadata
    setup_type: Optional[str] = None # e.g. "ob", "fvg"
    regime: Optional[str] = None # "tight_smc" or "wide_structure"
    
    def liquidation_distance_pct(self) -> Decimal:
        """
        Calculate liquidation distance as percentage.
        
        Returns direction-aware distance:
        - Long: (mark_price - liq_price) / mark_price
        - Short: (liq_price - mark_price) / mark_price
        """
        if self.side == Side.LONG:
            return (self.current_mark_price - self.liquidation_price) / self.current_mark_price
        else:  # SHORT
            return (self.liquidation_price - self.current_mark_price) / self.current_mark_price


@dataclass
class RiskDecision:
    """
    Risk management decision for a proposed trade.
    """
    approved: bool
    position_notional: Decimal
    leverage: Decimal
    margin_required: Decimal
    liquidation_buffer_pct: Decimal
    basis_divergence_pct: Decimal
    estimated_fees_funding: Decimal
    
    # Opportunity Cost Replacement
    should_close_existing: bool = False
    close_symbol: Optional[str] = None
    
    # Rejection reasons
    rejection_reasons: list[str] = field(default_factory=list)
    
    # Metadata
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class Trade:
    """
    Completed trade (entry → exit).
    """
    trade_id: str
    symbol: str  # Futures symbol
    side: Side
    entry_price: Decimal
    exit_price: Decimal
    size_notional: Decimal
    leverage: Decimal
    
    # P&L
    gross_pnl: Decimal
    fees: Decimal
    funding: Decimal
    net_pnl: Decimal
    
    # Timing
    entered_at: datetime
    exited_at: datetime
    holding_period_hours: Decimal
    
    # Exit reason
    exit_reason: str  # "stop_loss", "take_profit", "manual", "kill_switch"
    
    # V2.1 Metadata
    setup_type: Optional[str] = None
    regime: Optional[str] = None
