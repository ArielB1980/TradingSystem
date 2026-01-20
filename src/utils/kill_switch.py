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
    Prevents oscillation. State persists across restarts.
    """

    def __init__(self, client=None):
        """
        Initialize kill switch.

        Args:
            client: KrakenClient instance (optional, for executing actions)
        """
        self.active = False
        self.latched = False
        self.reason: Optional[KillSwitchReason] = None
        self.activated_at: Optional[datetime] = None
        self.client = client

        # Load persisted state
        self._load_state()

        logger.info("Kill Switch initialized")
    
    def set_client(self, client):
        """Set the KrakenClient instance."""
        self.client = client

    def activate_sync(self, reason: KillSwitchReason):
        """
        Synchronously activate kill switch (for CLI use).
        Does not execute cancel/close actions (requires async client).

        Args:
            reason: Reason for activation
        """
        if not self.active:
            self.active = True
            self.latched = True
            self.reason = reason
            self.activated_at = datetime.now(timezone.utc)

            # Persist state immediately
            self._save_state()

            logger.critical(
                "🛑 KILL SWITCH ACTIVATED (sync)",
                reason=reason.value,
                timestamp=self.activated_at.isoformat(),
            )

            logger.critical(
                "Manual acknowledgment required to restart trading"
            )

    async def activate(self, reason: KillSwitchReason, emergency: bool = False):
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
            
            # Persist state immediately
            self._save_state()

            logger.critical(
                "🛑 KILL SWITCH ACTIVATED",
                reason=reason.value,
                emergency=emergency,
                timestamp=self.activated_at.isoformat(),
            )

            if self.client:
                try:
                    # 1. Cancel all open orders
                    await self.client.cancel_all_orders()
                    logger.info("Kill switch: All orders cancelled")
                    
                    # 2. If emergency: flatten all positions
                    if emergency:
                         positions = await self.client.get_all_futures_positions()
                         for pos in positions:
                             symbol = pos['symbol']
                             try:
                                 await self.client.close_position(symbol)
                                 logger.warning(f"Kill switch: Emergency closed position for {symbol}")
                             except Exception as e:
                                 logger.error(f"Kill switch: Failed to close {symbol}", error=str(e))
                except Exception as e:
                    logger.critical("Kill switch action failed", error=str(e))
            else:
                 logger.critical("Kill switch: No client attached, cannot execute actions")

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

        # Persist deactivated state
        self._save_state()

        return True
    
    def is_active(self) -> bool:
        """Check if kill switch is active."""
        return self.active
    
    def is_latched(self) -> bool:
        """Check if kill switch is latched (requires manual ack)."""
        return self.latched

    def get_status(self) -> dict:
        """
        Get kill switch status.

        Returns:
            Dict with status information
        """
        return {
            "active": self.active,
            "latched": self.latched,
            "activated_at": self.activated_at.isoformat() if self.activated_at else None,
            "reason": self.reason.value if self.reason else None,
            "duration_seconds": (datetime.now(timezone.utc) - self.activated_at).total_seconds()
                               if self.activated_at else 0
        }

    def _save_state(self) -> None:
        """Persist kill switch state to file."""
        try:
            import json
            state = {
                "active": self.active,
                "latched": self.latched,
                "activated_at": self.activated_at.isoformat() if self.activated_at else None,
                "reason": self.reason.value if self.reason else None
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

            self.active = state.get("active", False)
            self.latched = state.get("latched", False)

            reason_str = state.get("reason")
            if reason_str:
                try:
                    self.reason = KillSwitchReason(reason_str)
                except ValueError:
                    self.reason = None

            activated_at_str = state.get("activated_at")
            if activated_at_str:
                self.activated_at = datetime.fromisoformat(activated_at_str)

            if self.active:
                logger.warning(
                    "Kill switch was active on startup",
                    activated_at=activated_at_str,
                    reason=self.reason.value if self.reason else "unknown"
                )

        except Exception as e:
            logger.error("Failed to load kill switch state", error=str(e))


# Global instance
_kill_switch = KillSwitch()


def get_kill_switch() -> KillSwitch:
    """Get global kill switch instance."""
    return _kill_switch
