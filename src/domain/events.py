"""
Event schemas for dashboard observability.

Defines data contracts for coin state snapshots, signal decisions,
risk validations, and execution events.
"""
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional, List, Dict, Tuple
from enum import Enum


class ActionType(Enum):
    """Next action type."""
    ENTER = "ENTER"
    WAIT = "WAIT"
    EXIT = "EXIT"
    MANAGE = "MANAGE"
    BLOCKED = "BLOCKED"


@dataclass
class CoinStateSnapshot:
    """
    Complete state snapshot for a single coin.
    
    Used for Coin Matrix display and real-time monitoring.
    """
    # Identity
    symbol_spot: str
    symbol_perp: str
    timestamp: datetime
    
    # Spot (Signal Generation)
    spot_price: Decimal
    spot_ohlcv_ts: datetime
    bias_htf: str  # "bullish", "bearish", "neutral"
    regime: str  # "trending", "ranging", "transition"
    adx: Decimal
    atr: Decimal
    ema200_slope: str  # "up", "down", "flat"
    
    # SMC Structure
    ob_level: Optional[Decimal] = None
    ob_band: Optional[Tuple[Decimal, Decimal]] = None
    fvg_band: Optional[Tuple[Decimal, Decimal]] = None
    bos_state: Optional[str] = None
    
   # Futures (Execution)
    perp_mark: Decimal = Decimal("0")
    best_bid: Decimal = Decimal("0")
    best_ask: Decimal = Decimal("0")
    spread_pct: Decimal = Decimal("0")
    basis_pct: Decimal = Decimal("0")
    funding_rate: Decimal = Decimal("0")
    funding_against_flag: bool = False
    
    # Position (if exists)
    pos_side: Optional[str] = None
    pos_notional: Optional[Decimal] = None
    entry_price: Optional[Decimal] = None
    liq_price_exchange: Optional[Decimal] = None
    liq_distance_pct: Optional[Decimal] = None
    stop_price: Optional[Decimal] = None
    stop_distance_pct: Optional[Decimal] = None
    tp_orders: List[Dict] = field(default_factory=list)
    
    # Decision
    signal: str = "HOLD"  # "LONG", "SHORT", "HOLD"
    setup_quality: float = 0.0  # 0-100
    next_action: str = ActionType.WAIT.value
    score_breakdown: Dict[str, float] = field(default_factory=dict)
    block_reason_codes: List[str] = field(default_factory=list)
    
    # Risk flags
    risk_flags: List[str] = field(default_factory=list)  # e.g., ["NEAR_LIQ", "BASIS_HIGH"]
    
    # Event Stats
    event_count: int = 0
    last_event_ts: Optional[datetime] = None


@dataclass
class SignalDecisionEvent:
    """
    Signal generation event with full reasoning.
    
    Used for Coin Detail reasoning transparency.
    """
    timestamp: datetime
    symbol: str
    signal_type: str  # "LONG", "SHORT", "NO_SIGNAL"
    setup_quality: float  # 0-100
    
    # Full reasoning breakdown
    reasoning: Dict = field(default_factory=dict)
    
    # Rule checklist (for transparency)
    rule_checklist: Dict[str, Dict] = field(default_factory=dict)
    # Example structure:
    # {
    #     "HTF_BIAS": {"value": "bullish", "threshold": "bullish", "passed": True, "explanation": "..."},
    #     "ADX_THRESHOLD": {"value": 28.5, "threshold": 25.0, "passed": True, "explanation": "..."},
    # }


@dataclass
class RiskDecisionEvent:
    """
    Risk validation event.
    
    Captures why a trade was approved or rejected.
    """
    timestamp: datetime
    symbol: str
    approved: bool
    rejection_reasons: List[str] = field(default_factory=list)
    
    # Risk metrics
    risk_metrics: Dict = field(default_factory=dict)
    # {
    #     "position_notional": 5000.0,
    #     "stop_distance_pct": 0.015,
    #     "effective_leverage": 2.5,
    #     "liq_buffer_pct": 0.45,
    #     "basis_pct": 0.003,
    #     ...
    # }


@dataclass
class ExecutionEvent:
    """
    Order execution event.
    
    Tracks order lifecycle: submit → fill → cancel.
    """
    timestamp: datetime
    symbol: str
    event_type: str  # "SUBMIT", "FILL", "CANCEL", "REJECT"
    order_type: str  # "ENTRY", "STOP", "TP1", "TP2", "TP3"
    
    price: Optional[Decimal] = None
    size: Optional[Decimal] = None
    order_id: Optional[str] = None
    
    # For rejections
    reject_reason: Optional[str] = None


@dataclass
class ReconciliationEvent:
    """
    Reconciliation correction event.
    
    Tracks when system state was corrected.
    """
    timestamp: datetime
    symbol: str
    correction_type: str  # "GHOST_ORDER", "STATE_REWRITE", "MARGIN_MISMATCH"
    details: Dict = field(default_factory=dict)


@dataclass
class AlertEvent:
    """
    System alert event.
    
    For dashboard notifications and push alerts.
    """
    timestamp: datetime
    severity: str  # "INFO", "WARNING", "CRITICAL"
    category: str  # "SIGNAL", "RISK", "EXECUTION", "SYSTEM"
    symbol: Optional[str] = None
    message: str = ""
    details: Dict = field(default_factory=dict)


# Reason code definitions
REASON_CODES = {
    # Basis & Spread
    "BASIS_MAX_EXCEEDED": "Spot-futures basis exceeds maximum threshold",
    "SPREAD_MAX_EXCEEDED": "Bid-ask spread too wide for safe execution",
    "FUNDING_SPIKE_BLOCK": "Funding rate spike detected (against position)",
    
    # Risk & Costs
    "RR_DISTORTION_EXCEEDED": "Fees/funding distort risk-reward ratio too much",
    "LIQ_BUFFER_TOO_SMALL": "Liquidation buffer below minimum safety threshold",
    "EFFECTIVE_LEVERAGE_EXCEEDED": "Effective leverage exceeds maximum allowed",
    "MARGIN_INSUFFICIENT": "Insufficient margin for position",
    
    # Strategy Filters
    "ADX_TOO_LOW": "ADX below minimum trend threshold",
    "ATR_TOO_HIGH": "ATR too high - excessive volatility",
    "ATR_TOO_LOW": "ATR too low - insufficient movement",
    "NO_STRUCTURE": "No valid OB or FVG structure detected",
    "BOS_NOT_CONFIRMED": "Break of Structure not confirmed",
    
    # Portfolio Limits
    "MAX_POSITIONS_REACHED": "Maximum concurrent positions limit reached",
    "DAILY_LOSS_LIMIT": "Daily loss limit exceeded",
    "POSITION_EXISTS": "Position already exists (no pyramiding)",
    
    # Data Health
    "DATA_STALE_SPOT": "Spot data feed stale or delayed",
    "DATA_STALE_FUTURES": "Futures data feed stale or delayed",
    "RECON_UNCERTAIN": "Reconciliation uncertainty - state mismatch",
    
    # System Safety
    "KILL_SWITCH_LATCHED": "Kill switch has been triggered",
    "ENTRIES_BLOCKED": "New entries blocked globally",
    "ASSET_UNHEALTHY": "Asset marked unhealthy (feed/basis issues)",
}
