"""
Signal processing: risk validation, state machine entry, order placement.

Extracted from live_trading.py to reduce god-object size.
All functions receive a typed reference to the LiveTrading host.
"""
from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from src.exceptions import OperationalError, DataError
from src.domain.models import Signal, SignalType, Side
from src.execution.equity import calculate_effective_equity
from src.execution.position_manager_v2 import ActionType as ActionTypeV2
from src.monitoring.logger import get_logger

if TYPE_CHECKING:
    from src.live.live_trading import LiveTrading

logger = get_logger(__name__)


async def handle_signal(
    lt: "LiveTrading",
    signal: Signal,
    spot_price: Decimal,
    mark_price: Decimal,
) -> dict:
    """
    Process signal through Position State Machine V2.

    Args:
        lt: LiveTrading host reference
        signal: Trading signal
        spot_price: Current spot price
        mark_price: Current futures mark price

    Returns:
        dict with keys:
            - order_placed: bool
            - reason: str (human-readable reason for success/failure)
            - rejection_reasons: list[str] (if rejected)
    """
    lt.signals_since_emit += 1
    logger.info("New signal detected", type=signal.signal_type.value, symbol=signal.symbol)

    # Health gate: no new entries when candle health is insufficient
    if getattr(lt, "trade_paused", False):
        return {
            "order_placed": False,
            "reason": "TRADING PAUSED: candle health insufficient",
            "rejection_reasons": ["trade_paused"],
        }

    return await handle_signal_v2(lt, signal, spot_price, mark_price)


async def handle_signal_v2(
    lt: "LiveTrading",
    signal: Signal,
    spot_price: Decimal,
    mark_price: Decimal,
) -> dict:
    """
    Process signal through Position State Machine V2.

    CRITICAL: All orders flow through ExecutionGateway.
    No direct exchange calls allowed.

    Returns:
        {"order_placed": bool, "reason": str | None}
    """
    import uuid

    def _fail(reason: str) -> dict:
        return {"order_placed": False, "reason": reason}

    def _ok() -> dict:
        return {"order_placed": True, "reason": None}

    logger.info(
        "Processing signal via State Machine V2",
        symbol=signal.symbol,
        type=signal.signal_type.value,
    )

    # 1. Fetch Account Equity and Available Margin
    balance = await lt.client.get_futures_balance()
    base = getattr(lt.config.exchange, "base_currency", "USD")
    equity, available_margin, _ = await calculate_effective_equity(
        balance, base_currency=base, kraken_client=lt.client
    )
    if equity <= 0:
        logger.error("Insufficient equity for trading", equity=str(equity))
        return _fail("Insufficient equity for trading")

    # 2. Risk Validation (Safety Gate)
    symbol_tier = lt.market_discovery.get_symbol_tier(signal.symbol) if lt.market_discovery else "C"
    if symbol_tier != "A":
        static_tier = lt._get_static_tier(signal.symbol)
        if static_tier == "A":
            logger.warning(
                "Tier downgrade detected",
                symbol=signal.symbol,
                static_tier=static_tier,
                dynamic_tier=symbol_tier,
                reason="Dynamic classification is authoritative",
            )

    decision = lt.risk_manager.validate_trade(
        signal,
        equity,
        spot_price,
        mark_price,
        available_margin=available_margin,
        symbol_tier=symbol_tier,
    )

    if not decision.approved:
        reasons = getattr(decision, "rejection_reasons", []) or []
        detail = reasons[0] if reasons else "Trade rejected by Risk Manager"
        logger.warning("Trade rejected by Risk Manager", symbol=signal.symbol, reasons=reasons)
        return _fail(f"Risk Manager rejected: {detail}")
    logger.info("Risk approved", symbol=signal.symbol, notional=str(decision.position_notional))

    # 3. Map to futures symbol
    futures_symbol = lt.futures_adapter.map_spot_to_futures(
        signal.symbol, futures_tickers=lt.latest_futures_tickers
    )

    # 3b. Enforce minimum position notional (venue min_size * price)
    if hasattr(lt, "instrument_spec_registry") and lt.instrument_spec_registry and mark_price > 0:
        min_size = lt.instrument_spec_registry.get_effective_min_size(futures_symbol)
        min_notional = min_size * mark_price
        if decision.position_notional < min_notional:
            logger.warning(
                "Position notional below venue minimum - rejecting",
                symbol=signal.symbol,
                notional=str(decision.position_notional),
                min_notional=str(min_notional),
                min_size=str(min_size),
            )
            return _fail(f"Position notional {decision.position_notional} below venue min {min_notional}")

    # 4. Generate entry plan to get TP levels
    step_size = None
    if hasattr(lt, "instrument_spec_registry") and lt.instrument_spec_registry:
        spec = lt.instrument_spec_registry.get_spec(futures_symbol)
        if spec and spec.size_step > 0:
            step_size = spec.size_step
    order_intent = lt.execution_engine.generate_entry_plan(
        signal, decision.position_notional, spot_price, mark_price, decision.leverage,
        step_size=step_size,
    )

    tps = order_intent.get("take_profits", [])
    tp1_price = tps[0]["price"] if len(tps) > 0 else None
    tp2_price = tps[1]["price"] if len(tps) > 1 else None
    # In runner mode (2 TPs), final_target comes from metadata (3.0R aspiration level).
    # In legacy mode (3+ TPs), final_target is the last TP price.
    metadata = order_intent.get("metadata", {})
    final_target = metadata.get("final_target_price")
    if final_target is None:
        final_target = tps[-1]["price"] if len(tps) > 2 else None

    # 5. Calculate position size in contracts
    position_size = Decimal(str(order_intent.get("size", 0)))
    if position_size <= 0:
        position_size = decision.position_notional / mark_price

    # 6. Evaluate entry via Position Manager V2
    action, position = lt.position_manager_v2.evaluate_entry(
        signal=signal,
        entry_price=mark_price,
        stop_price=order_intent["metadata"]["fut_sl"],
        tp1_price=tp1_price,
        tp2_price=tp2_price,
        final_target=final_target,
        position_size=position_size,
        trade_type=signal.regime if hasattr(signal, "regime") else "tight_smc",
        leverage=decision.leverage,
    )

    if action.type == ActionTypeV2.REJECT_ENTRY:
        logger.warning("Entry REJECTED by State Machine", symbol=signal.symbol, reason=action.reason)
        return _fail(f"State Machine rejected: {action.reason or 'REJECT_ENTRY'}")
    logger.info(
        "State machine accepted entry",
        symbol=signal.symbol,
        client_order_id=action.client_order_id,
    )

    # 7. Handle opportunity cost replacement via V2
    if decision.should_close_existing and decision.close_symbol:
        logger.warning(
            "Opportunity cost replacement via V2",
            closing=decision.close_symbol,
            opening=signal.symbol,
        )

        close_actions = lt.position_manager_v2.request_reversal(
            decision.close_symbol,
            Side.LONG if signal.signal_type == SignalType.LONG else Side.SHORT,
            mark_price,
        )

        for close_action in close_actions:
            result = await lt.execution_gateway.execute_action(close_action)
            if not result.success:
                logger.error("Failed to close for replacement", error=result.error)
                return _fail(f"Failed to close for replacement: {result.error}")

        lt.position_registry.confirm_reversal_closed(decision.close_symbol)

    # 8. Register position in state machine
    position.entry_order_id = action.client_order_id
    position.entry_client_order_id = action.client_order_id
    position.futures_symbol = futures_symbol

    try:
        lt.position_registry.register_position(position)
    except (OperationalError, DataError) as e:
        logger.error("Failed to register position", error=str(e), error_type=type(e).__name__)
        return _fail(f"Failed to register position: {e}")

    # 9. Execute entry via Execution Gateway
    logger.info(
        "Submitting entry to gateway",
        symbol=futures_symbol,
        client_order_id=action.client_order_id,
    )
    result = await lt.execution_gateway.execute_action(action, order_symbol=futures_symbol)

    if not result.success:
        logger.error("Entry failed", error=result.error)
        position.mark_error(f"Entry failed: {result.error}")
        return _fail(f"Entry failed: {result.error}")

    logger.info(
        "Entry order placed via V2",
        symbol=futures_symbol,
        client_order_id=action.client_order_id,
        exchange_order_id=result.exchange_order_id,
    )

    # 10. Persist position state
    if lt.position_persistence:
        lt.position_persistence.save_position(position)
        lt.position_persistence.log_action(
            position.position_id,
            "entry_submitted",
            {
                "signal_type": signal.signal_type.value,
                "entry_price": str(mark_price),
                "stop_price": str(position.initial_stop_price),
                "size": str(position_size),
            },
        )

    # Send alert for new position
    try:
        from src.monitoring.alerting import send_alert_sync, fmt_price, fmt_size

        send_alert_sync(
            "NEW_POSITION",
            f"New {signal.signal_type.value} position\n"
            f"Symbol: {signal.symbol}\n"
            f"Size: {fmt_size(position_size)} @ ${fmt_price(mark_price)}\n"
            f"Stop: ${fmt_price(position.initial_stop_price)}",
        )
    except (OperationalError, ImportError, OSError):
        pass  # Alert failure must never block trading

    return _ok()
