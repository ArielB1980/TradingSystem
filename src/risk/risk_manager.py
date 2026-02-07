"""
Risk management for position sizing, leverage control, and safety limits.
"""
from typing import List, Optional, TYPE_CHECKING
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass

from src.domain.models import Signal, RiskDecision, Position, Side
from src.config.config import RiskConfig, TierConfig, LiquidityFilters
from src.monitoring.logger import get_logger
from src.storage.repository import record_event
from src.risk.basis_guard import BasisGuard

if TYPE_CHECKING:
    from src.config.config import Config

logger = get_logger(__name__)


class RiskManager:
    """
    Risk management and position sizing.
    
    CRITICAL: Position sizing is independent of leverage.
    Leverage determines margin usage, not risk.
    
    Supports tier-based sizing when liquidity_filters is provided:
    - Tier A (high liquidity): Full leverage and size limits
    - Tier B (medium liquidity): Reduced leverage and size
    - Tier C (low liquidity): Most conservative limits
    """
    
    def __init__(self, config: RiskConfig, *, liquidity_filters: Optional[LiquidityFilters] = None):
        """
        Initialize risk manager.
        
        Args:
            config: Risk configuration
            liquidity_filters: Optional liquidity filters with tier configs for tier-based sizing
        """
        self.config = config
        self.liquidity_filters = liquidity_filters
        
        # Portfolio state tracking
        self.current_positions: List[Position] = []
        self.daily_pnl = Decimal("0")
        self.daily_start_equity = Decimal("0")
        
        # Regime-specific streak tracking
        self.consecutive_losses_tight = 0
        self.consecutive_losses_wide = 0
        
        self.cooldown_until: Optional[datetime] = None  # Time-based cooldown
        
        tier_info = "enabled" if liquidity_filters else "disabled"
        logger.info("Risk Manager initialized", config=config.model_dump(), tier_based_sizing=tier_info)
    
    def get_tier_config(self, tier: str) -> Optional[TierConfig]:
        """Get tier-specific config if liquidity_filters is set."""
        if self.liquidity_filters:
            return self.liquidity_filters.get_tier_config(tier)
        return None
    
    def validate_trade(
        self,
        signal: Signal,
        account_equity: Decimal,
        spot_price: Decimal,
        perp_mark_price: Decimal,
        exchange_liquidation_price: Optional[Decimal] = None,
        futures_entry_price: Optional[Decimal] = None,
        futures_stop_loss: Optional[Decimal] = None,
        available_margin: Optional[Decimal] = None,
        notional_override: Optional[Decimal] = None,
        skip_margin_check: bool = False,
        symbol_tier: Optional[str] = None,
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
            available_margin: Kraken available margin (equity minus margin in use). When set,
                position size is capped so we never exceed it, preventing insufficientAvailableFunds.
        
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

        # Validate entry price to prevent division by zero
        if entry_for_risk <= 0:
            logger.error(
                "Invalid entry price for risk calculation",
                symbol=signal.symbol,
                entry_for_risk=str(entry_for_risk),
                futures_entry_price=str(futures_entry_price) if futures_entry_price else None,
                signal_entry_price=str(signal.entry_price)
            )
            return RiskDecision(
                approved=False,
                rejection_reasons=["Invalid entry price (zero or negative)"],
                position_notional=Decimal("0"),
                leverage=Decimal("1"),
                margin_required=Decimal("0"),
                liquidation_buffer_pct=Decimal("0"),
                basis_divergence_pct=Decimal("0"),
                estimated_fees_funding=Decimal("0")
            )

        stop_distance_pct = abs(entry_for_risk - stop_for_risk) / entry_for_risk

        # Validate stop distance to prevent division by zero
        if stop_distance_pct <= 0:
            logger.error(
                "Invalid stop distance for risk calculation",
                symbol=signal.symbol,
                entry_for_risk=str(entry_for_risk),
                stop_for_risk=str(stop_for_risk),
                stop_distance_pct=str(stop_distance_pct)
            )
            return RiskDecision(
                approved=False,
                rejection_reasons=["Invalid stop distance (stop equals entry)"],
                position_notional=Decimal("0"),
                leverage=Decimal("1"),
                margin_required=Decimal("0"),
                liquidation_buffer_pct=Decimal("0"),
                basis_divergence_pct=Decimal("0"),
                estimated_fees_funding=Decimal("0")
            )

        # Validate account equity to prevent division by zero
        if account_equity <= 0:
            logger.error(
                "Invalid account equity for risk calculation",
                symbol=signal.symbol,
                account_equity=str(account_equity)
            )
            return RiskDecision(
                approved=False,
                rejection_reasons=["Invalid account equity (zero or negative)"],
                position_notional=Decimal("0"),
                leverage=Decimal("1"),
                margin_required=Decimal("0"),
                liquidation_buffer_pct=Decimal("0"),
                basis_divergence_pct=Decimal("0"),
                estimated_fees_funding=Decimal("0")
            )

        # Calculate leverage setting (use target leverage for sizing)
        # target_leverage determines actual position leverage (e.g., 7x)
        # max_leverage is the absolute cap for safety checks (10x)
        requested_leverage = Decimal(str(getattr(self.config, 'target_leverage', self.config.max_leverage)))
        
        # Apply tier-specific leverage cap if symbol_tier is provided
        tier_config = self.get_tier_config(symbol_tier) if symbol_tier else None
        tier_max_leverage = Decimal(str(tier_config.max_leverage)) if tier_config else None
        tier_max_size = tier_config.max_position_size_usd if tier_config else None
        
        logger.info(
            "Trade tier classification",
            symbol=signal.symbol,
            tier=symbol_tier or "none",
            tier_max_leverage=str(tier_max_leverage) if tier_max_leverage else "global",
            tier_max_size=str(tier_max_size) if tier_max_size else "global",
        )
        
        if tier_max_leverage and requested_leverage > tier_max_leverage:
            logger.info(
                "Applying tier leverage cap",
                symbol=signal.symbol,
                tier=symbol_tier,
                original_leverage=str(requested_leverage),
                tier_max_leverage=str(tier_max_leverage),
            )
            requested_leverage = tier_max_leverage
        
        # Get sizing method (needed for later logic even if using override)
        sizing_method = getattr(self.config, 'sizing_method', 'fixed')
        
        # Calculate buying_power (needed for later checks even if using override)
        buying_power = account_equity * requested_leverage
        
        # If notional_override provided (auction execution), use it directly and skip sizing
        if notional_override is not None:
            position_notional = notional_override
            logger.debug(
                "Using notional override from auction",
                symbol=signal.symbol,
                notional=str(position_notional),
            )
        else:
            # --- NEW SIZING LOGIC (V4: Adaptive) ---
            # Leverage-Based Sizing (Simple)
            # Position size = Equity × Leverage × Risk%
            if sizing_method == "leverage_based":
                position_notional = buying_power * Decimal(str(self.config.risk_per_trade_pct))
                logger.debug(
                    "Leverage-based sizing",
                    equity=str(account_equity),
                    leverage=str(requested_leverage),
                    risk_pct=str(self.config.risk_per_trade_pct),
                    position_notional=str(position_notional)
                )
            else:
                # Base Sizing (Fixed Risk)
                # Position size = (Equity * Risk%) / Stop_Dist%
                base_risk_amount = account_equity * Decimal(str(self.config.risk_per_trade_pct))
                position_notional = base_risk_amount / stop_distance_pct

            # Kelly Criterion Sizing (skip if leverage_based)
            if sizing_method in ["kelly", "kelly_volatility"]:
                win_prob = Decimal(str(self.config.kelly_win_prob))
                win_loss_ratio = Decimal(str(self.config.kelly_win_loss_ratio))
                
                # Kelly % = W - (1-W)/R
                kelly_pct = win_prob - ((Decimal("1") - win_prob) / win_loss_ratio)
                
                # Apply Cap (Quarter Kelly defaults to 0.25)
                max_kelly = Decimal(str(self.config.kelly_max_fraction))
                kelly_fraction = min(kelly_pct, max_kelly)
                
                if kelly_fraction > 0:
                    # Kelly suggests risking X% of bankroll per trade
                    # Risk Amount = Equity * Kelly%
                    kelly_risk_amount = account_equity * kelly_fraction
                    
                    # Check absolute max risk cap
                    max_risk_pct = Decimal(str(self.config.max_risk_per_trade_entry_pct))
                    abs_risk_cap = account_equity * max_risk_pct
                    
                    final_risk_amount = min(kelly_risk_amount, abs_risk_cap)
                    
                    kelly_notional = final_risk_amount / stop_distance_pct
                    position_notional = kelly_notional
                    logger.debug(f"Kelly Sizing: Frac={kelly_fraction:.2f}, Risk=${final_risk_amount:.2f}")
            
            # Volatility Scaling
            if sizing_method in ["volatility", "kelly_volatility"]:
                # Check availability of ATR Ratio from Signal
                if hasattr(signal, 'atr_ratio') and signal.atr_ratio is not None:
                    ratio = float(signal.atr_ratio)
                    scaler = 1.0
                    
                    high_threshold = float(getattr(self.config, 'vol_sizing_atr_threshold_high', 1.5))
                    low_threshold = float(getattr(self.config, 'vol_sizing_atr_threshold_low', 0.8))
                    
                    if ratio > high_threshold:
                        penalty = float(getattr(self.config, 'vol_sizing_high_vol_penalty', 0.6))
                        scaler = penalty
                        logger.debug(f"Volatility Sizing: High Vol (Ratio {ratio:.2f}) -> Penalty {penalty}x")
                        
                    elif ratio < low_threshold:
                        boost = float(getattr(self.config, 'vol_sizing_low_vol_boost', 1.2))
                        scaler = boost
                        logger.debug(f"Volatility Sizing: Low Vol (Ratio {ratio:.2f}) -> Boost {boost}x")
                        
                    position_notional *= Decimal(str(scaler))
                else:
                    logger.debug("Volatility Sizing skipped: atr_ratio missing in Signal")
            
            # Hard Cap: Max Notional USD (use tier-specific cap if available)
            max_usd = Decimal(str(self.config.max_position_size_usd))
            if tier_max_size and tier_max_size < max_usd:
                effective_max_usd = tier_max_size
                logger.debug(
                    "Using tier max position size",
                    symbol=signal.symbol,
                    tier=symbol_tier,
                    tier_max=str(tier_max_size),
                    global_max=str(max_usd),
                )
            else:
                effective_max_usd = max_usd
            
            if position_notional > effective_max_usd:
                position_notional = effective_max_usd

        # Hard Cap: Max Leverage Buying Power (only if not using override)
        if notional_override is None:
            buying_power = account_equity * requested_leverage
            if position_notional > buying_power:
                 position_notional = buying_power

        # Hard Cap: Max single position as % of equity (pre-trade enforcement)
        # This prevents opening positions larger than 25% of equity (notional basis).
        # The invariant monitor checks this post-trade (pos_notional / equity),
        # so we enforce the same formula here pre-trade.
        # IMPORTANT: This applies to ALL paths including auction overrides.
        max_position_pct_equity = Decimal("0.25")  # 25% of equity max per position (notional)
        max_notional_from_equity = account_equity * max_position_pct_equity
        if position_notional > max_notional_from_equity:
            logger.info(
                "Capping position notional by max_single_position_pct_equity",
                symbol=signal.symbol,
                before=str(position_notional),
                after=str(max_notional_from_equity),
                equity=str(account_equity),
                max_pct=str(max_position_pct_equity),
            )
            position_notional = max_notional_from_equity
        
        # Reject if capped notional is below minimum viable size
        min_notional_viable = Decimal("10")
        if position_notional < min_notional_viable:
            rejection_reasons.append(
                f"Position notional ${position_notional:.2f} below minimum ${min_notional_viable} "
                f"after equity cap (equity=${account_equity:.2f}, max_pct={max_position_pct_equity:.0%})"
            )

        # Cap by available margin (prevents Kraken "insufficientAvailableFunds")
        # Skip margin check if skip_margin_check=True (auction already validated)
        # Note: min_notional lowered to $10 to support smaller accounts with tier C (2x leverage)
        min_notional = Decimal("10")
        if not skip_margin_check and available_margin is not None:
            if available_margin <= 0:
                rejection_reasons.append(
                    "Insufficient available margin (all margin in use); cannot open new position"
                )
                return RiskDecision(
                    approved=False,
                    rejection_reasons=rejection_reasons,
                    position_notional=Decimal("0"),
                    leverage=requested_leverage,
                    margin_required=Decimal("0"),
                    liquidation_buffer_pct=Decimal("0"),
                    basis_divergence_pct=Decimal("0"),
                    estimated_fees_funding=Decimal("0"),
                )
            # Use ~95% of available to leave buffer for fees/slippage
            max_margin_use = available_margin * Decimal("0.95")
            max_notional_from_avail = max_margin_use * requested_leverage
            if position_notional > max_notional_from_avail:
                logger.debug(
                    "Capping position notional by available margin",
                    symbol=signal.symbol,
                    available_margin=str(available_margin),
                    before=str(position_notional),
                    after=str(max_notional_from_avail),
                )
                position_notional = max_notional_from_avail
            if position_notional < min_notional:
                rejection_reasons.append(
                    f"Insufficient available margin: would allow only ${position_notional:.0f} notional (min ${min_notional})"
                )
                return RiskDecision(
                    approved=False,
                    rejection_reasons=rejection_reasons,
                    position_notional=Decimal("0"),
                    leverage=requested_leverage,
                    margin_required=Decimal("0"),
                    liquidation_buffer_pct=Decimal("0"),
                    basis_divergence_pct=Decimal("0"),
                    estimated_fees_funding=Decimal("0"),
                )
        elif skip_margin_check:
            logger.debug(
                "Skipping margin check (auction execution)",
                symbol=signal.symbol,
                notional=str(position_notional),
            )
        
        logger.debug(
            f"Validating trade: {len(self.current_positions)} active positions",
            symbol=signal.symbol,
            equity=str(account_equity),
            buying_power=str(buying_power),
            available_margin=str(available_margin) if available_margin is not None else "n/a",
        )

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
        if spot_price <= 0:
            logger.error(
                "Invalid spot price for basis calculation",
                symbol=signal.symbol,
                spot_price=str(spot_price)
            )
            rejection_reasons.append("Invalid spot price (zero or negative)")
            basis_divergence_pct = Decimal("0")
        else:
            basis_divergence_pct = abs(spot_price - perp_mark_price) / spot_price
        
        # BASIS GUARD ENFORCEMENT
        # This was missing a hard check
        basis_max = Decimal(str(getattr(self.config, 'basis_max_pct', '0.0075'))) 
        if basis_divergence_pct > basis_max:
             rejection_reasons.append(
                f"Basis divergence {basis_divergence_pct:.2%} > limit {basis_max:.2%}"
            )

        # Portfolio-level limits
        should_close_existing = False
        close_symbol = None
        
        # If auction mode is enabled, ignore max_concurrent_positions and use auction_max_positions instead
        position_limit = self.config.auction_max_positions if self.config.auction_mode_enabled else self.config.max_concurrent_positions
        
        if len(self.current_positions) >= position_limit:
            # Replacement is explicitly opt-in.
            # Default behavior (especially in prod): reject when at limit (no close-then-open race).
            if not bool(getattr(self.config, "replacement_enabled", False)):
                rejection_reasons.append(
                    f"Max concurrent positions ({position_limit}) reached"
                )
            else:
                # OPPORTUNITY COST LOGIC (LEGACY / DEPRECATED)
                # NOTE: This is intentionally disabled by default. If re-enabled, it should be
                # replaced by an explicit persisted entry_score + strict guards.
            
                # 1. Calculate New Trade R:R
                if signal.take_profit:
                    new_reward = abs(signal.take_profit - signal.entry_price)
                    new_risk = abs(signal.entry_price - signal.stop_loss)
                    new_rr = new_reward / new_risk if new_risk > 0 else Decimal("0")
                else:
                    new_rr = Decimal("0")
                
                # 2. Find Weakest Existing Position
                weakest_pos = None
                lowest_rr = Decimal("9999")
            
                for pos in self.current_positions:
                    # Calculate existing R:R based on initial params (if available)
                    if pos.final_target_price and pos.initial_stop_price and pos.entry_price:
                        curr_reward = abs(pos.final_target_price - pos.entry_price)
                        curr_risk = abs(pos.entry_price - pos.initial_stop_price)
                        curr_rr = curr_reward / curr_risk if curr_risk > 0 else Decimal("0")
                        
                        if curr_rr < lowest_rr:
                            lowest_rr = curr_rr
                            weakest_pos = pos
            
                # 3. Compare
                # Threshold: New potential must be > 2.0x existing potential
                if weakest_pos and new_rr > (lowest_rr * Decimal("2.0")):
                    should_close_existing = True
                    close_symbol = weakest_pos.symbol
                    logger.info(
                        "Opportunity Cost Override Triggered",
                        new_symbol=signal.symbol,
                        new_rr=float(new_rr),
                        weakest_symbol=weakest_pos.symbol,
                        weakest_rr=float(lowest_rr),
                        multiplier=float(new_rr/lowest_rr if lowest_rr > 0 else 0)
                    )
                else:
                    rejection_reasons.append(
                        f"Max concurrent positions ({position_limit}) reached"
                    )
        
        # Daily loss limit
        daily_loss_pct = abs(self.daily_pnl) / self.daily_start_equity if self.daily_start_equity > 0 else Decimal("0")
        if self.daily_pnl < 0 and daily_loss_pct > Decimal(str(self.config.daily_loss_limit_pct)):
            rejection_reasons.append(
                f"Daily loss limit exceeded: {daily_loss_pct:.1%} > {self.config.daily_loss_limit_pct:.1%}"
            )
        
        # Time-based loss streak cooldown (NEW - prevents deadlock)
        now = datetime.now(timezone.utc)
        
        if self.cooldown_until and now < self.cooldown_until:
            remaining_minutes = int((self.cooldown_until - now).total_seconds() / 60)
            rejection_reasons.append(
                f"Loss streak cooldown active: {remaining_minutes} minutes remaining until {self.cooldown_until.strftime('%H:%M UTC')}"
            )
        
        # **REGIME-SPECIFIC VALIDATION** (NEW)
        # Different cost models for tight-stop SMC vs wide-stop structure
        regime = signal.regime  # "tight_smc" or "wide_structure"
        
        if regime == "tight_smc":
            # Tight-stop SMC (OB/FVG): 0.4-1.0% stops
            # DISABLE R:R distortion filter
            # INSTEAD: Absolute cost cap + minimum R multiple
            
            # 1. Calculate expected costs with probabilistic funding
            estimated_fees_funding = self._estimate_costs_tight_smc(position_notional, stop_distance_pct)
            
            # 2. Absolute cost cap (e.g., 25 bps max)
            cost_cap_decimal = Decimal(str(self.config.tight_smc_cost_cap_bps / 10000))  # bps to decimal
            if estimated_fees_funding > position_notional * cost_cap_decimal:
                rejection_reasons.append(
                    f"Total cost ${estimated_fees_funding:.2f} exceeds {self.config.tight_smc_cost_cap_bps:.0f} bps cap on ${position_notional:.2f} notional"
                )
            
            # 3. Minimum R:R multiple (ensure TP is far enough)
            if signal.take_profit:
                tp_distance = abs(signal.take_profit - signal.entry_price)
                stop_distance = abs(signal.stop_loss - signal.entry_price)
                rr_multiple = tp_distance / stop_distance if stop_distance > 0 else Decimal("0")
                
                min_rr = Decimal(str(self.config.tight_smc_min_rr_multiple))
                if rr_multiple < min_rr:
                    rejection_reasons.append(
                        f"R:R multiple {rr_multiple:.1f} < minimum {min_rr:.1f} for tight-stop SMC"
                    )
            
            # For logging
            risk_amount = position_notional * stop_distance_pct
            rr_distortion = estimated_fees_funding / risk_amount if risk_amount > 0 else Decimal("0")
            max_distortion = cost_cap_decimal  # For compatibility
        
        else:
            # Wide-stop structure (BOS/TREND): 1.5-3.0% stops
            # KEEP R:R distortion filter (existing logic)
            
            estimated_fees_funding = self._estimate_costs_wide_structure(position_notional, stop_distance_pct)
            
            # Cost-aware R:R distortion
            risk_amount = position_notional * stop_distance_pct
            rr_distortion = estimated_fees_funding / risk_amount if risk_amount > 0 else Decimal("0")
            
            # Use regime-specific distortion limit (e.g., 15%)
            max_distortion = Decimal(str(self.config.wide_structure_max_distortion_pct))
            
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
            should_close_existing=should_close_existing,
            close_symbol=close_symbol
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
        """
        if side == "long":
            distance = (mark_price - liq_price) / mark_price
        else:  # short
            distance = (liq_price - mark_price) / mark_price
        
        return distance
    
    def _estimate_costs_tight_smc(self, position_notional: Decimal, stop_distance_pct: Decimal) -> Decimal:
        """
        Estimate costs for TIGHT-STOP SMC trades (OB/FVG).
        """
        taker_fee_bps = Decimal(str(self.config.taker_fee_bps))
        
        entry_fee = position_notional * (taker_fee_bps / Decimal("10000"))
        exit_fee = position_notional * (taker_fee_bps / Decimal("10000"))
        
        # Probabilistic funding
        avg_hold_hours = Decimal(str(self.config.tight_smc_avg_hold_hours))  # e.g., 6 hours
        funding_interval_hours = Decimal("8")  # Funding every 8 hours
        
        # Probability of paying funding = min(avg_hold / 8, 1.0)
        funding_probability = min(avg_hold_hours / funding_interval_hours, Decimal("1.0"))
        
        daily_funding_bps = Decimal(str(self.config.funding_rate_daily_bps))
        one_interval_funding = position_notional * (daily_funding_bps / Decimal("10000")) / Decimal("3")  # Daily / 3 intervals
        
        expected_funding = one_interval_funding * funding_probability
        
        total = entry_fee + exit_fee + expected_funding
        
        return total
    
    def _estimate_costs_wide_structure(self, position_notional: Decimal, stop_distance_pct: Decimal) -> Decimal:
        """
        Estimate costs for WIDE-STOP structure trades (BOS/TREND).
        """
        taker_fee_bps = Decimal(str(self.config.taker_fee_bps))
        
        entry_fee = position_notional * (taker_fee_bps / Decimal("10000"))
        exit_fee = position_notional * (taker_fee_bps / Decimal("10000"))
        
        # Multi-interval funding
        avg_hold_hours = Decimal(str(self.config.wide_structure_avg_hold_hours))  # e.g., 36 hours
        funding_interval_hours = Decimal("8")
        
        funding_intervals = avg_hold_hours / funding_interval_hours
        
        daily_funding_bps = Decimal(str(self.config.funding_rate_daily_bps))
        one_interval_funding = position_notional * (daily_funding_bps / Decimal("10000")) / Decimal("3")
        
        expected_funding = one_interval_funding * funding_intervals
        
        total = entry_fee + exit_fee + expected_funding
        
        return total
    
    def update_position_list(self, positions: List[Position]):
        """Update current positions for portfolio tracking."""
        prev_count = len(self.current_positions)
        self.current_positions = positions
        if len(positions) != prev_count:
            logger.info(f"RiskManager position count updated: {prev_count} -> {len(positions)}")
    
    def record_trade_result(self, net_pnl: Decimal, account_equity: Decimal, setup_type: Optional[str] = None):
        """
        Record trade result for daily P&L and streak tracking.
        
        CRITICAL CHANGE: Regime-aware loss streaks.
        - tight_smc: shorter tolerance (3 losses), longer pause (120m)
        - wide_structure: longer tolerance (4-5 losses), shorter pause (90m)
        
        Args:
            net_pnl: Net P&L from the trade
            account_equity: Current account equity
            setup_type: str "ob"/"fvg" (tight) or "bos"/"trend" (wide)
        """
        from src.domain.models import SetupType
        
        self.daily_pnl += net_pnl
        
        # Determine regime
        is_tight = setup_type in [SetupType.OB, SetupType.FVG] if setup_type else False
        
        # Only count MEANINGFUL losses (> X bps of equity)
        loss_bps = abs(net_pnl) / account_equity * Decimal("10000") if account_equity > 0 else Decimal("0")
        min_loss_bps = Decimal(str(self.config.loss_streak_min_loss_bps))
        
        if net_pnl < 0 and loss_bps >= min_loss_bps:
            # Loss Logic
            if is_tight:
                self.consecutive_losses_tight += 1
                limit = self.config.loss_streak_cooldown_tight
                pause_min = self.config.loss_streak_pause_minutes_tight
                msg_prefix = "Tight SMC"
            else:
                self.consecutive_losses_wide += 1
                limit = self.config.loss_streak_cooldown_wide
                pause_min = self.config.loss_streak_pause_minutes_wide
                msg_prefix = "Wide Structure"
            
            # Check Threshold
            current_streak = self.consecutive_losses_tight if is_tight else self.consecutive_losses_wide
            
            if current_streak >= limit:
                # Activate Pause
                pause_duration = timedelta(minutes=pause_min)
                self.cooldown_until = datetime.now(timezone.utc) + pause_duration
                
                logger.warning(
                    f"{msg_prefix} Streak Limit Reached - COOLDOWN ACTIVATED",
                    streak=current_streak,
                    limit=limit,
                    pause_min=pause_min,
                    cooldown_until=self.cooldown_until.isoformat()
                )
                
                # Reset ALL streaks on cooldown activation to prevent immediate re-trigger
                self.consecutive_losses_tight = 0
                self.consecutive_losses_wide = 0
        
        elif net_pnl > 0:
            # Win Logic - Reset ALL streaks
            # A win restores confidence in the system overall
            self.consecutive_losses_tight = 0
            self.consecutive_losses_wide = 0
            
            if self.cooldown_until:
                logger.info("Win recorded - clearing active cooldown early")
                self.cooldown_until = None
        
        logger.info(
            "Trade result recorded",
            net_pnl=str(net_pnl),
            daily_pnl=str(self.daily_pnl),
            streaks={
                "tight": self.consecutive_losses_tight,
                "wide": self.consecutive_losses_wide
            },
            cooldown_active=bool(self.cooldown_until),
        )
    
    def reset_daily_metrics(self, starting_equity: Decimal):
        """Reset daily metrics at start of new trading day."""
        self.daily_pnl = Decimal("0")
        self.daily_start_equity = starting_equity
        self.consecutive_losses_tight = 0
        self.consecutive_losses_wide = 0
        logger.info("Daily metrics reset", starting_equity=str(starting_equity))
