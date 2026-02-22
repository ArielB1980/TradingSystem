"""
Auction-based portfolio allocation execution.

Extracted from live_trading.py to reduce god-object size.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Dict, List

from src.exceptions import OperationalError, DataError
from src.execution.equity import calculate_effective_equity
from src.monitoring.logger import get_logger
from src.storage.repository import get_active_position

if TYPE_CHECKING:
    from src.live.live_trading import LiveTrading

logger = get_logger(__name__)


async def run_auction_allocation(lt: "LiveTrading", raw_positions: List[Dict]) -> None:
    """
    Run auction-based portfolio allocation if auction mode is enabled.

    Collects all open positions and candidate signals, runs the auction,
    and executes the allocation plan.
    """
    logger.debug(
        "Auction: _run_auction_allocation called",
        signals_count=len(lt.auction_signals_this_tick),
    )
    try:
        from src.portfolio.auction_allocator import (
            position_to_open_metadata,
            create_candidate_signal,
        )
        from src.domain.models import Signal, SignalType

        # Get account state
        balance = await lt.client.get_futures_balance()
        base = getattr(lt.config.exchange, "base_currency", "USD")
        equity, available_margin, _ = await calculate_effective_equity(
            balance, base_currency=base, kraken_client=lt.client
        )

        # Build spot-to-futures mapping for symbol matching
        spot_to_futures_map: Dict[str, str] = {}
        for pos_data in raw_positions:
            futures_sym = pos_data.get("symbol")
            if futures_sym:
                for spot_sym in lt.auction_signals_this_tick:
                    mapped_futures = lt.futures_adapter.map_spot_to_futures(
                        spot_sym[0].symbol,
                        futures_tickers=lt.latest_futures_tickers,
                    )
                    if mapped_futures == futures_sym:
                        spot_to_futures_map[spot_sym[0].symbol] = futures_sym
                        break

        open_positions_meta = []
        for pos_data in raw_positions:
            if pos_data.get("size", 0) == 0:
                continue
            try:
                pos = lt._convert_to_position(pos_data)
                futures_symbol = pos.symbol

                # Merge protection status from database
                try:
                    db_pos = await asyncio.to_thread(get_active_position, futures_symbol)
                    if db_pos:
                        pos.is_protected = db_pos.is_protected
                        pos.protection_reason = db_pos.protection_reason
                        pos.stop_loss_order_id = db_pos.stop_loss_order_id
                        pos.initial_stop_price = db_pos.initial_stop_price
                        if hasattr(db_pos, "tp_order_ids"):
                            pos.tp_order_ids = db_pos.tp_order_ids
                except (OperationalError, DataError) as e:
                    logger.warning(
                        "Failed to fetch DB position for protection merge",
                        symbol=futures_symbol,
                        error=str(e),
                        error_type=type(e).__name__,
                    )

                is_protective_live = pos.stop_loss_order_id is not None or (
                    hasattr(pos, "tp_order_ids") and pos.tp_order_ids
                )
                meta = position_to_open_metadata(
                    position=pos,
                    account_equity=equity,
                    is_protective_orders_live=is_protective_live,
                )
                spot_symbol = None
                for spot_sym, fut_sym in spot_to_futures_map.items():
                    if fut_sym == futures_symbol:
                        spot_symbol = spot_sym
                        break
                if not spot_symbol:
                    base_sym = (
                        futures_symbol.replace("PF_", "")
                        .replace("USD", "")
                        .replace("PI_", "")
                        .replace("FI_", "")
                    )
                    if base_sym:
                        spot_symbol = f"{base_sym}/USD"
                meta.spot_symbol = spot_symbol
                open_positions_meta.append(meta)
            except (ValueError, TypeError, KeyError) as e:
                logger.error(
                    "Failed to convert position for auction",
                    symbol=pos_data.get("symbol"),
                    error=str(e),
                    error_type=type(e).__name__,
                )

        # Pre-filter: exclude signals for symbols that already have open positions
        open_position_symbols: set = set()
        for meta in open_positions_meta:
            spot = getattr(meta, "spot_symbol", None)
            if spot:
                from src.data.symbol_utils import normalize_symbol_for_position_match
                open_position_symbols.add(normalize_symbol_for_position_match(spot))

        pre_filter_count = len(lt.auction_signals_this_tick)
        lt.auction_signals_this_tick = [
            (sig, sp, mp) for sig, sp, mp in lt.auction_signals_this_tick
            if normalize_symbol_for_position_match(sig.symbol) not in open_position_symbols
        ]
        filtered_out = pre_filter_count - len(lt.auction_signals_this_tick)
        if filtered_out > 0:
            logger.debug(
                "Auction: pre-filtered signals for existing positions",
                filtered_out=filtered_out,
                open_symbols=sorted(open_position_symbols),
            )

        signals_count = len(lt.auction_signals_this_tick)
        logger.info("Auction: Collecting candidate signals", signals_count=signals_count, pre_filtered=filtered_out)

        # Refresh instrument spec registry
        try:
            await lt.instrument_spec_registry.refresh()
        except (OperationalError, DataError) as e:
            logger.warning("Instrument spec refresh failed before auction", error=str(e), error_type=type(e).__name__)

        auction_budget_margin = equity * Decimal(str(lt.config.risk.auction_max_margin_util))

        candidate_signals = []
        signal_to_candidate: dict = {}
        requested_leverage = int(getattr(lt.config.risk, "target_leverage", 7) or 7)

        from src.risk.symbol_cooldown import check_symbol_cooldown

        for signal, spot_price, mark_price in lt.auction_signals_this_tick:
            try:
                if getattr(lt.config.strategy, "symbol_loss_cooldown_enabled", True):
                    is_on_cooldown, cooldown_reason = check_symbol_cooldown(
                        symbol=signal.symbol,
                        lookback_hours=getattr(lt.config.strategy, "symbol_loss_lookback_hours", 24),
                        loss_threshold=getattr(lt.config.strategy, "symbol_loss_threshold", 3),
                        cooldown_hours=getattr(lt.config.strategy, "symbol_loss_cooldown_hours", 12),
                        min_pnl_pct=getattr(lt.config.strategy, "symbol_loss_min_pnl_pct", -0.5),
                    )
                    if is_on_cooldown:
                        logger.warning(
                            "AUCTION_OPEN_REJECTED",
                            symbol=signal.symbol,
                            reason="SYMBOL_COOLDOWN",
                            details=cooldown_reason,
                        )
                        continue

                futures_symbol = lt.futures_adapter.map_spot_to_futures(
                    signal.symbol, futures_tickers=lt.latest_futures_tickers
                )
                spec = lt.instrument_spec_registry.get_spec(futures_symbol)
                if not spec:
                    logger.warning(
                        "AUCTION_OPEN_REJECTED",
                        symbol=signal.symbol,
                        reason="NO_SPEC",
                        requested_leverage=requested_leverage,
                        spec_summary=None,
                    )
                    continue

                symbol_tier = (
                    lt.market_discovery.get_symbol_tier(signal.symbol)
                    if lt.market_discovery
                    else "C"
                )
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
                    available_margin=auction_budget_margin,
                    symbol_tier=symbol_tier,
                )
                if not decision.approved:
                    logger.info(
                        "Auction candidate rejected by risk manager",
                        symbol=signal.symbol,
                        score=signal.score,
                        rejection_reasons=decision.rejection_reasons,
                        position_notional=str(decision.position_notional),
                    )
                elif decision.position_notional > 0 and decision.margin_required > 0:
                    stop_distance = (
                        abs(signal.entry_price - signal.stop_loss) / signal.entry_price
                        if signal.stop_loss
                        else Decimal("0")
                    )
                    risk_R = (
                        decision.position_notional * stop_distance
                        if stop_distance > 0
                        else Decimal("0")
                    )
                    candidate = create_candidate_signal(
                        signal=signal,
                        required_margin=decision.margin_required,
                        risk_R=risk_R,
                        position_notional=decision.position_notional,
                    )
                    candidate_signals.append(candidate)
                    signal_to_candidate[signal.symbol] = candidate
                    logger.info(
                        "Auction candidate created",
                        symbol=signal.symbol,
                        score=signal.score,
                        notional=str(decision.position_notional),
                        margin=str(decision.margin_required),
                    )
                else:
                    logger.warning(
                        "Signal not added to auction candidates",
                        symbol=signal.symbol,
                        score=signal.score,
                        position_notional=str(decision.position_notional),
                        margin_required=str(decision.margin_required),
                        approved=decision.approved,
                        rejection_reasons=decision.rejection_reasons,
                    )
            except (OperationalError, DataError, ValueError, TypeError, KeyError) as e:
                logger.error(
                    "Failed to create candidate signal for auction",
                    symbol=signal.symbol,
                    error=str(e),
                    error_type=type(e).__name__,
                )

        portfolio_state = {
            "available_margin": auction_budget_margin,
            "account_equity": equity,
            "last_partial_close_at": getattr(lt, "_last_partial_close_at", None),
            "partial_close_cooldown_seconds": getattr(
                lt.config.risk, "auction_partial_close_cooldown_seconds", 0
            ),
        }

        plan = lt.auction_allocator.allocate(
            open_positions=open_positions_meta,
            candidate_signals=candidate_signals,
            portfolio_state=portfolio_state,
        )

        logger.info(
            "Auction plan generated",
            closes_count=len(plan.closes),
            closes_symbols=plan.closes,
            opens_count=len(plan.opens),
            opens_symbols=[s.symbol for s in plan.opens],
            reasons=plan.reasons,
        )

        # Record auction wins for churn tracking
        now_utc = datetime.now(timezone.utc)
        for sig in plan.opens:
            lt._auction_win_log.setdefault(sig.symbol, []).append(now_utc)

        # Execute closes first
        for symbol in plan.closes:
            try:
                await lt.client.close_position(symbol)
                logger.info("Auction: Closed position", symbol=symbol)
            except (OperationalError, DataError) as e:
                logger.error("Auction: Failed to close position", symbol=symbol, error=str(e), error_type=type(e).__name__)

        # Refresh margin after closes
        balance_after_closes = await lt.client.get_futures_balance()
        equity_after, refreshed_available_margin, _ = await calculate_effective_equity(
            balance_after_closes, base_currency=base, kraken_client=lt.client
        )
        logger.info(
            "Auction: Margin refreshed after closes",
            equity=str(equity_after),
            refreshed_available_margin=str(refreshed_available_margin),
            previous_available_margin=str(available_margin),
        )

        # Execute opens (deduplicated)
        seen_opens: set = set()
        opens_executed = 0
        opens_failed = 0
        rejection_counts: Dict[str, int] = {}

        for signal in plan.opens:
            if signal.symbol in seen_opens:
                logger.warning(
                    "Auction: Skipping duplicate open for same symbol",
                    symbol=signal.symbol,
                )
                continue
            seen_opens.add(signal.symbol)
            try:
                # Hard entry blocklist
                spot_key = (signal.symbol or "").strip().upper().split(":")[0]
                base_cur = spot_key.split("/")[0].strip() if "/" in spot_key else spot_key
                blocked_spot = set(
                    s.strip().upper().split(":")[0]
                    for s in getattr(lt.config.execution, "entry_blocklist_spot_symbols", []) or []
                )
                blocked_base = set(
                    b.strip().upper()
                    for b in getattr(lt.config.execution, "entry_blocklist_bases", []) or []
                )
                if (spot_key and spot_key in blocked_spot) or (base_cur and base_cur in blocked_base):
                    opens_failed += 1
                    reason = "ENTRY_BLOCKED"
                    rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
                    logger.warning(
                        "Auction: Open blocked by entry blocklist",
                        symbol=signal.symbol,
                        reason=(
                            "blocked_spot_symbol"
                            if spot_key in blocked_spot
                            else "blocked_base"
                        ),
                    )
                    continue

                spot_price_val = None
                mark_price_val = None
                candidate = signal_to_candidate.get(signal.symbol)

                for sig, sp, mp in lt.auction_signals_this_tick:
                    if sig.symbol == signal.symbol:
                        spot_price_val = sp
                        mark_price_val = mp
                        break

                if spot_price_val and mark_price_val and candidate:
                    logger.info(
                        "Auction: Executing open with overrides",
                        symbol=signal.symbol,
                        notional_override=str(candidate.position_notional),
                        refreshed_margin=str(refreshed_available_margin),
                    )
                    result = await lt._handle_signal(
                        signal, spot_price_val, mark_price_val,
                        notional_override=candidate.position_notional,
                    )
                    if result.get("order_placed", False):
                        opens_executed += 1
                        lt._auction_entry_log[signal.symbol] = datetime.now(timezone.utc)
                        logger.info(
                            "Auction: Opened position",
                            symbol=signal.symbol,
                            reason=result.get("reason", "unknown"),
                        )
                    else:
                        opens_failed += 1
                        rejection_reasons = result.get("rejection_reasons", [])
                        for r in rejection_reasons:
                            rejection_counts[r] = rejection_counts.get(r, 0) + 1
                        logger.warning(
                            "Auction: Open rejected/failed",
                            symbol=signal.symbol,
                            reason=result.get("reason", "unknown"),
                            rejection_reasons=rejection_reasons,
                        )
                else:
                    opens_failed += 1
                    missing: list = []
                    if not spot_price_val or not mark_price_val:
                        missing.append("price_data")
                    if not candidate:
                        missing.append("candidate")
                    reason = "missing_data:" + ",".join(missing)
                    rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
                    logger.warning(
                        "Auction: Missing data for signal",
                        symbol=signal.symbol,
                        missing=missing,
                    )
            except ValueError as e:
                opens_failed += 1
                err_str = str(e)
                if "SIZE_BELOW_MIN" in err_str:
                    reason = "SIZE_BELOW_MIN"
                elif "SIZE_STEP_ROUND_TO_ZERO" in err_str:
                    reason = "SIZE_STEP_ROUND_TO_ZERO"
                elif "Size validation failed" in err_str:
                    reason = err_str.split(":")[-1].strip() if ":" in err_str else "SIZE_VALIDATION_FAILED"
                else:
                    reason = "ValueError"
                rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
                logger.error(
                    "Auction: Failed to open position (size/validation)",
                    symbol=signal.symbol,
                    error=err_str,
                )
            except (OperationalError, DataError) as e:
                opens_failed += 1
                reason = type(e).__name__
                rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
                logger.error(
                    "Auction: Failed to open position",
                    symbol=signal.symbol,
                    error=str(e),
                    error_type=type(e).__name__,
                    exc_info=True,
                )

        logger.info(
            "Auction allocation executed",
            closes=len(plan.closes),
            opens_planned=len(plan.opens),
            opens_executed=opens_executed,
            opens_failed=opens_failed,
            rejection_counts=rejection_counts if rejection_counts else None,
            reasons=plan.reasons,
        )
        if opens_executed > 0 or len(plan.closes) > 0:
            lt._reconcile_requested = True

    except (OperationalError, DataError, ValueError, TypeError, KeyError) as e:
        logger.error("Failed to run auction allocation", error=str(e), error_type=type(e).__name__)
