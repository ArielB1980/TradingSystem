"""
Alert system for critical trading events.

Sends notifications for:
- Position size violations
- Unusual PnL swings
- System errors
- Kill switch activation
"""
import json
import os
import urllib.request
import urllib.error
from decimal import Decimal
from typing import Optional
from datetime import datetime, timezone
from src.exceptions import OperationalError, DataError
from src.monitoring.logger import get_logger

logger = get_logger(__name__)


class AlertLevel:
    """Alert severity levels."""
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


def _send_slack_webhook(url: str, level: str, title: str, message: str, metadata: Optional[dict] = None) -> None:
    """POST to Slack incoming webhook."""
    try:
        payload = {
            "text": f"[{level.upper()}] {title}",
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*\n{message}"}},
            ],
        }
        if metadata:
            payload["blocks"].append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": json.dumps(metadata)[:2000]}],
            })
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as _:
            pass
    except (OperationalError, DataError, OSError, ConnectionError) as e:
        logger.warning("Slack webhook failed", error=str(e), error_type=type(e).__name__)


def _send_discord_webhook(url: str, level: str, title: str, message: str, metadata: Optional[dict] = None) -> None:
    """POST to Discord webhook."""
    try:
        color = {"info": 0x3498DB, "warning": 0xF39C12, "critical": 0xE74C3C}.get(level, 0x95A5A6)
        embed = {
            "title": title,
            "description": message[:4000],
            "color": color,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if metadata:
            embed["fields"] = [{"name": k, "value": str(v)[:1024], "inline": False} for k, v in list(metadata.items())[:5]]
        payload = {"embeds": [embed]}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as _:
            pass
    except (OperationalError, DataError, OSError, ConnectionError) as e:
        logger.warning("Discord webhook failed", error=str(e), error_type=type(e).__name__)


class AlertSystem:
    """
    Alert system for critical events.

    Logs all alerts. Also supports Slack and Discord webhooks when configured.
    """

    def __init__(self, config: Optional[dict] = None):
        self.config = config or {}
        self.enabled = self.config.get("enabled", True)
        self.max_position_size_usd = self.config.get("max_position_size_usd", 10000)
        self.max_daily_loss_pct = self.config.get("max_daily_loss_pct", 5.0)
        self.max_single_loss_pct = self.config.get("max_single_loss_pct", 2.0)
        self.alert_methods = self.config.get("alert_methods", ["log"])
        self.slack_webhook_url = self.config.get("slack_webhook_url") or os.getenv("SLACK_WEBHOOK_URL")
        self.discord_webhook_url = self.config.get("discord_webhook_url") or os.getenv("DISCORD_WEBHOOK_URL")
        logger.info(
            "Alert system initialized",
            enabled=self.enabled,
            methods=self.alert_methods,
            slack=bool(self.slack_webhook_url),
            discord=bool(self.discord_webhook_url),
        )

    def send_alert(
        self,
        level: str,
        title: str,
        message: str,
        metadata: Optional[dict] = None
    ) -> None:
        if not self.enabled:
            return
        log_method = {
            AlertLevel.INFO: logger.info,
            AlertLevel.WARNING: logger.warning,
            AlertLevel.CRITICAL: logger.critical,
        }.get(level, logger.info)
        log_method(f"🔔 ALERT: {title}", message=message, level=level, metadata=metadata or {})
        md = metadata or {}
        if "slack" in self.alert_methods and self.slack_webhook_url:
            _send_slack_webhook(self.slack_webhook_url, level, title, message, md)
        if "discord" in self.alert_methods and self.discord_webhook_url:
            _send_discord_webhook(self.discord_webhook_url, level, title, message, md)
    
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


_alert_system: Optional[AlertSystem] = None


def get_alert_system(config: Optional[dict] = None) -> AlertSystem:
    """Get global alert system instance. If config is None, loads from Config.monitoring."""
    global _alert_system
    if _alert_system is None:
        if config is None:
            try:
                from src.config.config import load_config
                cfg = load_config()
                config = {
                    "enabled": True,
                    "alert_methods": list(getattr(cfg.monitoring, "alert_methods", ["log"])),
                    "slack_webhook_url": getattr(cfg.monitoring, "slack_webhook_url", None),
                    "discord_webhook_url": getattr(cfg.monitoring, "discord_webhook_url", None),
                    "max_position_size_usd": getattr(cfg.risk, "max_position_size_usd", 10000),
                    "max_daily_loss_pct": getattr(cfg.risk, "daily_loss_limit_pct", 5.0) * 100,
                }
            except (ImportError, OSError, ValueError, TypeError, KeyError):
                config = {}
        _alert_system = AlertSystem(config)
    return _alert_system
