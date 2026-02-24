"""
Trade Recorder — persists one Trade row per position lifecycle.

Called by the ExecutionGateway after every close path (normal exit, stop,
TP ladder completion, force-close, reconciliation).

Design decisions:
- Uses position.position_id as trade_id (natural lifecycle key, PK-unique).
- Computes entry/exit VWAPs directly from position.entry_fills / exit_fills
  (per-lifecycle attribution — never recomputed from "all fills since opened_at").
- Fees estimated per fill using inferred fill type (maker/taker) + config rates.
  Conservative fallback: unknown fill type → taker.
- Funding estimated separately — never mixed into fee calculation.
- Idempotent: IntegrityError on duplicate trade_id → set trade_recorded=True,
  log WARNING, return None.
"""
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from src.domain.models import Trade, Side
from sqlalchemy.exc import IntegrityError as _IntegrityError
from src.exceptions import OperationalError, DataError
from src.execution.position_state_machine import (
    ManagedPosition,
    PositionState,
    FillRecord,
    ExitReason,
)
from src.monitoring.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Fill-type inference
# ---------------------------------------------------------------------------

_TAKER = "taker"
_MAKER = "maker"


def _infer_fill_type(fill: FillRecord, position: ManagedPosition) -> str:
    """
    Infer whether a fill was maker or taker from the order context.

    Heuristic (directionally correct, not 100%):
      - TP limit orders → maker (resting on book)
      - Stop orders → taker (triggered, crosses spread)
      - Market entry/exit → taker
      - Limit entry → maker
      - Reconciliation / synthetic fills → taker (conservative)

    Returns "maker" or "taker".
    """
    order_id = fill.order_id

    # Synthetic / reconciliation fills
    if order_id.startswith("reconcile-") or order_id.startswith("sync-") or order_id == "":
        return _TAKER

    # TP orders are limit orders resting on book → maker
    if position.tp1_order_id and order_id == position.tp1_order_id:
        return _MAKER
    if position.tp2_order_id and order_id == position.tp2_order_id:
        return _MAKER

    # Stop orders → taker (market after trigger)
    if position.stop_order_id and order_id == position.stop_order_id:
        return _TAKER

    # Exit orders (explicit close) — could be limit or market.
    # Conservative default: taker.
    if position.pending_exit_order_id and order_id == position.pending_exit_order_id:
        return _TAKER

    # Entry orders — if it reached the book, likely maker.
    # But market entries are taker.  We don't track order type on
    # FillRecord, so default to maker for entry (limit entries are
    # the common case in this system).
    if fill.is_entry:
        return _MAKER

    # Unknown — conservative
    return _TAKER


def _resolve_exit_reason(position: ManagedPosition) -> str:
    """
    Return a deterministic exit reason for persisted trade rows.

    If a close path forgot to set ``position.exit_reason``, infer a conservative
    fallback from observed exit order IDs and persist it back onto the position.
    """
    if position.exit_reason is not None:
        return position.exit_reason.value

    exit_order_ids = {f.order_id for f in position.exit_fills if f.order_id}
    resolved = ExitReason.RECONCILIATION

    # Prefer concrete order-driven reasons when we can infer them.
    if position.stop_order_id and position.stop_order_id in exit_order_ids:
        resolved = ExitReason.TRAILING_STOP if position.trailing_active else ExitReason.STOP_LOSS
    elif (
        (position.tp1_order_id and position.tp1_order_id in exit_order_ids)
        or (position.tp2_order_id and position.tp2_order_id in exit_order_ids)
    ):
        resolved = ExitReason.TAKE_PROFIT_FINAL
    elif position.pending_exit_order_id and position.pending_exit_order_id in exit_order_ids:
        resolved = ExitReason.MANUAL
    elif position.state == PositionState.ORPHANED:
        resolved = ExitReason.RECONCILIATION

    position.exit_reason = resolved
    logger.warning(
        "Missing exit_reason on closed position, applying deterministic fallback",
        position_id=position.position_id,
        symbol=position.symbol,
        resolved_exit_reason=resolved.value,
        state=position.state.value,
    )
    return resolved.value


# ---------------------------------------------------------------------------
# Core recorder
# ---------------------------------------------------------------------------


def record_closed_trade(
    position: ManagedPosition,
    maker_fee_rate: Decimal,
    taker_fee_rate: Decimal,
    funding_rate_daily_bps: Decimal = Decimal("10"),
) -> Optional[Trade]:
    """
    Build and persist a Trade record for a closed position.

    Args:
        position: A ManagedPosition that has reached CLOSED state.
        maker_fee_rate: Maker fee as a fraction (e.g. Decimal("0.0002")).
        taker_fee_rate: Taker fee as a fraction (e.g. Decimal("0.0005")).
        funding_rate_daily_bps: Daily funding rate in bps (default 10 = 0.10%).

    Returns:
        The persisted Trade, or None if already recorded / not eligible.
    """
    # ---- Guards ----
    terminal_states = (PositionState.CLOSED, PositionState.ORPHANED)
    if position.state not in terminal_states:
        return None
    if position.trade_recorded:
        logger.debug(
            "Trade already recorded, skipping",
            position_id=position.position_id,
            symbol=position.symbol,
        )
        return None

    # ---- VWAPs ----
    entry_vwap = position.avg_entry_price
    exit_vwap = position.avg_exit_price

    # Fallback: if entry VWAP is zero/None but initial_entry_price is set
    # (common for orphaned/imported positions with synthetic fills)
    if (entry_vwap is None or entry_vwap == 0) and position.initial_entry_price and position.initial_entry_price > 0:
        logger.info(
            "Using initial_entry_price as fallback for missing/zero avg_entry_price",
            position_id=position.position_id,
            symbol=position.symbol,
            initial_entry_price=str(position.initial_entry_price),
            avg_entry_price=str(entry_vwap),
        )
        entry_vwap = position.initial_entry_price

    if entry_vwap is None or entry_vwap == 0 or exit_vwap is None or exit_vwap == 0:
        logger.warning(
            "Cannot record trade yet: missing/zero VWAP — will retry after backfill",
            position_id=position.position_id,
            symbol=position.symbol,
            entry_vwap=str(entry_vwap),
            exit_vwap=str(exit_vwap),
            has_entry_fills=len(position.entry_fills),
            has_exit_fills=len(position.exit_fills),
        )
        return None

    # ---- Size ----
    qty = position.filled_entry_qty
    if qty <= 0:
        logger.warning(
            "Cannot record trade yet: zero filled qty — will retry after backfill",
            position_id=position.position_id,
            symbol=position.symbol,
        )
        return None

    size_notional = qty * entry_vwap
    if size_notional <= 0:
        logger.critical(
            "TRADE_RECORD_BLOCKED_INVALID_NOTIONAL",
            position_id=position.position_id,
            symbol=position.symbol,
            qty=str(qty),
            entry_vwap=str(entry_vwap),
            size_notional=str(size_notional),
        )
        return None

    # ---- Gross PnL ----
    if position.side == Side.LONG:
        gross_pnl = (exit_vwap - entry_vwap) * qty
    else:
        gross_pnl = (entry_vwap - exit_vwap) * qty

    # ---- Fees (per-fill, maker/taker from order context) ----
    total_fees = Decimal("0")
    maker_count = 0
    taker_count = 0

    for fill in position.entry_fills + position.exit_fills:
        fill_notional = fill.qty * fill.price
        fill_type = _infer_fill_type(fill, position)
        if fill_type == _MAKER:
            total_fees += fill_notional * maker_fee_rate
            maker_count += 1
        else:
            total_fees += fill_notional * taker_fee_rate
            taker_count += 1

    # ---- Funding (separate from fees) ----
    opened_at = position.created_at
    closed_at = position.exit_time or datetime.now(timezone.utc)
    holding_hours = max(
        Decimal(str((closed_at - opened_at).total_seconds() / 3600)),
        Decimal("0"),
    )
    # funding_rate_daily_bps is daily bps (e.g. 10 → 0.10% / day)
    funding_rate_per_hour = Decimal(str(funding_rate_daily_bps)) / Decimal("10000") / Decimal("24")
    funding = size_notional * funding_rate_per_hour * holding_hours

    # ---- Net PnL ----
    net_pnl = gross_pnl - total_fees - funding

    # ---- Leverage ----
    leverage = getattr(position, "leverage", None) or Decimal("1")
    if not isinstance(leverage, Decimal):
        leverage = Decimal(str(leverage))

    # ---- Exit reason ----
    exit_reason = _resolve_exit_reason(position)

    # ---- Timing: use final fill timestamp, not datetime.now() ----
    # Priority: exchange fill time > state-machine exit_time > fallback now()
    exit_time_source = "fill_timestamp"
    if position.exit_fills:
        exited_at = max(f.timestamp for f in position.exit_fills)
    elif position.exit_time:
        exited_at = position.exit_time
        exit_time_source = "state_machine_exit_time"
    else:
        exited_at = datetime.now(timezone.utc)
        exit_time_source = "fallback_now"
        logger.warning(
            "Trade exit timestamp using fallback now() — no fills or exit_time available",
            position_id=position.position_id,
            symbol=position.symbol,
        )

    entered_at = opened_at
    if position.entry_fills:
        entered_at = min(f.timestamp for f in position.entry_fills)

    trade = Trade(
        trade_id=position.position_id,
        symbol=position.symbol,
        side=position.side,
        entry_price=entry_vwap,
        exit_price=exit_vwap,
        size=qty,
        size_notional=size_notional,
        leverage=leverage,
        gross_pnl=gross_pnl,
        fees=total_fees,
        funding=funding,
        net_pnl=net_pnl,
        entered_at=entered_at,
        exited_at=exited_at,
        holding_period_hours=holding_hours,
        exit_reason=exit_reason,
        maker_fills_count=maker_count,
        taker_fills_count=taker_count,
        setup_type=position.setup_type,
        regime=position.regime,
    )

    # ---- Persist ----
    try:
        from src.storage.repository import save_trade
        save_trade(trade)
    except (OperationalError, DataError, OSError, _IntegrityError) as e:
        err_str = str(e).lower()
        if "duplicate" in err_str or "unique" in err_str or "integrity" in err_str:
            logger.warning(
                "Trade already exists in DB (duplicate PK), marking recorded",
                trade_id=trade.trade_id,
                symbol=trade.symbol,
            )
            position.trade_recorded = True
            return None
        else:
            logger.error(
                "TRADE_RECORD_FAILURE: Failed to persist trade",
                trade_id=trade.trade_id,
                symbol=trade.symbol,
                error=str(e),
            )
            # Do NOT set trade_recorded — allow retry on next cycle
            raise

    # ---- Mark recorded ----
    position.trade_recorded = True

    logger.info(
        "Trade recorded",
        trade_id=trade.trade_id,
        symbol=trade.symbol,
        side=trade.side.value,
        entry_price=str(entry_vwap),
        exit_price=str(exit_vwap),
        size=str(qty),
        gross_pnl=str(gross_pnl),
        fees_estimated=str(total_fees),
        funding_estimated=str(funding),
        net_pnl=str(net_pnl),
        exit_reason=exit_reason,
        maker_fills=maker_count,
        taker_fills=taker_count,
        holding_hours=f"{holding_hours:.2f}",
        exit_time_source=exit_time_source,
    )

    return trade


async def record_closed_trade_async(
    position: ManagedPosition,
    maker_fee_rate: Decimal,
    taker_fee_rate: Decimal,
    funding_rate_daily_bps: Decimal = Decimal("10"),
) -> Optional[Trade]:
    """Async wrapper — runs the synchronous DB write in a thread."""
    return await asyncio.to_thread(
        record_closed_trade,
        position,
        maker_fee_rate,
        taker_fee_rate,
        funding_rate_daily_bps,
    )
