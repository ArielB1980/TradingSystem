"""
Lightweight alerting module for critical trading events.

Sends notifications via webhook (Telegram or Discord).
Configure via environment variables:
  ALERT_WEBHOOK_URL  - Telegram bot URL or Discord webhook URL
  ALERT_CHAT_ID      - Telegram chat ID (required for Telegram, ignored for Discord)

If no webhook is configured, alerts are logged but not sent.
"""
import os
import asyncio
from datetime import datetime, timezone
from typing import Optional
import aiohttp

from src.monitoring.logger import get_logger

logger = get_logger(__name__)

# Rate limit: max 1 alert per event type per 5 minutes
_last_alert_times: dict[str, datetime] = {}
_RATE_LIMIT_SECONDS = 300


def _is_telegram(url: str) -> bool:
    return "api.telegram.org" in url


def _is_discord(url: str) -> bool:
    return "discord.com/api/webhooks" in url or "discordapp.com/api/webhooks" in url


async def send_alert(event_type: str, message: str, urgent: bool = False) -> None:
    """
    Send an alert notification.
    
    Args:
        event_type: Type of event (e.g., "KILL_SWITCH", "HALT", "NEW_POSITION")
        message: Human-readable message
        urgent: If True, bypass rate limiting
    """
    webhook_url = os.environ.get("ALERT_WEBHOOK_URL", "").strip()
    chat_id = os.environ.get("ALERT_CHAT_ID", "").strip()
    
    if not webhook_url:
        # No webhook configured — log only
        logger.info("Alert (no webhook configured)", event_type=event_type, message=message)
        return
    
    # Rate limiting (unless urgent)
    now = datetime.now(timezone.utc)
    if not urgent:
        last = _last_alert_times.get(event_type)
        if last and (now - last).total_seconds() < _RATE_LIMIT_SECONDS:
            return  # Rate limited, skip
    
    _last_alert_times[event_type] = now
    
    # Format message
    timestamp = now.strftime("%H:%M:%S UTC")
    prefix = "🚨" if urgent else "📊"
    formatted = f"{prefix} [{event_type}] {timestamp}\n{message}"
    
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            if _is_telegram(webhook_url):
                payload = {
                    "chat_id": chat_id,
                    "text": formatted,
                    "parse_mode": "HTML",
                }
                async with session.post(webhook_url, json=payload) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning("Telegram alert failed", status=resp.status, body=body[:200])
            elif _is_discord(webhook_url):
                payload = {"content": formatted}
                async with session.post(webhook_url, json=payload) as resp:
                    if resp.status not in (200, 204):
                        body = await resp.text()
                        logger.warning("Discord alert failed", status=resp.status, body=body[:200])
            else:
                # Generic webhook — POST JSON
                payload = {
                    "event_type": event_type,
                    "message": message,
                    "timestamp": now.isoformat(),
                    "urgent": urgent,
                }
                async with session.post(webhook_url, json=payload) as resp:
                    if resp.status >= 400:
                        logger.warning("Webhook alert failed", status=resp.status)
    except Exception as e:
        # Alert failures must never crash the trading system
        logger.warning("Alert send failed (non-fatal)", event_type=event_type, error=str(e))


def send_alert_sync(event_type: str, message: str, urgent: bool = False) -> None:
    """Synchronous wrapper for send_alert (for use outside async context)."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(send_alert(event_type, message, urgent))
    except RuntimeError:
        # No running loop — run directly
        asyncio.run(send_alert(event_type, message, urgent))
