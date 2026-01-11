"""
Signal quality scoring system for V2.

Scores each signal on multiple factors to prioritize opportunities.
Used for dashboard display and future trade selection optimization.
"""
from typing import Dict, Optional
from decimal import Decimal
from dataclasses import dataclass

from src.domain.models import Signal, SignalType
from src.strategy.fibonacci_engine import FibonacciLevels
from src.monitoring.logger import get_logger

logger = get_logger(__name__)


@dataclass
class SignalScore:
    """Composite quality score for a signal with breakdown."""
    total_score: float  # 0-100
    smc_quality: float  # 0-25
    fib_confluence: float  # 0-20
    htf_alignment: float  # 0-20
    adx_strength: float  # 0-15
    cost_efficiency: float  # 0-20
    
    def get_grade(self) -> str:
        """Convert score to letter grade."""
        if self.total_score >= 80:
            return "A"
        elif self.total_score >= 65:
            return "B"
        elif self.total_score >= 50:
            return "C"
        elif self.total_score >= 35:
            return "D"
        else:
            return "F"


class SignalScorer:
    """
    Scores trading signals on multiple quality factors.
    
    Philosophy:
    - High scores = better confluence, structure, and efficiency
    - Does NOT block trades (risk manager does that)
    - Used for prioritization and dashboard display
    """
    
    def __init__(self):
        """Initialize signal scorer."""
        logger.info("SignalScorer initialized")
    
    def score_signal(
        self,
        signal: Signal,
        structures: Dict,
        fib_levels: Optional[FibonacciLevels],
        adx: float,
        cost_bps: Decimal
    ) -> SignalScore:
        """
        Calculate composite quality score for a signal.
        
        Args:
            signal: Generated signal
            structures: SMC structures dict (OB, FVG, BOS)
            fib_levels: Fibonacci levels (if available)
            adx: ADX value for trend strength
            cost_bps: Estimated cost in basis points
        
        Returns:
            SignalScore with total and component scores
        """
        # Score each component
        smc_score = self._score_smc_quality(structures)
        fib_score = self._score_fib_confluence(signal, fib_levels)
        htf_score = self._score_htf_alignment(signal)
        adx_score = self._score_adx_strength(adx)
        cost_score = self._score_cost_efficiency(signal, cost_bps)
        
        total = smc_score + fib_score + htf_score + adx_score + cost_score
        
        score = SignalScore(
            total_score=total,
            smc_quality=smc_score,
            fib_confluence=fib_score,
            htf_alignment=htf_score,
            adx_strength=adx_score,
            cost_efficiency=cost_score
        )
        
        logger.debug(
            "Signal scored",
            symbol=signal.symbol,
            total=f"{total:.1f}",
            grade=score.get_grade(),
            breakdown={
                "smc": f"{smc_score:.1f}",
                "fib": f"{fib_score:.1f}",
                "htf": f"{htf_score:.1f}",
                "adx": f"{adx_score:.1f}",
                "cost": f"{cost_score:.1f}"
            }
        )
        
        return score
    
    def _score_smc_quality(self, structures: Dict) -> float:
        """
        Score SMC structure quality (0-25 points).
        
        Scoring:
        - Order Block present: +10
        - FVG present: +8
        - BOS confirmed: +7
        - Max: 25 (all structures)
        """
        score = 0.0
        
        if structures.get("order_block"):
            score += 10.0
        
        if structures.get("fvg"):
            score += 8.0
        
        if structures.get("bos"):
            score += 7.0
        
        return min(score, 25.0)
    
    def _score_fib_confluence(
        self,
        signal: Signal,
        fib_levels: Optional[FibonacciLevels]
    ) -> float:
        """
        Score Fibonacci confluence (0-20 points).
        
        Scoring:
        - In OTE zone: +15
        - Near any fib level (0.382, 0.618, etc): +10
        - Near extension: +5
        - No fib data: 0
        """
        if not fib_levels:
            return 0.0
        
        score = 0.0
        entry = signal.entry_price
        
        # Check OTE zone (highest value)
        if fib_levels.ote_low <= entry <= fib_levels.ote_high:
            score = 15.0
        else:
            # Check proximity to standard levels
            tolerance = Decimal("0.002")  # 0.2%
            
            levels = [
                fib_levels.fib_0_382,
                fib_levels.fib_0_618,
                fib_levels.fib_0_500,
                fib_levels.fib_0_786
            ]
            
            for level in levels:
                if abs(entry - level) / level <= tolerance:
                    score = 10.0
                    break
            
            # Check extensions if no retracement match
            if score == 0:
                ext_levels = [fib_levels.fib_1_272, fib_levels.fib_1_618]
                for level in ext_levels:
                    if abs(entry - level) / level <= tolerance:
                        score = 5.0
                        break
        
        return score
    
    def _score_htf_alignment(self, signal: Signal) -> float:
        """
        Score HTF alignment (0-20 points).
        
        Placeholder: In full implementation, would check:
        - 4H trend direction
        - 1D trend direction
        - Alignment with signal direction
        
        For now: award points based on signal type presence
        """
        # Simple scoring based on setup type
        if hasattr(signal, 'setup_type'):
            from src.domain.models import SetupType
            if signal.setup_type in [SetupType.OB, SetupType.FVG]:
                return 15.0  # Tight-stop setups get bonus
            elif signal.setup_type == SetupType.BOS:
                return 20.0  # BOS implies strong HTF alignment
            else:
                return 10.0  # TREND
        
        return 10.0  # Default moderate score
    
    def _score_adx_strength(self, adx: float) -> float:
        """
        Score ADX trend strength (0-15 points).
        
        Scoring:
        - ADX >= 40: 15 (very strong trend)
        - ADX >= 30: 12
        - ADX >= 25: 10
        - ADX >= 20: 7
        - ADX < 20: 3 (weak trend)
        """
        if adx >= 40:
            return 15.0
        elif adx >= 30:
            return 12.0
        elif adx >= 25:
            return 10.0
        elif adx >= 20:
            return 7.0
        else:
            return 3.0
    
    def _score_cost_efficiency(self, signal: Signal, cost_bps: Decimal) -> float:
        """
        Score cost efficiency (0-20 points).
        
        Lower cost relative to potential reward = higher score.
        
        Scoring:
        - Cost <= 10 bps: 20
        - Cost <= 20 bps: 15
        - Cost <= 30 bps: 10
        - Cost <= 50 bps: 5
        - Cost > 50 bps: 0
        """
        if cost_bps <= Decimal("10"):
            return 20.0
        elif cost_bps <= Decimal("20"):
            return 15.0
        elif cost_bps <= Decimal("30"):
            return 10.0
        elif cost_bps <= Decimal("50"):
            return 5.0
        else:
            return 0.0
