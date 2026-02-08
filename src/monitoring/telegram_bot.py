"""
Telegram bot command handler for interactive status queries.

Runs as a background async task, polling for incoming commands.
Supports:
  /status  - Equity, margin, positions, system state
  /positions - Detailed open positions with P&L
  /help    - List available commands

Requires ALERT_WEBHOOK_URL (Telegram bot URL) and ALERT_CHAT_ID env vars.
"""
import os
import re
import asyncio
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional, Any, Callable, Awaitable

import aiohttp

from src.monitoring.logger import get_logger

logger = get_logger(__name__)


def _extract_bot_token(webhook_url: str) -> Optional[str]:
    """Extract bot token from Telegram webhook URL."""
    match = re.search(r"bot(\d+:[A-Za-z0-9_-]+)", webhook_url)
    return match.group(1) if match else None


class TelegramCommandHandler:
    """
    Polls Telegram for incoming commands and responds.
    
    Designed to be non-blocking and crash-safe — errors are logged,
    never propagated. The trading system must never be affected by
    command handling failures.
    """
    
    def __init__(self, data_provider: Callable[..., Awaitable[dict]]):
        """
        Args:
            data_provider: Async callable that returns system state dict with keys:
                equity, margin_used, margin_pct, positions (list of dicts),
                system_state, kill_switch_active, cycle_count, cooldowns_active,
                universe_size
        """
        self._data_provider = data_provider
        self._bot_token: Optional[str] = None
        self._chat_id: Optional[str] = None
        self._last_update_id: int = 0
        self._active = False
    
    def _init_config(self) -> bool:
        """Load config from env vars. Returns True if configured."""
        webhook_url = os.environ.get("ALERT_WEBHOOK_URL", "").strip()
        self._chat_id = os.environ.get("ALERT_CHAT_ID", "").strip()
        
        if not webhook_url or "api.telegram.org" not in webhook_url:
            return False
        
        self._bot_token = _extract_bot_token(webhook_url)
        return bool(self._bot_token and self._chat_id)
    
    async def run(self) -> None:
        """Main polling loop. Call as asyncio.create_task(handler.run())."""
        if not self._init_config():
            logger.info("Telegram command handler: not configured, skipping")
            return
        
        self._active = True
        logger.info("Telegram command handler started", chat_id=self._chat_id)
        
        while self._active:
            try:
                await self._poll_updates()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Telegram poll error (non-fatal)", error=str(e))
            
            await asyncio.sleep(5)  # Poll every 5 seconds
    
    def stop(self):
        """Stop the polling loop."""
        self._active = False
    
    async def _poll_updates(self) -> None:
        """Fetch new messages from Telegram."""
        url = f"https://api.telegram.org/bot{self._bot_token}/getUpdates"
        params = {"offset": self._last_update_id + 1, "timeout": 3}
        
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
        
        if not data.get("ok") or not data.get("result"):
            return
        
        for update in data["result"]:
            self._last_update_id = update["update_id"]
            
            message = update.get("message", {})
            chat_id = str(message.get("chat", {}).get("id", ""))
            text = (message.get("text") or "").strip().lower()
            
            # Only respond to our authorized chat
            if chat_id != self._chat_id:
                continue
            
            if text in ("/status", "/s"):
                await self._handle_status()
            elif text in ("/positions", "/pos", "/p"):
                await self._handle_positions()
            elif text in ("/trades", "/t"):
                await self._handle_trades()
            elif text in ("/help", "/start"):
                await self._handle_help()
            # Silently ignore unknown commands
    
    async def _send_message(self, text: str) -> None:
        """Send a message to the configured chat."""
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
        }
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.post(url, json=payload) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning("Telegram send failed", status=resp.status, body=body[:200])
        except Exception as e:
            logger.warning("Telegram send error", error=str(e))
    
    async def _handle_help(self) -> None:
        """Respond to /help."""
        await self._send_message(
            "🤖 <b>KBot Commands</b>\n\n"
            "/status - Equity, margin, system state\n"
            "/positions - Open positions with P&L\n"
            "/trades - Last 5 closed trades\n"
            "/help - This message"
        )
    
    async def _handle_status(self) -> None:
        """Respond to /status with system overview."""
        try:
            data = await self._data_provider()
        except Exception as e:
            await self._send_message(f"❌ Failed to fetch status: {e}")
            return
        
        equity = data.get("equity", Decimal("0"))
        margin_pct = data.get("margin_pct", 0)
        system_state = data.get("system_state", "UNKNOWN")
        kill_active = data.get("kill_switch_active", False)
        positions = data.get("positions", [])
        cycle = data.get("cycle_count", "?")
        cooldowns = data.get("cooldowns_active", 0)
        universe = data.get("universe_size", 0)
        
        state_emoji = "🟢" if system_state == "NORMAL" else "🔴" if kill_active else "🟡"
        
        now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        
        # Calculate total unrealized P&L
        total_upnl = sum(
            Decimal(str(p.get("unrealizedPnl", p.get("unrealized_pnl", 0))))
            for p in positions
        )
        upnl_sign = "+" if total_upnl >= 0 else ""
        
        msg = (
            f"📊 <b>KBot Status</b> ({now})\n\n"
            f"{state_emoji} State: <b>{system_state}</b>\n"
            f"💰 Equity: <b>${equity:.2f}</b>\n"
            f"📈 Margin: {margin_pct:.1f}%\n"
            f"💹 Unrealized P&L: {upnl_sign}${total_upnl:.2f}\n\n"
            f"📋 Positions: {len(positions)}\n"
            f"🌐 Universe: {universe} coins\n"
            f"🔄 Cycle: #{cycle}\n"
            f"⏳ Cooldowns: {cooldowns}"
        )
        
        await self._send_message(msg)
    
    async def _handle_positions(self) -> None:
        """Respond to /positions with detailed position list."""
        try:
            data = await self._data_provider()
        except Exception as e:
            await self._send_message(f"❌ Failed to fetch positions: {e}")
            return
        
        positions = data.get("positions", [])
        
        if not positions:
            await self._send_message("📋 No open positions")
            return
        
        lines = ["📋 <b>Open Positions</b>\n"]
        
        for p in positions:
            symbol = p.get("symbol", "?")
            side = p.get("side", "?").upper()
            size = Decimal(str(p.get("size", 0)))
            entry = Decimal(str(p.get("entryPrice", p.get("entry_price", 0))))
            mark = Decimal(str(p.get("markPrice", p.get("mark_price", 0))))
            upnl = Decimal(str(p.get("unrealizedPnl", p.get("unrealized_pnl", 0))))
            leverage = p.get("leverage", "?")
            
            upnl_sign = "+" if upnl >= 0 else ""
            pnl_emoji = "🟢" if upnl >= 0 else "🔴"
            side_emoji = "📈" if side == "LONG" else "📉"
            
            # Calculate P&L percentage
            notional = abs(size * entry)
            pnl_pct = (upnl / notional * 100) if notional > 0 else Decimal("0")
            pnl_pct_sign = "+" if pnl_pct >= 0 else ""
            
            lines.append(
                f"{side_emoji} <b>{symbol}</b> ({side})\n"
                f"  Entry: ${entry:.4f} → Mark: ${mark:.4f}\n"
                f"  Size: {size} ({leverage}x)\n"
                f"  {pnl_emoji} P&L: {upnl_sign}${upnl:.2f} ({pnl_pct_sign}{pnl_pct:.1f}%)\n"
            )
        
        await self._send_message("\n".join(lines))

    async def _handle_trades(self) -> None:
        """Respond to /trades with recent closed trades."""
        try:
            import asyncio
            from src.storage.repository import get_all_trades
            
            trades = await asyncio.to_thread(get_all_trades)
            trades = trades[:5]  # Last 5
        except Exception as e:
            await self._send_message(f"❌ Failed to fetch trades: {e}")
            return
        
        if not trades:
            await self._send_message("📋 No closed trades yet")
            return
        
        lines = ["📋 <b>Recent Trades</b>\n"]
        total_pnl = Decimal("0")
        wins = 0
        
        for t in trades:
            pnl = t.net_pnl
            total_pnl += pnl
            if pnl > 0:
                wins += 1
            
            pnl_emoji = "🟢" if pnl >= 0 else "🔴"
            pnl_sign = "+" if pnl >= 0 else ""
            side_emoji = "📈" if t.side.value.upper() == "LONG" else "📉"
            
            # Duration
            if t.entered_at and t.exited_at:
                duration = t.exited_at - t.entered_at
                hours = duration.total_seconds() / 3600
                if hours < 1:
                    dur_str = f"{duration.total_seconds() / 60:.0f}m"
                elif hours < 24:
                    dur_str = f"{hours:.1f}h"
                else:
                    dur_str = f"{hours / 24:.1f}d"
            else:
                dur_str = "?"
            
            lines.append(
                f"{side_emoji} <b>{t.symbol}</b> ({t.side.value.upper()})\n"
                f"  {pnl_emoji} {pnl_sign}${pnl:.2f} | {dur_str} | {t.exit_reason or '?'}\n"
            )
        
        # Summary
        total_sign = "+" if total_pnl >= 0 else ""
        total_emoji = "🟢" if total_pnl >= 0 else "🔴"
        lines.append(
            f"\n{total_emoji} <b>Total: {total_sign}${total_pnl:.2f}</b> "
            f"({wins}/{len(trades)} wins)"
        )
        
        await self._send_message("\n".join(lines))
