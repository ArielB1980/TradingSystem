"""
Kill switch for emergency trading halt.

Provides emergency mechanism to:
- Stop all new signal processing
- Cancel all pending orders
- Close all open positions
- Persist state across restarts
"""
from datetime import datetime, timezone
from typing import Optional
from src.monitoring.logger import get_logger

logger = get_logger(__name__)


class KillSwitch:
    """
    Emergency kill switch for trading system.
    
    When activated:
    - Blocks all new trades
    - Cancels pending orders
    - Closes open positions
    - State persists across restarts
    """
    
    def __init__(self):
        """Initialize kill switch."""
        self._active = False
        self._activated_at: Optional[datetime] = None
        self._activated_by: str = "unknown"
        self._reason: str = ""
        
        # Load persisted state
        self._load_state()
    
    def activate(self, reason: str = "Manual activation", activated_by: str = "user") -> None:
        """
        Activate kill switch.
        
        Args:
            reason: Reason for activation
            activated_by: Who/what activated it
        """
        if self._active:
            logger.warning("Kill switch already active")
            return
        
        self._active = True
        self._activated_at = datetime.now(timezone.utc)
        self._activated_by = activated_by
        self._reason = reason
        
        # Persist state
        self._save_state()
        
        logger.critical(
            "🚨 KILL SWITCH ACTIVATED",
            reason=reason,
            activated_by=activated_by,
            timestamp=self._activated_at.isoformat()
        )
    
    def deactivate(self, deactivated_by: str = "user") -> None:
        """
        Deactivate kill switch.
        
        Args:
            deactivated_by: Who deactivated it
        """
        if not self._active:
            logger.warning("Kill switch already inactive")
            return
        
        duration = (datetime.now(timezone.utc) - self._activated_at).total_seconds() if self._activated_at else 0
        
        self._active = False
        
        # Persist state
        self._save_state()
        
        logger.critical(
            "✅ KILL SWITCH DEACTIVATED",
            deactivated_by=deactivated_by,
            was_active_for_seconds=duration
        )
        
        # Clear activation metadata
        self._activated_at = None
        self._activated_by = "unknown"
        self._reason = ""
    
    def is_active(self) -> bool:
        """Check if kill switch is active."""
        return self._active
    
    def get_status(self) -> dict:
        """
        Get kill switch status.
        
        Returns:
            Dict with status information
        """
        return {
            "active": self._active,
            "activated_at": self._activated_at.isoformat() if self._activated_at else None,
            "activated_by": self._activated_by,
            "reason": self._reason,
            "duration_seconds": (datetime.now(timezone.utc) - self._activated_at).total_seconds() 
                               if self._activated_at else 0
        }
    
    def _save_state(self) -> None:
        """Persist kill switch state to file."""
        try:
            import json
            state = {
                "active": self._active,
                "activated_at": self._activated_at.isoformat() if self._activated_at else None,
                "activated_by": self._activated_by,
                "reason": self._reason
            }
            
            with open(".kill_switch_state", "w") as f:
                json.dump(state, f)
                
        except Exception as e:
            logger.error("Failed to save kill switch state", error=str(e))
    
    def _load_state(self) -> None:
        """Load persisted kill switch state."""
        try:
            import json
            import os
            
            if not os.path.exists(".kill_switch_state"):
                return
            
            with open(".kill_switch_state", "r") as f:
                state = json.load(f)
            
            self._active = state.get("active", False)
            self._activated_by = state.get("activated_by", "unknown")
            self._reason = state.get("reason", "")
            
            activated_at_str = state.get("activated_at")
            if activated_at_str:
                self._activated_at = datetime.fromisoformat(activated_at_str)
            
            if self._active:
                logger.warning(
                    "Kill switch was active on startup",
                    activated_at=activated_at_str,
                    reason=self._reason
                )
                
        except Exception as e:
            logger.error("Failed to load kill switch state", error=str(e))


# Global instance
_kill_switch = KillSwitch()


def get_kill_switch() -> KillSwitch:
    """Get global kill switch instance."""
    return _kill_switch
