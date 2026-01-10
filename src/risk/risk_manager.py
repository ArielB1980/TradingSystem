"""
Risk management for position sizing and liquidation safety.

Implements:
- Correct position sizing (leverage-independent)
- Liquidation distance calculation (directional)
- Portfolio-level risk limits
- Cost-aware validation
- Non-negotiable safety rules
"""
from decimal import Decimal
from typing import Optional, List
from datetime import datetime, timezone
from src.domain.models import Signal, RiskDecision, Position, Side
from src.config.config import RiskConfig
from src.monitoring.logger import get_logger
from src.storage.repository import record_event

logger = get_logger(__name__)


class RiskManager:
    """
    Risk management and position sizing.
    
    CRITICAL: Position sizing is independent of leverage.
    Leverage determines margin usage, not risk.
    """
    
    def __init__(self, config: RiskConfig):
        """
        Initialize risk manager.
        
        Args:
            config: Risk configuration
        """
        self.config = config
        
        # Portfolio state tracking
        self.current_positions: List[Position] = []
        self.daily_pnl = Decimal("0")
        self.consecutive_losses = 0
        self.daily_start_equity = Decimal("0")
        
        logger.info("Risk Manager initialized", config=config.model_dump())
    
    def validate_trade(
        self,
        signal: Signal,
        account_equity: Decimal,
        spot_price: Decimal,
        perp_mark_price: Decimal,
        exchange_liquidation_price: Optional[Decimal] = None,
        futures_entry_price: Optional[Decimal] = None,
        futures_stop_loss: Optional[Decimal] = None,
    ) -> RiskDecision:
        """
        Validate proposed trade against all risk limits.
        
        Args:
            signal: Trading signal from strategy
            account_equity: Current account equity
            spot_price: Current spot price
            perp_mark_price: Current perpetual mark price
            exchange_liquidation_price: Exchange-reported liquidation price (if position exists)
            futures_entry_price: Converted futures entry price (for accurate risk calc)
            futures_stop_loss: Converted futures stop price (for accurate risk calc)
        
        Returns:
            RiskDecision with approval status and details
        """
        rejection_reasons = []
        
        # Calculate position size using FUTURES prices if available (more accurate)
        # Otherwise fall back to spot prices
        if futures_entry_price and futures_stop_loss:
            entry_for_risk = futures_entry_price
            stop_for_risk = futures_stop_loss
        else:
            entry_for_risk = signal.entry_price
            stop_for_risk = signal.stop_loss
        
        stop_distance_pct = abs(entry_for_risk - stop_for_risk) / entry_for_risk
        position_notional = (account_equity * Decimal(str(self.config.risk_per_trade_pct))) / stop_distance_pct
        
        # Calculate leverage setting (Fixed target as per PRD)
        # We set the order leverage to the configured target (e.g. 10x)
        # This allocates margin based on the SETTING, leaving rest as buffer.
        requested_leverage = Decimal(str(self.config.max_leverage))

        # Determine Effective Leverage for monitoring ONLY
        effective_leverage = position_notional / account_equity
        if effective_leverage > requested_leverage:
             rejection_reasons.append(
                f"Effective leverage {effective_leverage:.2f}× exceeds max {requested_leverage}×"
            )

        margin_required = position_notional / requested_leverage
        leverage = requested_leverage
        
        logger.debug(
            "Position sizing calculated",
            position_notional=str(position_notional),
            leverage=str(leverage),
            margin_required=str(margin_required),
            stop_distance_pct=str(stop_distance_pct),
            using_futures_prices=bool(futures_entry_price),
        )
        
        # Calculate liquidation buffer (if we have exchange-reported liq price)
        liquidation_buffer_pct = Decimal("0")
        if exchange_liquidation_price:
            liquidation_buffer_pct = self._calculate_liquidation_distance(
                perp_mark_price,
                exchange_liquidation_price,
                signal.signal_type.value,
            )
            
            min_buffer = Decimal(str(self.config.min_liquidation_buffer_pct))
            
            if liquidation_buffer_pct < min_buffer:
                rejection_reasons.append(
                    f"Liquidation buffer {liquidation_buffer_pct:.1%} < minimum {min_buffer:.1%}"
                )
        else:
            # Enforce liquidation safety using proxy checks
            # 1. Effective leverage must not be too close to max
            max_effective_leverage = requested_leverage * Decimal("0.90")  # 90% of max
            if effective_leverage > max_effective_leverage:
                rejection_reasons.append(
                    f"Effective leverage {effective_leverage:.2f}× too close to max {requested_leverage}×"
                )
            
            # 2. Require minimum free margin buffer
            min_free_margin_pct = Decimal("0.15")  # 15% safety buffer
            free_margin_pct = (account_equity - margin_required) / account_equity
            if free_margin_pct < min_free_margin_pct:
                rejection_reasons.append(
                    f"Insufficient margin buffer: {free_margin_pct:.1%} < {min_free_margin_pct:.1%}"
                )
        
        # Calculate basis divergence
        basis_divergence_pct = abs(spot_price - perp_mark_price) / spot_price
        
        # BASIS GUARD ENFORCEMENT
        # This was missing a hard check
        basis_max = Decimal(str(getattr(self.config, 'basis_max_pct', '0.0075'))) 
        if basis_divergence_pct > basis_max:
             rejection_reasons.append(
                f"Basis divergence {basis_divergence_pct:.2%} > limit {basis_max:.2%}"
            )

        # Portfolio-level limits
        if len(self.current_positions) >= self.config.max_concurrent_positions:
            rejection_reasons.append(
                f"Max concurrent positions ({self.config.max_concurrent_positions}) reached"
            )
        
        # Daily loss limit
        daily_loss_pct = abs(self.daily_pnl) / self.daily_start_equity if self.daily_start_equity > 0 else Decimal("0")
        if self.daily_pnl < 0 and daily_loss_pct > Decimal(str(self.config.daily_loss_limit_pct)):
            rejection_reasons.append(
                f"Daily loss limit exceeded: {daily_loss_pct:.1%} > {self.config.daily_loss_limit_pct:.1%}"
            )
        
        # Loss streak cooldown
        if self.consecutive_losses >= self.config.loss_streak_cooldown:
            rejection_reasons.append(
                f"Loss streak cooldown: {self.consecutive_losses} consecutive losses"
            )
        
        # Estimate fees and funding
        estimated_fees_funding = self._estimate_costs(position_notional)
        
        # Cost-aware validation
        risk_amount = position_notional * stop_distance_pct
        rr_distortion = estimated_fees_funding / risk_amount if risk_amount > 0 else Decimal("0")
        
        # Determine applicable cap based on stop tightness
        # If stop is TIGHT (<= 1.5%), allow higher distortion (up to 20%)
        # If stop is WIDE (> 1.5%), enforce strict limit (10%)
        tight_threshold = Decimal(str(self.config.tight_stop_threshold_pct))
        if stop_distance_pct <= tight_threshold:
            max_distortion = Decimal(str(self.config.max_fee_funding_rr_distortion_pct))
        else:
            max_distortion = Decimal(str(self.config.rr_distortion_strict_limit_pct))

        if rr_distortion > max_distortion:
            rejection_reasons.append(
                f"Fees+funding distort R:R by {rr_distortion:.1%} > max {max_distortion:.1%} (Stop: {stop_distance_pct:.2%})"
            )
        
        # Approve or reject
        approved = len(rejection_reasons) == 0
        
        decision = RiskDecision(
            approved=approved,
            position_notional=position_notional,
            leverage=leverage,
            margin_required=margin_required,
            liquidation_buffer_pct=liquidation_buffer_pct,
            basis_divergence_pct=basis_divergence_pct,
            estimated_fees_funding=estimated_fees_funding,
            rejection_reasons=rejection_reasons,
        )
        
        if approved:
            logger.info(
                "Trade approved",
                symbol=signal.symbol,
                notional=str(position_notional),
                leverage=str(leverage),
            )
        else:
            logger.warning(
                "Trade rejected",
                symbol=signal.symbol,
                reasons=rejection_reasons,
            )
            
        # --- EXPLAINABILITY INSTRUMENTATION ---
        
        # Determine strictness tier for logging
        tight_threshold = Decimal(str(self.config.tight_stop_threshold_pct))
        strictness_tier = "TIGHT" if stop_distance_pct <= tight_threshold else "NORMAL"
        
        validation_data = {
            "approved": approved,
            "reasons": rejection_reasons,
            "metrics": {
                "position_notional": float(position_notional),
                "leverage": float(leverage),
                "margin_required": float(margin_required),
                "stop_distance_pct": float(stop_distance_pct),
                "rr_distortion": float(rr_distortion),
                "liquidation_buffer_pct": float(liquidation_buffer_pct),
                "basis_divergence_pct": float(basis_divergence_pct),
            },
            "limits": {
                "max_leverage": float(self.config.max_leverage),
                "max_distortion": float(max_distortion),
                "strictness_tier": strictness_tier
            }
        }
        
        record_event("RISK_VALIDATION", signal.symbol, validation_data)
        
        return decision
    
    def _calculate_liquidation_distance(
        self,
        mark_price: Decimal,
        liq_price: Decimal,
        side: str,
    ) -> Decimal:
        """
        Calculate directional liquidation distance.
        
        Formula (directional):
        - Long: (mark_price - liq_price) / mark_price
        - Short: (liq_price - mark_price) / mark_price
        
        Args:
            mark_price: Current mark price
            liq_price: Exchange-reported liquidation price
            side: "long" or "short"
        
        Returns:
            Liquidation distance as percentage (positive = safe)
        """
        if side == "long":
            distance = (mark_price - liq_price) / mark_price
        else:  # short
            distance = (liq_price - mark_price) / mark_price
        
        return distance
    
    def _estimate_costs(self, position_notional: Decimal) -> Decimal:
        """
        Estimate total fees and funding costs using configurable parameters.
        
        Args:
            position_notional: Position size in USD notional
        
        Returns:
            Estimated total cost
        """
        # Use configurable fee assumptions
        taker_fee_bps = Decimal(str(self.config.taker_fee_bps))
        funding_rate_daily = Decimal(str(self.config.funding_rate_daily_bps))
        
        entry_fee = position_notional * (taker_fee_bps / Decimal("10000"))
        exit_fee = position_notional * (taker_fee_bps / Decimal("10000"))
        funding = position_notional * (funding_rate_daily / Decimal("10000"))
        
        total = entry_fee + exit_fee + funding
        
        return total
    
    def update_position_list(self, positions: List[Position]):
        """Update current positions for portfolio tracking."""
        self.current_positions = positions
    
    def record_trade_result(self, net_pnl: Decimal):
        """
        Record trade result for daily P&L and streak tracking.
        
        Args:
            net_pnl: Net P&L from closed trade
        """
        self.daily_pnl += net_pnl
        
        if net_pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0
        
        logger.info(
            "Trade result recorded",
            net_pnl=str(net_pnl),
            daily_pnl=str(self.daily_pnl),
            consecutive_losses=self.consecutive_losses,
        )
    
    def reset_daily_metrics(self, starting_equity: Decimal):
        """Reset daily metrics at start of new trading day."""
        self.daily_pnl = Decimal("0")
        self.daily_start_equity = starting_equity
        logger.info("Daily metrics reset", starting_equity=str(starting_equity))
