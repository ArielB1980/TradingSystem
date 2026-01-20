"""
Spot-perp basis monitoring and enforcement.

Implements:
- Pre-entry basis guard
- Post-entry basis risk handling
- Divergence tracking
"""
from decimal import Decimal
from datetime import datetime, timezone
from typing import Dict, Optional
from src.config.config import RiskConfig
from src.monitoring.logger import get_logger

logger = get_logger(__name__)


class BasisGuard:
    """
    Spot-perpetual basis monitoring and enforcement.
    
    Design: Basis guards are mandatory for all entries.
    """
    
    def __init__(self, config: RiskConfig):
        """
        Initialize basis guard.
        
        Args:
            config: Risk configuration
        """
        self.config = config
        
        # Track basis state per symbol
        self.basis_risk_state: Dict[str, bool] = {}  # symbol -> in_risk_state
        
        logger.info(
            "Basis Guard initialized",
            basis_max=self.config.basis_max_pct,
            basis_max_post=self.config.basis_max_post_pct,
        )
    
    def check_pre_entry(
        self,
        spot_price: Decimal,
        perp_mark_price: Decimal,
        symbol: str,
    ) -> tuple[bool, Decimal, Optional[str]]:
        """
        Check pre-entry basis guard.
        
        Args:
            spot_price: Current spot price
            perp_mark_price: Current perpetual mark price
            symbol: Symbol being traded
        
        Returns:
            (approved, divergence_pct, rejection_reason)
        """
        if spot_price <= 0:
            logger.error(
                "Invalid spot price in pre-entry basis check",
                symbol=symbol,
                spot_price=str(spot_price)
            )
            return False, Decimal("0"), "Invalid spot price (zero or negative)"

        divergence_pct = abs(spot_price - perp_mark_price) / spot_price
        max_basis = Decimal(str(self.config.basis_max_pct))
        
        if divergence_pct > max_basis:
            reason = (
                f"Spot-perp divergence {divergence_pct:.2%} > max {max_basis:.2%}"
            )
            logger.warning(
                "Pre-entry basis guard REJECTED",
                symbol=symbol,
                spot=str(spot_price),
                perp=str(perp_mark_price),
                divergence=f"{divergence_pct:.2%}",
            )
            return False, divergence_pct, reason
        
        logger.debug(
            "Pre-entry basis guard PASSED",
            symbol=symbol,
            divergence=f"{divergence_pct:.2%}",
        )
        return True, divergence_pct, None
    
    def check_post_entry(
        self,
        spot_price: Decimal,
        perp_mark_price: Decimal,
        symbol: str,
    ) -> tuple[bool, Decimal]:
        """
        Check post-entry basis risk.
        
        If basis widens beyond threshold, disallow pyramiding but keep
        existing protective orders active.
        
        Args:
            spot_price: Current spot price
            perp_mark_price: Current perpetual mark price
            symbol: Symbol being monitored
        
        Returns:
            (allow_pyramiding, divergence_pct)
        """
        if spot_price <= 0:
            logger.error(
                "Invalid spot price in post-entry basis check",
                symbol=symbol,
                spot_price=str(spot_price)
            )
            return False, Decimal("0")

        divergence_pct = abs(spot_price - perp_mark_price) / spot_price
        max_basis_post = Decimal(str(self.config.basis_max_post_pct))
        
        if divergence_pct > max_basis_post:
            if not self.basis_risk_state.get(symbol, False):
                logger.warning(
                    "Post-entry basis risk DETECTED",
                    symbol=symbol,
                    divergence=f"{divergence_pct:.2%}",
                    max_allowed=f"{max_basis_post:.2%}",
                )
                self.basis_risk_state[symbol] = True
            
            return False, divergence_pct
        else:
            if self.basis_risk_state.get(symbol, False):
                logger.info(
                    "Post-entry basis risk CLEARED",
                    symbol=symbol,
                    divergence=f"{divergence_pct:.2%}",
                )
                self.basis_risk_state[symbol] = False
            
            return True, divergence_pct
    
    def is_in_basis_risk_state(self, symbol: str) -> bool:
        """Check if symbol is currently in basis risk state."""
        return self.basis_risk_state.get(symbol, False)
