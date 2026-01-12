"""
Alert system for critical trading events.

Sends notifications for:
- Position size violations
- Unusual PnL swings
- System errors
- Kill switch activation
"""
from decimal import Decimal
from typing import Optional
from datetime import datetime, timezone
from src.monitoring.logger import get_logger

logger = get_logger(__name__)


class AlertLevel:
    """Alert severity levels."""
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertSystem:
    """
    Alert system for critical events.
    
    Currently logs alerts. Can be extended to send:
    - Email via SMTP
    - SMS via Twilio
    - Slack/Discord webhooks
    - Push notifications
    """
    
    def __init__(self, config: Optional[dict] = None):
        """
        Initialize alert system.
        
        Args:
            config: Alert configuration (email, SMS, etc.)
        """
        self.config = config or {}
        self.enabled = self.config.get("enabled", True)
        
        # Thresholds
        self.max_position_size_usd = self.config.get("max_position_size_usd", 10000)
        self.max_daily_loss_pct = self.config.get("max_daily_loss_pct", 5.0)
        self.max_single_loss_pct = self.config.get("max_single_loss_pct", 2.0)
        
        logger.info("Alert system initialized", enabled=self.enabled)
    
    def send_alert(
        self,
        level: str,
        title: str,
        message: str,
        metadata: Optional[dict] = None
    ) -> None:
        """
        Send alert notification.
        
        Args:
            level: Alert level (info, warning, critical)
            title: Alert title
            message: Alert message
            metadata: Additional context
        """
        if not self.enabled:
            return
        
        # Log alert
        log_method = {
            AlertLevel.INFO: logger.info,
            AlertLevel.WARNING: logger.warning,
            AlertLevel.CRITICAL: logger.critical,
        }.get(level, logger.info)
        
        log_method(
            f"🔔 ALERT: {title}",
            message=message,
            level=level,
            metadata=metadata or {}
        )
        
        # TODO: Implement actual notification channels
        # - Email via SMTP
        # - SMS via Twilio
        # - Slack webhook
        # - Discord webhook
        # - Push notification
    
    def check_position_size_violation(
        self,
        symbol: str,
        size_notional: Decimal
    ) -> None:
        """
        Check if position size exceeds limits.
        
        Args:
            symbol: Trading symbol
            size_notional: Position size in USD
        """
        if float(size_notional) > self.max_position_size_usd:
            self.send_alert(
                AlertLevel.CRITICAL,
                "Position Size Violation",
                f"{symbol} position size ${size_notional:,.2f} exceeds limit ${self.max_position_size_usd:,.2f}",
                metadata={
                    "symbol": symbol,
                    "size": float(size_notional),
                    "limit": self.max_position_size_usd
                }
            )
    
    def check_daily_loss_violation(
        self,
        daily_pnl: Decimal,
        equity: Decimal
    ) -> None:
        """
        Check if daily loss exceeds threshold.
        
        Args:
            daily_pnl: Daily PnL
            equity: Current equity
        """
        if equity == 0:
            return
        
        daily_loss_pct = abs(float(daily_pnl / equity * 100))
        
        if daily_pnl < 0 and daily_loss_pct > self.max_daily_loss_pct:
            self.send_alert(
                AlertLevel.CRITICAL,
                "Daily Loss Limit Exceeded",
                f"Daily loss {daily_loss_pct:.2f}% exceeds limit {self.max_daily_loss_pct}%",
                metadata={
                    "daily_pnl": float(daily_pnl),
                    "daily_loss_pct": daily_loss_pct,
                    "limit": self.max_daily_loss_pct
                }
            )
    
    def check_single_trade_loss(
        self,
        symbol: str,
        pnl: Decimal,
        equity: Decimal
    ) -> None:
        """
        Check if single trade loss is unusual.
        
        Args:
            symbol: Trading symbol
            pnl: Trade PnL
            equity: Current equity
        """
        if equity == 0:
            return
        
        loss_pct = abs(float(pnl / equity * 100))
        
        if pnl < 0 and loss_pct > self.max_single_loss_pct:
            self.send_alert(
                AlertLevel.WARNING,
                "Large Single Trade Loss",
                f"{symbol} loss {loss_pct:.2f}% exceeds threshold {self.max_single_loss_pct}%",
                metadata={
                    "symbol": symbol,
                    "pnl": float(pnl),
                    "loss_pct": loss_pct,
                    "limit": self.max_single_loss_pct
                }
            )
    
    def alert_kill_switch_activated(self, reason: str) -> None:
        """Alert when kill switch is activated."""
        self.send_alert(
            AlertLevel.CRITICAL,
            "🚨 KILL SWITCH ACTIVATED",
            f"Trading halted: {reason}",
            metadata={"reason": reason}
        )
    
    def alert_system_error(self, error: str, context: Optional[dict] = None) -> None:
        """Alert on system errors."""
        self.send_alert(
            AlertLevel.CRITICAL,
            "System Error",
            error,
            metadata=context
        )


# Global instance
_alert_system = None


def get_alert_system(config: Optional[dict] = None) -> AlertSystem:
    """Get global alert system instance."""
    global _alert_system
    if _alert_system is None:
        _alert_system = AlertSystem(config)
    return _alert_system
