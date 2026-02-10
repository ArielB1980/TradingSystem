"""
Health monitoring operations extracted from LiveTrading.

Functions in this module handle:
- Order polling (pending entry orders)
- Protection checks (naked position detection with escalation)
- System status reporting (for Telegram commands)
- Daily P&L summary (midnight UTC)
- Startup position protection validation
- Auto-recovery from margin-critical kill switch

All functions receive the LiveTrading instance as their first argument (``lt``)
to access shared state, following the same delegate pattern used by
protection_ops, signal_handler, auction_runner, and exchange_sync.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Dict

from src.execution.equity import calculate_effective_equity
from src.monitoring.logger import get_logger
from src.utils.kill_switch import KillSwitchReason

if TYPE_CHECKING:
    from src.live.live_trading import LiveTrading

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Order polling
# ---------------------------------------------------------------------------

async def run_order_polling(lt: "LiveTrading", interval_seconds: int = 12) -> None:
    """Poll pending entry order status, process fills, trigger PLACE_STOP (SL/TP)."""
    while lt.active:
        await asyncio.sleep(interval_seconds)
        if not lt.active:
            break
        if not lt.execution_gateway:
            continue
        try:
            n = await lt.execution_gateway.poll_and_process_order_updates()
            if n > 0:
                logger.info("Order poll processed updates", count=n)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Order poll failed", error=str(e))


# ---------------------------------------------------------------------------
# Protection checks
# ---------------------------------------------------------------------------

async def run_protection_checks(lt: "LiveTrading", interval_seconds: int = 30) -> None:
    """
    V2 protection monitor loop with escalation policy.

    If a naked position is detected in prod live, fail closed by activating
    the kill switch (emergency flatten).

    Startup grace: The first check is delayed by 3x the normal interval (90s
    by default) to give the main tick loop time to place missing stops after a
    restart or kill switch recovery.
    """
    startup_grace_seconds = interval_seconds * 3
    logger.info(
        "Protection monitor: startup grace period",
        grace_seconds=startup_grace_seconds,
        enforce_after="first check",
    )
    await asyncio.sleep(startup_grace_seconds)

    consecutive_naked_count: Dict[str, int] = {}
    # Require 3 consecutive detections (90s at 30s interval) before emergency kill.
    # This gives the stop-order poller (12s interval) multiple chances to detect
    # and process a legitimate stop fill before we escalate to kill switch.
    ESCALATION_THRESHOLD = 3

    while lt.active:
        if not lt.active:
            break
        if not getattr(lt, "_protection_monitor", None):
            await asyncio.sleep(interval_seconds)
            continue
        try:
            results = await lt._protection_monitor.check_all_positions()
            naked = [s for s, ok in results.items() if not ok]
            if naked:
                for s in naked:
                    consecutive_naked_count[s] = consecutive_naked_count.get(s, 0) + 1

                persistent_naked = [
                    s
                    for s in naked
                    if consecutive_naked_count.get(s, 0) >= ESCALATION_THRESHOLD
                ]

                if persistent_naked:
                    logger.critical(
                        "NAKED_POSITIONS_DETECTED (persistent)",
                        naked_symbols=persistent_naked,
                        details=results,
                        consecutive_counts={
                            s: consecutive_naked_count[s] for s in persistent_naked
                        },
                    )
                    is_prod_live = (
                        os.getenv("ENVIRONMENT", "").strip().lower() == "prod"
                    ) and (not lt.config.system.dry_run)
                    if is_prod_live:
                        await lt.kill_switch.activate(
                            KillSwitchReason.RECONCILIATION_FAILURE, emergency=True
                        )
                        return
                else:
                    logger.warning(
                        "NAKED_POSITIONS_DETECTED (first occurrence, giving time to self-heal)",
                        naked_symbols=naked,
                        details=results,
                        consecutive_counts={
                            s: consecutive_naked_count.get(s, 0) for s in naked
                        },
                    )
            else:
                consecutive_naked_count.clear()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(
                "Protection check loop failed",
                error=str(e),
                error_type=type(e).__name__,
            )

        await asyncio.sleep(interval_seconds)


# ---------------------------------------------------------------------------
# Trade starvation monitor
# ---------------------------------------------------------------------------

async def run_trade_starvation_monitor(
    lt: "LiveTrading",
    check_interval_seconds: int = 300,
    *,
    starvation_window_hours: float = 6.0,
    min_signals_threshold: int = 10,
) -> None:
    """
    Alert if the system generates signals but never executes trades.

    This catches silent regressions where the signal pipeline works
    but the execution pipeline is blocked (sizing bug, auction deadlock,
    exchange rejection loop, missing futures mapping, etc.).

    Logic:
        Every ``check_interval_seconds`` (default 5 min), look at the
        rolling window of ``starvation_window_hours``.  If
        ``signals_generated >= min_signals_threshold`` AND
        ``orders_placed == 0`` over that window, fire an alert.

    The monitor tracks per-cycle stats from the CycleGuard (via the
    safety integration layer) and from the auction runner logs.
    """
    from src.monitoring.alerting import send_alert

    # Rolling window accumulators: (cycle_end_ts, signals, orders_placed)
    _history: list[tuple[datetime, int, int]] = []
    _alerted = False  # De-duplicate: only alert once per starvation episode

    # Let the system warm up before checking
    await asyncio.sleep(max(check_interval_seconds, 120))

    while lt.active:
        try:
            now = datetime.now(timezone.utc)
            cutoff = now - timedelta(hours=starvation_window_hours)

            # Collect current cycle stats from the CycleGuard
            hardening = getattr(lt, "hardening_layer", None)
            if hardening and hasattr(hardening, "cycle_guard") and hardening.cycle_guard:
                recent = hardening.cycle_guard.get_recent_cycles(limit=200)
                # Rebuild history from CycleGuard data
                _history.clear()
                for cycle in recent:
                    ts = cycle.get("started_at") or cycle.get("ended_at")
                    if ts and isinstance(ts, str):
                        try:
                            ts = datetime.fromisoformat(ts)
                        except (ValueError, TypeError):
                            continue
                    if ts is None:
                        continue
                    sig = cycle.get("signals_generated", 0)
                    orders = cycle.get("orders_placed", 0)
                    _history.append((ts, sig, orders))

            # Prune old entries
            _history[:] = [(ts, s, o) for ts, s, o in _history if ts >= cutoff]

            window_signals = sum(s for _, s, _ in _history)
            window_orders = sum(o for _, _, o in _history)

            if window_signals >= min_signals_threshold and window_orders == 0:
                if not _alerted:
                    msg = (
                        f"TRADE STARVATION: {window_signals} signals generated "
                        f"but 0 orders placed in the last {starvation_window_hours:.0f}h.\n"
                        f"Possible causes: sizing rejection, auction deadlock, "
                        f"exchange mapping failure, or risk pipeline bug."
                    )
                    logger.critical(
                        "TRADE_STARVATION_DETECTED",
                        signals=window_signals,
                        orders=window_orders,
                        window_hours=starvation_window_hours,
                    )
                    await send_alert("TRADE_STARVATION", msg, urgent=True)
                    _alerted = True
            else:
                if _alerted and window_orders > 0:
                    logger.info(
                        "Trade starvation resolved",
                        signals=window_signals,
                        orders=window_orders,
                    )
                    _alerted = False

            # Periodic health log (debug level)
            logger.debug(
                "Trade starvation check",
                window_signals=window_signals,
                window_orders=window_orders,
                window_hours=starvation_window_hours,
                alerted=_alerted,
            )

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(
                "Trade starvation monitor failed",
                error=str(e),
                error_type=type(e).__name__,
            )

        await asyncio.sleep(check_interval_seconds)


# ---------------------------------------------------------------------------
# Winner churn monitor
# ---------------------------------------------------------------------------

async def run_winner_churn_monitor(
    lt: "LiveTrading",
    check_interval_seconds: int = 300,
    *,
    max_wins_without_entry: int = 5,
    decay_hours: float = 12.0,
) -> None:
    """
    Alert if the same symbol wins the auction repeatedly without ever
    getting an entry executed.

    This catches the AXS-style deadlock regression: a symbol scores
    highest every cycle, wins the auction, but is always rejected at
    execution (basis guard, min-notional, exchange error, etc.).  The
    auction keeps picking it, starving other contenders.

    Logic:
        Track ``(symbol -> [win_timestamps])`` from auction plan logs.
        Track ``(symbol -> last_entry_ts)`` from successful opens.
        If a symbol has >= ``max_wins_without_entry`` wins in
        ``decay_hours`` without a single successful entry, fire an alert.
    """
    from src.monitoring.alerting import send_alert

    # symbols already alerted (to de-duplicate)
    _alerted_symbols: set[str] = set()

    # Warm-up
    await asyncio.sleep(max(check_interval_seconds, 120))

    while lt.active:
        try:
            now = datetime.now(timezone.utc)
            cutoff = now - timedelta(hours=decay_hours)

            # ---- Read auction tracking data from LiveTrading ----
            # lt._auction_win_log: Dict[symbol, list[datetime]] populated by auction_runner
            # lt._auction_entry_log: Dict[symbol, datetime] populated by auction_runner
            win_log = getattr(lt, "_auction_win_log", {})
            entry_log = getattr(lt, "_auction_entry_log", {})

            # ---- Prune old win entries ----
            for sym in list(win_log):
                win_log[sym] = [t for t in win_log[sym] if t >= cutoff]
                if not win_log[sym]:
                    del win_log[sym]

            # ---- Check for churn ----
            churn_symbols = []
            for sym, wins in win_log.items():
                if len(wins) < max_wins_without_entry:
                    continue
                # Has this symbol had a successful entry in the window?
                last_entry = entry_log.get(sym)
                if last_entry and last_entry >= cutoff:
                    continue  # Entry happened, not churning
                churn_symbols.append((sym, len(wins)))

            if churn_symbols:
                new_churn = [
                    (sym, count)
                    for sym, count in churn_symbols
                    if sym not in _alerted_symbols
                ]
                if new_churn:
                    symbols_str = ", ".join(
                        f"{sym} ({count} wins)" for sym, count in new_churn
                    )
                    msg = (
                        f"WINNER CHURN: {symbols_str} won the auction "
                        f">={max_wins_without_entry} times in {decay_hours:.0f}h "
                        f"without a single entry.\n"
                        f"Possible causes: persistent risk rejection, basis guard, "
                        f"exchange min-notional, missing futures mapping."
                    )
                    logger.warning(
                        "WINNER_CHURN_DETECTED",
                        churn_symbols=[(s, c) for s, c in new_churn],
                        window_hours=decay_hours,
                    )
                    await send_alert("WINNER_CHURN", msg, urgent=False)
                    for sym, _ in new_churn:
                        _alerted_symbols.add(sym)

            # Clear alerts for symbols that resolved (got an entry)
            resolved = _alerted_symbols - {sym for sym, _ in churn_symbols}
            if resolved:
                logger.info("Winner churn resolved", symbols=list(resolved))
            _alerted_symbols -= resolved

            logger.debug(
                "Winner churn check",
                tracked_symbols=len(win_log),
                churn_count=len(churn_symbols),
                alerted=len(_alerted_symbols),
            )

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(
                "Winner churn monitor failed",
                error=str(e),
                error_type=type(e).__name__,
            )

        await asyncio.sleep(check_interval_seconds)


# ---------------------------------------------------------------------------
# System status (Telegram data provider)
# ---------------------------------------------------------------------------

async def get_system_status(lt: "LiveTrading") -> dict:
    """
    Data provider for Telegram command handler.
    Returns current system state for /status and /positions commands.
    """
    result: dict = {
        "equity": Decimal("0"),
        "margin_used": Decimal("0"),
        "margin_pct": 0.0,
        "positions": [],
        "system_state": "UNKNOWN",
        "kill_switch_active": False,
        "cycle_count": getattr(lt, "_last_cycle_count", 0),
        "cooldowns_active": len(lt._signal_cooldown),
        "universe_size": len(lt._market_symbols()),
    }

    try:
        balance = await lt.client.get_futures_balance()
        base = getattr(lt.config.exchange, "base_currency", "USD")
        equity, available_margin, margin_used = await calculate_effective_equity(
            balance, base_currency=base, kraken_client=lt.client
        )
        result["equity"] = equity
        result["margin_used"] = margin_used
        result["margin_pct"] = (
            float((margin_used / equity) * 100) if equity > 0 else 0
        )
    except Exception as e:
        logger.warning("Status: failed to get equity", error=str(e))

    try:
        positions = await lt.client.get_all_futures_positions()
        result["positions"] = [p for p in positions if p.get("size", 0) != 0]
    except Exception as e:
        logger.warning("Status: failed to get positions", error=str(e))

    # System state
    kill_active = lt.kill_switch.is_active() if lt.kill_switch else False
    result["kill_switch_active"] = kill_active
    if kill_active:
        result["system_state"] = "KILL_SWITCH"
    elif lt.hardening and hasattr(lt.hardening, "invariant_monitor"):
        inv_state = lt.hardening.invariant_monitor.state.value
        result["system_state"] = (
            inv_state.upper() if inv_state != "active" else "NORMAL"
        )
    else:
        result["system_state"] = "NORMAL"

    # Connection pool health
    try:
        from src.storage.db import get_pool_status
        result["db_pool"] = get_pool_status()
    except Exception:
        pass

    return result


# ---------------------------------------------------------------------------
# Daily P&L summary
# ---------------------------------------------------------------------------

async def run_daily_summary(lt: "LiveTrading") -> None:
    """
    Send a daily P&L summary via Telegram at midnight UTC.

    Calculates: equity, daily P&L, open positions, trades today, win rate.
    Runs in a background loop, sleeping until the next midnight.
    """
    from src.monitoring.alerting import send_alert

    while lt.active:
        try:
            now = datetime.now(timezone.utc)
            tomorrow = (now + timedelta(days=1)).replace(
                hour=0, minute=0, second=5, microsecond=0
            )
            sleep_seconds = (tomorrow - now).total_seconds()
            await asyncio.sleep(sleep_seconds)

            if not lt.active:
                break

            try:
                account_info = await lt.client.get_futures_account_info()
                equity = Decimal(str(account_info.get("equity", 0)))
                margin_used = Decimal(str(account_info.get("marginUsed", 0)))
                margin_pct = (
                    float((margin_used / equity) * 100) if equity > 0 else 0
                )

                positions = await lt.client.get_all_futures_positions()
                open_positions = [
                    p for p in positions if p.get("size", 0) != 0
                ]

                today_trades = []
                try:
                    from src.storage.repository import get_trades_since

                    since = now - timedelta(hours=24)
                    all_trades = await asyncio.to_thread(get_trades_since, since)
                    today_trades = all_trades if all_trades else []
                except Exception:
                    pass

                wins = sum(
                    1 for t in today_trades if getattr(t, "net_pnl", 0) > 0
                )
                losses = sum(
                    1 for t in today_trades if getattr(t, "net_pnl", 0) <= 0
                )
                total_pnl = sum(
                    getattr(t, "net_pnl", Decimal("0")) for t in today_trades
                )
                win_rate = (
                    f"{(wins / (wins + losses) * 100):.0f}%"
                    if (wins + losses) > 0
                    else "N/A"
                )

                pnl_sign = "+" if total_pnl >= 0 else ""
                pnl_emoji = "\U0001f4c8" if total_pnl >= 0 else "\U0001f4c9"

                pos_lines = []
                for p in open_positions[:10]:
                    sym = p.get("symbol", "?")
                    side = p.get("side", "?")
                    upnl = Decimal(
                        str(
                            p.get(
                                "unrealizedPnl", p.get("unrealized_pnl", 0)
                            )
                        )
                    )
                    upnl_sign = "+" if upnl >= 0 else ""
                    pos_lines.append(
                        f"  \u2022 {sym} ({side}) {upnl_sign}${upnl:.2f}"
                    )

                positions_str = (
                    "\n".join(pos_lines) if pos_lines else "  None"
                )

                summary = (
                    f"{pnl_emoji} Daily Summary ({now.strftime('%Y-%m-%d')})\n"
                    f"\n"
                    f"Equity: ${equity:.2f}\n"
                    f"Margin used: {margin_pct:.1f}%\n"
                    f"\n"
                    f"Trades today: {len(today_trades)}\n"
                    f"Win/Loss: {wins}W / {losses}L ({win_rate})\n"
                    f"Day P&L: {pnl_sign}${total_pnl:.2f}\n"
                    f"\n"
                    f"Open positions ({len(open_positions)}):\n"
                    f"{positions_str}"
                )

                await send_alert("DAILY_SUMMARY", summary, urgent=True)
                logger.info(
                    "Daily summary sent",
                    equity=str(equity),
                    trades=len(today_trades),
                )

                lt.risk_manager.reset_daily_metrics(equity)

            except Exception as e:
                logger.warning(
                    "Failed to gather daily summary data", error=str(e)
                )

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Daily summary loop error", error=str(e))
            await asyncio.sleep(3600)


# ---------------------------------------------------------------------------
# Startup position protection validation
# ---------------------------------------------------------------------------

async def validate_position_protection(lt: "LiveTrading") -> None:
    """
    Validate all positions have protection (startup safety gate).

    Checks V2 position registry for unprotected positions.
    Emits alerts and optionally pauses trading.
    """
    from src.storage.repository import async_record_event

    unprotected = []
    tracked_symbols: set[str] = set()

    # V2: Check registry state (authoritative)
    for p in lt.position_registry.get_all_active():
        tracked_symbols.add(p.symbol)

        if p.remaining_qty <= 0:
            continue

        has_stop_price = p.current_stop_price is not None
        has_stop_order = p.stop_order_id is not None
        is_protected = bool(has_stop_price and has_stop_order)
        if not is_protected:
            unprotected.append(
                {
                    "symbol": p.symbol,
                    "source": "registry_v2",
                    "reason": "MISSING_STOP",
                    "has_sl_price": has_stop_price,
                    "has_sl_order": has_stop_order,
                    "is_protected": is_protected,
                    "remaining_qty": str(p.remaining_qty),
                }
            )

    if unprotected:
        logger.error(
            "UNPROTECTED positions detected",
            count=len(unprotected),
            positions=unprotected,
        )
        for up in unprotected:
            await async_record_event(
                "UNPROTECTED_POSITION", up["symbol"], up
            )
    else:
        logger.info(
            "All positions are protected",
            total_positions=len(tracked_symbols),
        )


# ---------------------------------------------------------------------------
# Auto-recovery from kill switch
# ---------------------------------------------------------------------------

async def try_auto_recovery(lt: "LiveTrading") -> bool:
    """
    Attempt automatic recovery from kill switch (margin_critical only).

    Rules (ALL must be true):
    1. Kill switch reason is MARGIN_CRITICAL
    2. At least 5 minutes since the halt was activated
    3. Current margin utilization is below 85% (well below 92% trigger)
    4. Fewer than 2 auto-recoveries in the last 24 hours

    Returns True if recovery was successful, False otherwise.
    """
    if not lt.kill_switch or not lt.kill_switch.is_active():
        return False

    if lt.kill_switch.reason != KillSwitchReason.MARGIN_CRITICAL:
        return False

    now = datetime.now(timezone.utc)

    # Rule 2: Cooldown since halt activation
    if lt.kill_switch.activated_at:
        elapsed = (now - lt.kill_switch.activated_at).total_seconds()
        if elapsed < lt._AUTO_RECOVERY_COOLDOWN_SECONDS:
            logger.debug(
                "Auto-recovery: waiting for cooldown",
                elapsed_seconds=int(elapsed),
                required_seconds=lt._AUTO_RECOVERY_COOLDOWN_SECONDS,
            )
            return False

    # Rule 4: Max recoveries per day
    cutoff = now - timedelta(hours=24)
    recent_attempts = [t for t in lt._auto_recovery_attempts if t > cutoff]
    lt._auto_recovery_attempts = recent_attempts  # Prune old entries
    if len(recent_attempts) >= lt._AUTO_RECOVERY_MAX_PER_DAY:
        logger.warning(
            "Auto-recovery: daily limit reached (system needs manual intervention)",
            attempts_today=len(recent_attempts),
            max_per_day=lt._AUTO_RECOVERY_MAX_PER_DAY,
        )
        return False

    # Rule 3: Check current margin utilization
    try:
        account_info = await lt.client.get_futures_account_info()
        equity = Decimal(str(account_info.get("equity", 0)))
        margin_used = Decimal(str(account_info.get("marginUsed", 0)))

        if equity <= 0:
            return False

        margin_util_pct = float((margin_used / equity) * 100)

        if margin_util_pct >= lt._AUTO_RECOVERY_MARGIN_SAFE_PCT:
            logger.info(
                "Auto-recovery: margin still too high",
                margin_util_pct=f"{margin_util_pct:.1f}",
                required_below=lt._AUTO_RECOVERY_MARGIN_SAFE_PCT,
            )
            return False

        # All conditions met -- recover!
        lt._auto_recovery_attempts.append(now)

        logger.critical(
            "AUTO_RECOVERY: Clearing kill switch (margin recovered)",
            margin_util_pct=f"{margin_util_pct:.1f}",
            recovery_attempt=len(lt._auto_recovery_attempts),
            max_per_day=lt._AUTO_RECOVERY_MAX_PER_DAY,
            halt_duration_seconds=(
                int((now - lt.kill_switch.activated_at).total_seconds())
                if lt.kill_switch.activated_at
                else 0
            ),
        )

        lt.kill_switch.acknowledge()

        if lt.hardening and lt.hardening.is_halted():
            lt.hardening.clear_halt(operator="auto_recovery")

        try:
            from src.monitoring.alerting import send_alert_sync

            send_alert_sync(
                "AUTO_RECOVERY",
                f"System auto-recovered from MARGIN_CRITICAL\n"
                f"Margin utilization: {margin_util_pct:.1f}%\n"
                f"Recovery #{len(lt._auto_recovery_attempts)} of {lt._AUTO_RECOVERY_MAX_PER_DAY}/day",
                urgent=True,
            )
        except Exception:
            pass

        return True

    except Exception as e:
        logger.warning(
            "Auto-recovery: failed to check margin", error=str(e)
        )
        return False
