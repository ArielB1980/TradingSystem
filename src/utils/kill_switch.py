"""
Kill switch with latching emergency stop.

Once triggered, system cannot auto-resume - manual acknowledgment required.
"""
from enum import Enum
from datetime import datetime, timezone
from typing import Optional
from src.monitoring.logger import get_logger

logger = get_logger(__name__)


class KillSwitchReason(str, Enum):
    """Reasons for kill switch activation."""
    MANUAL = "manual"
    API_ERROR = "api_error"
    MARGIN_CRITICAL = "margin_critical"
    LIQUIDATION_BREACH = "liquidation_breach"
    DATA_FAILURE = "data_failure"
    RECONCILIATION_FAILURE = "reconciliation_failure"


class KillSwitch:
    """
    Latched emergency kill switch.
    
    Design: Once activated, requires manual acknowledgment to restart.
    Prevents oscillation.
    """
    
    def __init__(self):
        """Initialize kill switch."""
        self.active = False
        self.latched = False
        self.reason: Optional[KillSwitchReason] = None
        self.activated_at: Optional[datetime] = None
        
        logger.info("Kill Switch initialized")
    
    def activate(self, reason: KillSwitchReason, emergency: bool = False):
        """
        Activate kill switch.
        
        Args:
            reason: Reason for activation
            emergency: If True, triggers emergency mode (flatten all positions)
        """
        if not self.active:
            self.active = True
            self.latched = True
            self.reason = reason
            self.activated_at = datetime.now(timezone.utc)
            
            logger.critical(
                "🛑 KILL SWITCH ACTIVATED",
                reason=reason.value,
                emergency=emergency,
                timestamp=self.activated_at.isoformat(),
            )
            
            # TODO: Implement actual actions:
            # 1. Cancel all open orders
            # 2. If emergency: flatten all positions (market orders)
            # 3. Halt all trading loops
            
            logger.critical(
                "Manual acknowledgment required to restart trading"
            )
    
    def acknowledge(self) -> bool:
        """
        Manually acknowledge kill switch to allow restart.
        
        Returns:
            True if acknowledged successfully
        """
        if not self.latched:
            logger.warning("Kill switch not latched, nothing to acknowledge")
            return False
        
        logger.info(
            "Kill switch acknowledged",
            reason=self.reason.value if self.reason else "unknown",
            activated_at=self.activated_at.isoformat() if self.activated_at else "unknown",
        )
        
        # Reset state
        self.active = False
        self.latched = False
        self.reason = None
        self.activated_at = None
        
        return True
    
    def is_active(self) -> bool:
        """Check if kill switch is active."""
        return self.active
    
    def is_latched(self) -> bool:
        """Check if kill switch is latched (requires manual ack)."""
        return self.latched
