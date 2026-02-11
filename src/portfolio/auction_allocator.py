"""
Auction-based portfolio allocator.

Implements deterministic auction logic to select the best 50 positions each cycle,
with hysteresis and cost penalties to prevent churn.
"""
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Set, Tuple
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from enum import Enum

from src.domain.models import Position, Signal, Side
from src.data.symbol_utils import normalize_symbol_for_position_match as _normalize_symbol_for_matching
from src.monitoring.logger import get_logger

logger = get_logger(__name__)


class ContenderKind(str, Enum):
    """Type of contender in the auction."""
    OPEN = "open"
    NEW = "new"


@dataclass
class OpenPositionMetadata:
    """Metadata for an open position needed for auction evaluation."""
    position: Position
    entry_time: datetime
    entry_score: float
    current_pnl_R: Decimal  # PnL in R multiples
    margin_used: Decimal
    cluster: str  # e.g., "tight_smc_ob", "wide_structure_bos"
    direction: Side
    age_seconds: float
    is_protective_orders_live: bool
    locked: bool = False  # Cannot be kicked (within MIN_HOLD, protective orders not live, etc.)
    spot_symbol: Optional[str] = None  # Spot symbol for matching against candidate signals


@dataclass
class CandidateSignal:
    """Candidate signal for entry."""
    signal: Signal
    score: float
    direction: Side
    symbol: str
    cluster: str
    required_margin: Decimal
    risk_R: Decimal  # Stop distance in R
    position_notional: Decimal  # Pre-computed position notional from auction sizing


@dataclass
class PortfolioLimits:
    """Portfolio-level constraints."""
    max_positions: int = 50
    max_margin_util: float = 0.90  # 90% of available margin
    max_per_cluster: int = 12  # For 50 positions, ~12 per cluster
    max_per_symbol: int = 1  # One position per symbol
    max_net_long: Optional[Decimal] = None  # Optional net exposure cap
    max_net_short: Optional[Decimal] = None


@dataclass
class Contender:
    """A single contender in the auction (open position or new candidate)."""
    kind: ContenderKind
    symbol: str
    cluster: str
    direction: Side
    required_margin: Decimal  # For NEW, margin_used for OPEN
    value: float  # Computed value score
    locked: bool = False  # OPEN only
    
    # Reference to original object
    open_metadata: Optional[OpenPositionMetadata] = None
    candidate: Optional[CandidateSignal] = None
    
    # For tie-breaking
    age_seconds: float = 0.0  # OPEN only


@dataclass
class AllocationPlan:
    """Result of the auction allocation."""
    opens: List[Signal]  # Signals to open
    closes: List[str]  # Position symbols to close
    reductions: List[Tuple[str, Decimal]] = field(default_factory=list)  # Optional size reductions
    reasons: Dict[str, any] = field(default_factory=dict)  # Logging metadata


class AuctionAllocator:
    """
    Deterministic auction allocator for portfolio management.
    
    Selects the best 50 positions each cycle using value scoring,
    with hysteresis and cost penalties to prevent churn.
    """
    
    def __init__(
        self,
        limits: PortfolioLimits,
        swap_threshold: float = 10.0,
        min_hold_minutes: int = 15,
        max_trades_per_cycle: int = 5,
        max_new_opens_per_cycle: int = 5,
        max_closes_per_cycle: int = 5,
        entry_cost: float = 2.0,
        exit_cost: float = 2.0,
    ):
        """
        Initialize auction allocator.
        
        Args:
            limits: Portfolio constraints
            swap_threshold: Minimum score advantage required to replace an open position
            min_hold_minutes: Minimum time before a position can be kicked
            max_trades_per_cycle: Maximum total trades per cycle (anti-churn)
            max_new_opens_per_cycle: Maximum new positions per cycle
            max_closes_per_cycle: Maximum closes per cycle
            entry_cost: Entry cost penalty in score points
            exit_cost: Exit cost penalty in score points
        """
        self.limits = limits
        self.swap_threshold = swap_threshold
        self.min_hold_seconds = min_hold_minutes * 60
        self.max_trades_per_cycle = max_trades_per_cycle
        self.max_new_opens_per_cycle = max_new_opens_per_cycle
        self.max_closes_per_cycle = max_closes_per_cycle
        self.entry_cost = entry_cost
        self.exit_cost = exit_cost
        
        logger.info(
            "AuctionAllocator initialized",
            max_positions=limits.max_positions,
            swap_threshold=swap_threshold,
            min_hold_minutes=min_hold_minutes,
        )
    
    def allocate(
        self,
        open_positions: List[OpenPositionMetadata],
        candidate_signals: List[CandidateSignal],
        portfolio_state: Dict[str, any],
    ) -> AllocationPlan:
        """
        Run the auction allocation algorithm.
        
        Args:
            open_positions: All open positions with metadata (<=50)
            candidate_signals: All new signals this cycle
            portfolio_state: Current portfolio state (account_equity, available_margin, etc.)
        
        Returns:
            AllocationPlan with opens, closes, and reasons
        """
        # Step A: Build contender list
        contenders = self._build_contender_list(open_positions, candidate_signals, portfolio_state)
        
        if not contenders:
            logger.debug("No contenders in auction")
            return AllocationPlan(opens=[], closes=[], reasons={"message": "No contenders"})
        
        # Step B: Sort by value (descending)
        contenders.sort(key=lambda c: (
            -c.value,  # Higher value first
            0 if c.kind == ContenderKind.OPEN else 1,  # OPEN beats NEW (stability)
            -c.age_seconds,  # Older open wins (stability)
            c.required_margin,  # Lower margin (efficiency)
        ))
        
        # Step C: Select winners under constraints
        winners = self._select_winners(contenders, portfolio_state)
        
        # Step D: Translate into actions
        winner_symbols = {c.symbol for c in winners}
        open_symbols = {op.position.symbol for op in open_positions}
        
        to_keep = [op for op in open_positions if op.position.symbol in winner_symbols]
        to_close_candidates = [op for op in open_positions if op.position.symbol not in winner_symbols]
        to_open_candidates = [c for c in winners if c.kind == ContenderKind.NEW]
        
        # Step E: Apply hysteresis swap rule
        final_closes, final_opens = self._apply_hysteresis(
            to_close_candidates,
            to_open_candidates,
            to_keep,
            portfolio_state,
        )
        
        # Apply per-cycle limits as paired swaps to preserve "best 50"
        # Limit swaps (paired closes+opens) to maintain portfolio quality
        max_swaps = min(self.max_new_opens_per_cycle, self.max_closes_per_cycle, self.max_trades_per_cycle)
        
        # Match closes to opens for swaps
        swap_pairs = []
        remaining_closes = []
        remaining_opens = []
        
        # First, pair up closes with opens (swaps)
        close_by_cluster = {op.position.symbol: op for op in final_closes}
        for new_contender in final_opens[:max_swaps]:
            # Find best matching close in same cluster
            matching_close = None
            for close_op in final_closes:
                if close_op.cluster == new_contender.cluster and close_op.position.symbol not in [p[0] for p in swap_pairs]:
                    if matching_close is None or close_op.position.symbol not in [p[0] for p in swap_pairs]:
                        matching_close = close_op
                        break
            
            if matching_close:
                swap_pairs.append((matching_close.position.symbol, new_contender))
            else:
                remaining_opens.append(new_contender)
        
        # Remaining closes (not part of swaps) - limit independently
        paired_close_symbols = {p[0] for p in swap_pairs}
        remaining_closes = [op for op in final_closes if op.position.symbol not in paired_close_symbols]
        remaining_closes = remaining_closes[:max(self.max_closes_per_cycle - len(swap_pairs), 0)]
        
        # Remaining opens (not part of swaps) - limit independently
        remaining_opens = remaining_opens[:max(self.max_new_opens_per_cycle - len(swap_pairs), 0)]
        
        # Build result
        all_opens = [c.candidate.signal for _, c in swap_pairs if c.candidate] + [c.candidate.signal for c in remaining_opens if c.candidate]
        closes = [symbol for symbol, _ in swap_pairs] + [op.position.symbol for op in remaining_closes]
        
        # CRITICAL FIX: Enforce net_opens <= net_closes + free_slots to prevent exceeding max positions
        current_open_count = len(open_positions)
        free_slots = max(self.limits.max_positions - current_open_count, 0)
        closes_count = len(closes)
        allowed_opens = closes_count + free_slots
        opens = all_opens[:allowed_opens]  # Enforce net position limit
        
        reasons = {
            "total_contenders": len(contenders),
            "winners_selected": len(winners),
            "opens_after_hysteresis": len(final_opens),
            "closes_after_hysteresis": len(final_closes),
            "opens_after_limits": len(opens),
            "closes_after_limits": len(closes),
        }
        
        logger.info(
            "Auction allocation complete",
            **reasons
        )
        
        return AllocationPlan(
            opens=opens,
            closes=closes,
            reasons=reasons,
        )
    
    def _build_contender_list(
        self,
        open_positions: List[OpenPositionMetadata],
        candidate_signals: List[CandidateSignal],
        portfolio_state: Dict[str, any],
    ) -> List[Contender]:
        """Build and filter the contender list."""
        contenders = []
        
        # Add open positions
        now = datetime.now(timezone.utc)
        for op_meta in open_positions:
            # Mark as locked if within MIN_HOLD, protective orders not live, or UNPROTECTED
            is_unprotected = not getattr(op_meta.position, 'is_protected', True)
            locked = (
                op_meta.age_seconds < self.min_hold_seconds or
                not op_meta.is_protective_orders_live or
                is_unprotected
            )
            if is_unprotected:
                logger.warning(
                    "UNPROTECTED position marked as locked in auction",
                    symbol=op_meta.position.symbol,
                    reason=getattr(op_meta.position, 'protection_reason', 'UNKNOWN')
                )
            
            # Update locked state in metadata (for use in hysteresis)
            op_meta.locked = locked
            
            # Compute value for open position
            value = self._compute_open_value(op_meta, portfolio_state)
            
            # CRITICAL: Use spot symbol for matching if available, otherwise use futures symbol
            # This ensures proper symbol matching between open positions (futures) and candidates (spot)
            contender_symbol = op_meta.spot_symbol if op_meta.spot_symbol else op_meta.position.symbol
            
            contender = Contender(
                kind=ContenderKind.OPEN,
                symbol=contender_symbol,  # Use spot symbol for proper matching
                cluster=op_meta.cluster,
                direction=op_meta.direction,
                required_margin=op_meta.margin_used,
                value=value,
                locked=locked,
                open_metadata=op_meta,
                age_seconds=op_meta.age_seconds,
            )
            contenders.append(contender)
        
        # Capital reallocation rate limit: skip new opens when within partial-close cooldown
        last_partial = portfolio_state.get("last_partial_close_at")
        cooldown_sec = portfolio_state.get("partial_close_cooldown_seconds", 0) or 0
        in_cooldown = (
            cooldown_sec > 0
            and last_partial is not None
            and (now - last_partial).total_seconds() < cooldown_sec
        )
        if in_cooldown:
            logger.info(
                "Auction: Skipping new opens (partial-close cooldown)",
                cooldown_sec=cooldown_sec,
                seconds_since_partial=(now - last_partial).total_seconds() if last_partial else 0,
            )

        # Add candidate signals (after hard filters)
        for candidate in candidate_signals:
            if in_cooldown:
                continue
            # Hard constraint checks
            if not self._passes_hard_filters(candidate, portfolio_state):
                continue
            
            # Compute value for new candidate
            value = self._compute_new_value(candidate, portfolio_state)
            
            contender = Contender(
                kind=ContenderKind.NEW,
                symbol=candidate.symbol,
                cluster=candidate.cluster,
                direction=candidate.direction,
                required_margin=candidate.required_margin,
                value=value,
                candidate=candidate,
            )
            contenders.append(contender)
        
        return contenders
    
    def _passes_hard_filters(
        self,
        candidate: CandidateSignal,
        portfolio_state: Dict[str, any],
    ) -> bool:
        """Check if candidate passes hard constraints."""
        # Margin utilization check
        available_margin = portfolio_state.get("available_margin", Decimal("0"))
        if candidate.required_margin > available_margin * Decimal(str(self.limits.max_margin_util)):
            return False
        
        # Symbol cap (would be checked later, but early reject if already at max)
        # This is a soft check - actual enforcement happens in _select_winners
        
        return True
    
    def _compute_new_value(
        self,
        candidate: CandidateSignal,
        portfolio_state: Dict[str, any],
    ) -> float:
        """
        Compute value score for a new candidate.
        
        value_new = score - entry_cost_penalty - concentration_penalty - correlation_penalty
        """
        value = candidate.score
        
        # Entry cost penalty
        value -= self.entry_cost
        
        # Concentration penalty (computed later in context, but estimate here)
        # Will be refined in _select_winners
        
        return value
    
    def _compute_open_value(
        self,
        op_meta: OpenPositionMetadata,
        portfolio_state: Dict[str, any],
    ) -> float:
        """
        Compute value score for an open position.
        
        value_open = entry_score + pnl_bonus + trend_followthrough_bonus
                    - time_decay_penalty - concentration_penalty
                    - correlation_penalty - exit_cost_penalty
        """
        value = op_meta.entry_score
        
        # PnL bonus (convert R to score points, e.g., 1R = +5 points)
        pnl_bonus = float(op_meta.current_pnl_R) * 5.0
        value += pnl_bonus
        
        # Time decay (optional - small penalty for very old positions)
        # Skip for now
        
        # Exit cost penalty (because closing costs money)
        value -= self.exit_cost
        
        return value
    
    def _select_winners(
        self,
        contenders: List[Contender],
        portfolio_state: Dict[str, any],
    ) -> List[Contender]:
        """
        Select winners under constraints.
        
        Iterate sorted list and add to winners if:
        - len(WINNERS) < MAX_POSITIONS
        - Adding keeps within margin cap, per-cluster/per-symbol caps, exposure caps
        """
        winners = []
        margin_used = Decimal("0")
        cluster_counts: Dict[str, int] = {}
        symbol_counts: Dict[str, int] = {}
        net_long = Decimal("0")
        net_short = Decimal("0")
        available_margin = Decimal(str(portfolio_state.get("available_margin", 0)))
        max_margin = available_margin * Decimal(str(self.limits.max_margin_util))
        
        for contender in contenders:
            # Check position limit
            if len(winners) >= self.limits.max_positions:
                break
            
            # Check margin limit
            if margin_used + contender.required_margin > max_margin:
                continue
            
            # Check symbol cap (normalize symbols for matching spot vs futures)
            # Candidates use spot symbols (e.g., "PROMPT/USD"), positions use futures (e.g., "PF_PROMPTUSD")
            contender_normalized = _normalize_symbol_for_matching(contender.symbol)
            
            # Check if any existing winner has the same normalized symbol
            existing_count = 0
            for winner in winners:
                winner_normalized = _normalize_symbol_for_matching(winner.symbol)
                if winner_normalized == contender_normalized:
                    existing_count += 1
            
            if existing_count >= self.limits.max_per_symbol:
                continue
            
            # Check cluster cap
            cluster_count = cluster_counts.get(contender.cluster, 0)
            if cluster_count >= self.limits.max_per_cluster:
                # Apply concentration penalty (soft reject)
                # For now, hard reject if at cap
                continue
            
            # Check net exposure caps (if configured)
            if self.limits.max_net_long is not None:
                if contender.direction == Side.LONG:
                    if net_long + contender.required_margin > self.limits.max_net_long:
                        continue
            if self.limits.max_net_short is not None:
                if contender.direction == Side.SHORT:
                    if net_short + contender.required_margin > self.limits.max_net_short:
                        continue
            
            # All checks passed - add to winners
            winners.append(contender)
            margin_used += contender.required_margin
            cluster_counts[contender.cluster] = cluster_count + 1
            # Track by normalized symbol for proper matching
            symbol_counts[contender_normalized] = symbol_counts.get(contender_normalized, 0) + 1
            
            if contender.direction == Side.LONG:
                net_long += contender.required_margin
            else:
                net_short += contender.required_margin
        
        return winners
    
    def _apply_hysteresis(
        self,
        to_close: List[OpenPositionMetadata],
        to_open: List[Contender],
        to_keep: List[OpenPositionMetadata],
        portfolio_state: Dict[str, any],
    ) -> Tuple[List[OpenPositionMetadata], List[Contender]]:
        """
        Apply hysteresis swap rule to prevent churn.
        
        For each open in to_close, find the new that is "taking its slot".
        Only close if value_new >= value_open + SWAP_THRESHOLD.
        If not, cancel that swap: keep the open, drop that new.
        """
        final_closes = []
        final_opens = []
        
        # Build lookup maps
        close_by_symbol = {op.position.symbol: op for op in to_close}
        open_by_symbol = {op.position.symbol: op for op in to_keep}
        
        # Match closes to opens by cluster (or globally if no cluster match)
        for close_op in to_close:
            # Skip if locked
            if close_op.locked:
                logger.debug(
                    "Skipping locked position",
                    symbol=close_op.position.symbol,
                    reason="locked"
                )
                continue
            
            # Find best matching new candidate in same cluster
            matching_new = None
            for new_contender in to_open:
                if new_contender.cluster == close_op.cluster:
                    if matching_new is None or new_contender.value > matching_new.value:
                        matching_new = new_contender
            
            # If no cluster match, find globally best
            if matching_new is None and to_open:
                matching_new = max(to_open, key=lambda c: c.value)
            
            # Check swap threshold
            if matching_new:
                # Recompute values for accurate comparison (use same portfolio_state)
                close_value = self._compute_open_value(close_op, portfolio_state)
                # For new, we already computed value, but ensure we're comparing apples to apples
                new_value = matching_new.value
                
                if new_value >= close_value + self.swap_threshold:
                    # Swap approved
                    final_closes.append(close_op)
                    if matching_new not in final_opens:
                        final_opens.append(matching_new)
                    logger.debug(
                        "Swap approved",
                        close_symbol=close_op.position.symbol,
                        open_symbol=matching_new.symbol,
                        close_value=close_value,
                        new_value=new_value,
                        threshold=self.swap_threshold,
                    )
                else:
                    # Swap rejected - keep the open
                    logger.debug(
                        "Swap rejected (hysteresis)",
                        close_symbol=close_op.position.symbol,
                        open_symbol=matching_new.symbol if matching_new else None,
                        close_value=close_value,
                        new_value=new_value if matching_new else None,
                        threshold=self.swap_threshold,
                        gap=new_value - close_value if matching_new else None,
                    )
            else:
                # No replacement candidate - close is fine (position not in winners)
                final_closes.append(close_op)
        
        # Add remaining opens that weren't matched to closes
        for new_contender in to_open:
            if new_contender not in final_opens:
                final_opens.append(new_contender)
        
        return final_closes, final_opens


def derive_cluster(signal: Signal) -> str:
    """
    Derive cluster identifier from signal.
    
    Cluster = regime + setup_type (e.g., "tight_smc_ob", "wide_structure_bos")
    """
    regime = signal.regime  # "tight_smc" or "wide_structure"
    setup = signal.setup_type.value if hasattr(signal.setup_type, 'value') else str(signal.setup_type)
    return f"{regime}_{setup}"


def create_candidate_signal(
    signal: Signal,
    required_margin: Decimal,
    risk_R: Decimal,
    position_notional: Decimal,
) -> CandidateSignal:
    """Create a CandidateSignal from a Signal."""
    direction = Side.LONG if signal.signal_type.value == "long" else Side.SHORT
    cluster = derive_cluster(signal)
    
    return CandidateSignal(
        signal=signal,
        score=signal.score,
        direction=direction,
        symbol=signal.symbol,
        cluster=cluster,
        required_margin=required_margin,
        risk_R=risk_R,
        position_notional=position_notional,
    )


def position_to_open_metadata(
    position: Position,
    account_equity: Decimal,
    is_protective_orders_live: bool = True,
) -> OpenPositionMetadata:
    """
    Convert a Position to OpenPositionMetadata for auction evaluation.
    
    Args:
        position: The position object
        account_equity: Current account equity (for R calculation)
        is_protective_orders_live: Whether stop/tp orders are active
    
    Returns:
        OpenPositionMetadata with computed values
    """
    now = datetime.now(timezone.utc)
    age_seconds = (now - position.opened_at).total_seconds()
    
    # Calculate current PnL in R multiples
    # R = risk at entry = position_notional * initial_stop_distance_pct
    if position.initial_stop_distance_pct and position.initial_stop_distance_pct > 0:
        risk_R = position.size_notional * position.initial_stop_distance_pct
        if risk_R > 0:
            current_pnl_R = position.unrealized_pnl / risk_R
        else:
            current_pnl_R = Decimal("0")
    else:
        # Fallback: estimate R from current stop distance
        if position.initial_stop_price and position.entry_price:
            stop_distance_pct = abs(position.entry_price - position.initial_stop_price) / position.entry_price
            risk_R = position.size_notional * stop_distance_pct
            if risk_R > 0:
                current_pnl_R = position.unrealized_pnl / risk_R
            else:
                current_pnl_R = Decimal("0")
        else:
            current_pnl_R = Decimal("0")
    
    # Derive cluster if not stored
    cluster = position.cluster
    if not cluster and position.setup_type and position.regime:
        cluster = f"{position.regime}_{position.setup_type}"
    elif not cluster:
        cluster = "unknown"
    
    # Get entry score (default to 0 if not stored)
    entry_score = position.entry_score if position.entry_score is not None else 0.0
    
    return OpenPositionMetadata(
        position=position,
        entry_time=position.opened_at,
        entry_score=entry_score,
        current_pnl_R=current_pnl_R,
        margin_used=position.margin_used,
        cluster=cluster,
        direction=position.side,
        age_seconds=age_seconds,
        is_protective_orders_live=is_protective_orders_live,
    )
