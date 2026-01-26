"""
ShockGuard: Protection against wicks and flash moves.

Detects extreme volatility and pauses entries while reducing exposure
on positions with threatened liquidation buffers.
"""
from dataclasses import dataclass, field
from typing import Dict, Optional, Set, List, Tuple
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from enum import Enum

from src.monitoring.logger import get_logger
from src.domain.models import Position, Side

logger = get_logger(__name__)


class ExposureAction(str, Enum):
    """Action to take for a position during shock."""
    HOLD = "HOLD"
    TRIM = "TRIM"  # Reduce by 50%
    CLOSE = "CLOSE"  # Close entirely


@dataclass
class ShockEvent:
    """Record of a shock detection event."""
    timestamp: datetime
    triggered_symbols: Set[str]
    reasons: List[str]
    shock_until: datetime


@dataclass
class MarkPriceSnapshot:
    """Snapshot of mark price at a timestamp."""
    mark_price: Decimal
    timestamp: datetime


@dataclass
class ExposureActionItem:
    """Action to take for a specific position."""
    symbol: str
    action: ExposureAction
    buffer_pct: Decimal
    reason: str


class ShockGuard:
    """
    Detects and responds to market shocks (wicks/flash moves).
    
    Detection triggers:
    1. 1-minute price move > threshold
    2. 1-minute range spike > threshold (if candles available)
    3. Basis spike > threshold
    4. Market-wide: multiple symbols trigger within window
    """
    
    def __init__(
        self,
        shock_move_pct: float = 0.025,
        shock_range_pct: float = 0.04,
        basis_shock_pct: float = 0.015,
        shock_cooldown_minutes: int = 30,
        emergency_buffer_pct: float = 0.10,
        trim_buffer_pct: float = 0.18,
        shock_marketwide_count: int = 3,
        shock_marketwide_window_sec: int = 60,
    ):
        """
        Initialize ShockGuard.
        
        Args:
            shock_move_pct: 1-minute move threshold (default 2.5%)
            shock_range_pct: 1-minute range threshold (default 4.0%)
            basis_shock_pct: Basis divergence threshold (default 1.5%)
            shock_cooldown_minutes: Minutes to pause entries after shock (default 30)
            emergency_buffer_pct: Liquidation buffer threshold for CLOSE (default 10%)
            trim_buffer_pct: Liquidation buffer threshold for TRIM (default 18%)
            shock_marketwide_count: Symbols needed for market-wide shock (default 3)
            shock_marketwide_window_sec: Window for market-wide detection (default 60s)
        """
        self.shock_move_pct = Decimal(str(shock_move_pct))
        self.shock_range_pct = Decimal(str(shock_range_pct))
        self.shock_basis_pct = Decimal(str(basis_shock_pct))
        self.shock_cooldown_minutes = shock_cooldown_minutes
        self.emergency_buffer_pct = Decimal(str(emergency_buffer_pct))
        self.trim_buffer_pct = Decimal(str(trim_buffer_pct))
        self.shock_marketwide_count = shock_marketwide_count
        self.shock_marketwide_window_sec = shock_marketwide_window_sec
        
        # State
        self.shock_mode_active = False
        self.shock_until: Optional[datetime] = None
        self.last_shock_event: Optional[ShockEvent] = None
        
        # Price history (1-minute rolling window per symbol)
        # symbol -> list of MarkPriceSnapshot (keep last 2-3 for 1m move detection)
        self.mark_price_history: Dict[str, List[MarkPriceSnapshot]] = {}
        
        # Market-wide shock tracking
        # List of (symbol, timestamp) for recent triggers
        self.recent_triggers: List[Tuple[str, datetime]] = []
        
        logger.info(
            "ShockGuard initialized",
            shock_move_pct=float(shock_move_pct),
            shock_range_pct=float(shock_range_pct),
            basis_shock_pct=float(basis_shock_pct),
            cooldown_minutes=shock_cooldown_minutes,
        )
    
    def update_mark_prices(self, mark_prices: Dict[str, Decimal]):
        """
        Update mark price history for shock detection.
        
        Args:
            mark_prices: Dict of symbol -> mark_price
        """
        now = datetime.now(timezone.utc)
        one_minute_ago = now - timedelta(minutes=1)
        
        for symbol, mark_price in mark_prices.items():
            if symbol not in self.mark_price_history:
                self.mark_price_history[symbol] = []
            
            # Add new snapshot
            self.mark_price_history[symbol].append(
                MarkPriceSnapshot(mark_price=mark_price, timestamp=now)
            )
            
            # Keep only snapshots within 1-minute window
            self.mark_price_history[symbol] = [
                s for s in self.mark_price_history[symbol]
                if s.timestamp > one_minute_ago
            ]
            # Sort by timestamp (oldest first) and limit to last 3
            self.mark_price_history[symbol].sort(key=lambda x: x.timestamp)
            if len(self.mark_price_history[symbol]) > 3:
                self.mark_price_history[symbol] = self.mark_price_history[symbol][-3:]
    
    def evaluate(
        self,
        mark_prices: Dict[str, Decimal],
        spot_prices: Optional[Dict[str, Decimal]] = None,
    ) -> bool:
        """
        Evaluate shock conditions and update state.
        
        Args:
            mark_prices: Current mark prices per symbol
            spot_prices: Optional spot prices for basis detection
        
        Returns:
            True if shock detected this call, False otherwise
        """
        now = datetime.now(timezone.utc)
        triggered_symbols: Set[str] = set()
        reasons: List[str] = []
        
        # Update price history (adds current prices)
        self.update_mark_prices(mark_prices)
        
        # Check each symbol for shocks
        for symbol, current_mark in mark_prices.items():
            history = self.mark_price_history.get(symbol, [])
            if len(history) < 2:
                continue  # Need at least 2 snapshots for move detection
            
            # Detection 1: 1-minute move
            # Find the oldest snapshot within 1-minute window (or closest to 60s ago)
            # Require dt >= 45s to avoid false triggers on micro-moves
            one_min_ago = now - timedelta(seconds=60)
            min_age_seconds = 45  # Minimum age to consider for 1-minute move
            
            prev_snapshot = None
            for snapshot in history:
                age_seconds = (now - snapshot.timestamp).total_seconds()
                if age_seconds >= min_age_seconds:
                    # Prefer snapshot closest to 60s ago, but accept any >= 45s
                    if prev_snapshot is None or abs(age_seconds - 60) < abs((now - prev_snapshot.timestamp).total_seconds() - 60):
                        prev_snapshot = snapshot
            
            if prev_snapshot and prev_snapshot.mark_price > 0:
                prev_mark = prev_snapshot.mark_price
                move_pct = abs(current_mark / prev_mark - Decimal("1"))
                age_seconds = (now - prev_snapshot.timestamp).total_seconds()
                if move_pct > self.shock_move_pct:
                    triggered_symbols.add(symbol)
                    reasons.append(
                        f"{symbol}: 1m move {move_pct:.2%} > {self.shock_move_pct:.2%} (age: {age_seconds:.1f}s)"
                    )
            
            # Detection 2: Range spike (1-minute high-low range)
            # Note: Range detection requires 1m candles or synthetic range from tick history
            # For now, we skip range detection as it requires additional data not available here
            # TODO: Implement range detection if 1m candles are available in the main loop
            # if self.shock_range_pct > 0:
            #     # Would need: high, low from 1m candle or tick history
            #     # range_pct = (high - low) / mid
            #     # if range_pct > self.shock_range_pct: trigger
            
            # Detection 3: Basis spike (if spot available)
            if spot_prices and symbol in spot_prices:
                spot_price = spot_prices[symbol]
                if spot_price > 0:
                    basis_pct = abs(current_mark / spot_price - Decimal("1"))
                    if basis_pct > self.shock_basis_pct:
                        triggered_symbols.add(symbol)
                        reasons.append(
                            f"{symbol}: basis {basis_pct:.2%} > {self.shock_basis_pct:.2%}"
                        )
        
        # Detection 4: Market-wide shock
        # Dedupe symbols by BASE to avoid counting aliases (PI_*, PF_*, BASE/USD:USD, BASE/USD)
        def extract_base(symbol: str) -> Optional[str]:
            """Extract base currency from symbol (e.g., 'PI_THETAUSD' -> 'THETA', 'THETA/USD:USD' -> 'THETA')."""
            # Remove common prefixes
            for prefix in ["PI_", "PF_", "FI_"]:
                if symbol.startswith(prefix):
                    symbol = symbol[len(prefix):]
            # Remove common suffixes
            for suffix in ["USD", "/USD:USD", "/USD"]:
                if symbol.endswith(suffix):
                    symbol = symbol[:-len(suffix)]
            return symbol if symbol else None
        
        # Dedupe triggered symbols by BASE
        triggered_bases = set()
        for symbol in triggered_symbols:
            base = extract_base(symbol)
            if base:
                triggered_bases.add(base)
        
        now_ts = now
        window_start = now_ts - timedelta(seconds=self.shock_marketwide_window_sec)
        
        # Add current triggers (using BASE for deduplication)
        for base in triggered_bases:
            self.recent_triggers.append((base, now_ts))
        
        # Clean old triggers
        self.recent_triggers = [
            (s, t) for s, t in self.recent_triggers
            if t > window_start
        ]
        
        # Count unique bases in window (already deduped)
        unique_bases_in_window = {s for s, _ in self.recent_triggers}
        if len(unique_bases_in_window) >= self.shock_marketwide_count:
            # Add all symbols that map to the triggered bases
            for symbol in mark_prices.keys():
                base = extract_base(symbol)
                if base in unique_bases_in_window:
                    triggered_symbols.add(symbol)
            reasons.append(
                f"Market-wide: {len(unique_bases_in_window)} unique assets triggered within {self.shock_marketwide_window_sec}s"
            )
        
        # Activate shock mode if any triggers
        if triggered_symbols:
            self.shock_mode_active = True
            self.shock_until = now + timedelta(minutes=self.shock_cooldown_minutes)
            self.last_shock_event = ShockEvent(
                timestamp=now,
                triggered_symbols=triggered_symbols,
                reasons=reasons,
                shock_until=self.shock_until,
            )
            
            logger.critical(
                "SHOCK_MODE ACTIVATED",
                triggered_symbols=list(triggered_symbols),
                reasons=reasons,
                shock_until=self.shock_until.isoformat(),
                cooldown_minutes=self.shock_cooldown_minutes,
            )
            return True
        
        return False
    
    def should_pause_entries(self, now: Optional[datetime] = None) -> bool:
        """
        Check if new entries should be paused.
        
        Args:
            now: Optional current time (defaults to now)
        
        Returns:
            True if entries should be paused
        """
        if not self.shock_mode_active:
            return False
        
        if now is None:
            now = datetime.now(timezone.utc)
        
        # Guard against None shock_until
        if not self.shock_until:
            # Clear shock mode if somehow active but no cooldown set
            self.shock_mode_active = False
            logger.warning("ShockGuard: shock_mode_active but shock_until is None, clearing state")
            return False
        
        if now < self.shock_until:
            return True
        
        # Cooldown expired, clear shock mode
        if now >= self.shock_until:
            self.shock_mode_active = False
            self.shock_until = None
            logger.info("ShockGuard cooldown expired, resuming normal trading")
        
        return False
    
    def evaluate_position_exposure(
        self,
        position: Position,
        mark_price: Decimal,
        liquidation_price: Optional[Decimal] = None,
    ) -> ExposureAction:
        """
        Evaluate what action to take for a position during shock.
        
        Args:
            position: Position to evaluate
            mark_price: Current mark price
            liquidation_price: Exchange-reported liquidation price (if available)
        
        Returns:
            ExposureAction: CLOSE, TRIM, or HOLD
        """
        if not self.shock_mode_active:
            return ExposureAction.HOLD
        
        # Use exchange liquidation price if available, otherwise skip
        if liquidation_price is None or liquidation_price == 0:
            return ExposureAction.HOLD
        
        # Calculate liquidation buffer
        if position.side == Side.LONG:
            buffer_pct = (mark_price - liquidation_price) / mark_price
        else:  # SHORT
            buffer_pct = (liquidation_price - mark_price) / mark_price
        
        # Determine action
        if buffer_pct < self.emergency_buffer_pct:
            return ExposureAction.CLOSE
        elif buffer_pct < self.trim_buffer_pct:
            return ExposureAction.TRIM
        else:
            return ExposureAction.HOLD
    
    def get_exposure_reduction_actions(
        self,
        positions: List[Position],
        mark_prices: Dict[str, Decimal],
        liquidation_prices: Optional[Dict[str, Decimal]] = None,
    ) -> List[ExposureActionItem]:
        """
        Get list of actions to take for positions during shock.
        
        Args:
            positions: List of open positions
            mark_prices: Current mark prices per symbol
            liquidation_prices: Optional liquidation prices per symbol
        
        Returns:
            List of ExposureActionItem
        """
        if not self.shock_mode_active:
            return []
        
        actions = []
        liquidation_prices = liquidation_prices or {}
        
        for position in positions:
            symbol = position.symbol
            mark_price = mark_prices.get(symbol)
            if not mark_price:
                continue
            
            liquidation_price = liquidation_prices.get(symbol) or position.liquidation_price
            
            action = self.evaluate_position_exposure(position, mark_price, liquidation_price)
            
            if action != ExposureAction.HOLD:
                # Calculate buffer for logging
                if liquidation_price and liquidation_price > 0:
                    if position.side == Side.LONG:
                        buffer_pct = (mark_price - liquidation_price) / mark_price
                    else:
                        buffer_pct = (liquidation_price - mark_price) / mark_price
                else:
                    buffer_pct = Decimal("0")
                
                actions.append(ExposureActionItem(
                    symbol=symbol,
                    action=action,
                    buffer_pct=buffer_pct,
                    reason=f"Liquidation buffer {buffer_pct:.1%} below threshold",
                ))
        
        return actions
