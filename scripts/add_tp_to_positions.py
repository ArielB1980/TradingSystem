#!/usr/bin/env python3
"""
Add TP orders to existing positions.

Default: DRY-RUN (preview without placing orders)
Use --execute flag to actually place orders.
"""
import sys
import os
import asyncio
import argparse
import json
from decimal import Decimal
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.storage.db import get_db
from src.storage.repository import PositionModel, get_active_positions, save_position
from src.domain.models import Position, Side
from src.data.kraken_client import KrakenClient
from src.execution.executor import Executor
from src.execution.futures_adapter import FuturesAdapter
from src.config.config import load_config
from src.monitoring.logger import get_logger

logger = get_logger(__name__)


def collect_tp_orders(symbol_orders: List[Dict], position_side: Side) -> List[Dict]:
    """
    Collect TP-like reduce-only orders from exchange orders.
    
    Uses same filter as _needs_tp_backfill in live_trading.py.
    """
    open_tp_orders = [
        o for o in symbol_orders
        if o.get('reduceOnly', False) and 
        o.get('type', '').lower() in ('take_profit', 'take-profit', 'limit') and
        # For LONG positions, TP orders are SELL (opposite side)
        # For SHORT positions, TP orders are BUY (opposite side)
        ((position_side == Side.LONG and o.get('side', '').lower() == 'sell') or
         (position_side == Side.SHORT and o.get('side', '').lower() == 'buy'))
    ]
    
    # Prefer explicit take_profit type if available
    explicit_tp_orders = [
        o for o in open_tp_orders
        if o.get('type', '').lower() in ('take_profit', 'take-profit')
    ]
    
    if explicit_tp_orders:
        return explicit_tp_orders
    return open_tp_orders


def check_existing_coverage(
    existing_tp_orders: List[Dict],
    planned_tp_prices: List[Decimal],
    min_tp_count: int,
    tp_price_tolerance: Decimal
) -> Tuple[bool, List[Decimal]]:
    """
    Check if existing TP orders match planned prices within tolerance.
    
    Returns:
        (is_covered, existing_tp_prices_sorted)
    """
    if not existing_tp_orders:
        return False, []
    
    # Extract prices from existing orders
    existing_prices = []
    for order in existing_tp_orders:
        price = order.get('stopPrice') or order.get('price')
        if price:
            try:
                existing_prices.append(Decimal(str(price)))
            except (ValueError, TypeError):
                continue
    
    if not existing_prices:
        return False, []
    
    existing_prices_sorted = sorted(existing_prices)
    planned_prices_sorted = sorted(planned_tp_prices[:min_tp_count])
    
    # Match check: each planned price should have a matching existing price within tolerance
    matched_count = 0
    used_indices = set()
    
    for planned_price in planned_prices_sorted:
        for i, existing_price in enumerate(existing_prices_sorted):
            if i in used_indices:
                continue
            
            price_diff_pct = abs(existing_price - planned_price) / planned_price
            if price_diff_pct <= tp_price_tolerance:
                matched_count += 1
                used_indices.add(i)
                break
    
    is_covered = matched_count >= min_tp_count
    return is_covered, existing_prices_sorted


def compute_tp_plan(
    db_pos: Position,
    exchange_pos: Dict,
    current_price: Decimal,
    config
) -> Optional[List[Decimal]]:
    """
    Compute TP plan: prefer DB stored prices, else compute by R-multiples.
    
    Returns None if computation fails or guards fail.
    """
    # Prefer stored plan
    tp_plan = []
    if db_pos.tp1_price:
        tp_plan.append(db_pos.tp1_price)
    if db_pos.tp2_price:
        tp_plan.append(db_pos.tp2_price)
    if db_pos.final_target_price:
        tp_plan.append(db_pos.final_target_price)
    
    if len(tp_plan) >= 2:  # We have a stored plan
        return tp_plan
    
    # Compute by R-multiples
    entry = Decimal(str(exchange_pos.get('entry_price', 0)))
    sl = db_pos.initial_stop_price
    
    if not entry or not sl or entry == 0:
        return None
    
    risk = abs(entry - sl)
    if risk == 0:
        return None
    
    # Risk sanity check
    risk_pct = risk / entry
    if risk_pct < Decimal("0.002") or risk_pct > Decimal("0.10"):
        logger.warning("Risk outside sensible band", symbol=db_pos.symbol, risk_pct=float(risk_pct))
        return None
    
    # Determine side sign
    side_sign = Decimal("1") if db_pos.side == Side.LONG else Decimal("-1")
    
    # Compute TP ladder: 1R, 2R, 3R
    tp1 = entry + side_sign * Decimal("1.0") * risk
    tp2 = entry + side_sign * Decimal("2.0") * risk
    tp3 = entry + side_sign * Decimal("3.0") * risk
    
    tp_plan = [tp1, tp2, tp3]
    
    # Apply distance guards
    min_distance = current_price * Decimal(str(config.execution.min_tp_distance_pct))
    
    if db_pos.side == Side.LONG:
        # For LONG: require tp1 > current_price * (1 + min_tp_distance_pct)
        if tp1 <= current_price + min_distance:
            logger.warning("TP1 too close to current price (LONG)", symbol=db_pos.symbol, tp1=str(tp1), current=str(current_price))
            return None
    else:  # SHORT
        # For SHORT: require tp1 < current_price * (1 - min_tp_distance_pct)
        if tp1 >= current_price - min_distance:
            logger.warning("TP1 too close to current price (SHORT)", symbol=db_pos.symbol, tp1=str(tp1), current=str(current_price))
            return None
    
    # Optional: clamp extreme TPs
    if config.execution.max_tp_distance_pct:
        max_distance = current_price * Decimal(str(config.execution.max_tp_distance_pct))
        if db_pos.side == Side.LONG:
            tp_plan = [min(tp, current_price + max_distance) for tp in tp_plan]
        else:
            tp_plan = [max(tp, current_price - max_distance) for tp in tp_plan]
    
    return tp_plan


async def add_tp_to_positions(
    execute: bool = False,
    require_sl: bool = True,
    min_tp_count: int = 2,
    symbol_filter: Optional[str] = None,
    limit: Optional[int] = None,
    verbose: bool = False
):
    """
    Add TP orders to existing positions.
    
    Args:
        execute: If True, actually place orders. Default False (dry-run).
        require_sl: If True, only process protected positions. Default True.
        min_tp_count: Minimum TP orders to place. Default 2.
        symbol_filter: Process only specific symbol (optional).
        limit: Limit number of positions to process (optional).
        verbose: Verbose logging. Default False.
    """
    logger.info(
        "Starting TP addition",
        execute=execute,
        require_sl=require_sl,
        min_tp_count=min_tp_count,
        symbol_filter=symbol_filter,
        limit=limit,
        mode="EXECUTE" if execute else "DRY-RUN"
    )
    
    if not execute:
        logger.warning("DRY-RUN MODE: No orders will be placed. Use --execute to actually place orders.")
    
    # Load config
    config = load_config()
    
    # Initialize client and components
    client = KrakenClient(
        api_key=config.exchange.api_key,
        api_secret=config.exchange.api_secret,
        futures_api_key=config.exchange.futures_api_key,
        futures_api_secret=config.exchange.futures_api_secret,
        use_testnet=config.exchange.use_testnet
    )
    await client.initialize()
    
    futures_adapter = FuturesAdapter(
        client,
        position_size_is_notional=config.exchange.position_size_is_notional
    )
    executor = Executor(config.execution, futures_adapter)
    
    # 1. Load positions from DB
    db_positions = await asyncio.to_thread(get_active_positions)
    logger.info("Loaded positions from DB", count=len(db_positions))
    
    # 2. Fetch exchange state
    exchange_positions = await client.get_all_futures_positions()
    exchange_orders = await client.get_futures_open_orders()
    
    # Build lookup dicts
    ex_pos_by_symbol = {p.get('symbol'): p for p in exchange_positions if p.get('size', 0) != 0}
    orders_by_symbol: Dict[str, List[Dict]] = {}
    for order in exchange_orders:
        sym = order.get('symbol')
        if sym:
            if sym not in orders_by_symbol:
                orders_by_symbol[sym] = []
            orders_by_symbol[sym].append(order)
    
    logger.info(
        "Exchange state",
        positions=len(ex_pos_by_symbol),
        total_orders=len(exchange_orders),
        symbols_with_orders=len(orders_by_symbol)
    )
    
    # 3. Process each position
    success_count = 0
    failed_count = 0
    skipped_count = 0
    skip_reasons: Dict[str, int] = {}
    
    # Apply filters
    positions_to_process = db_positions
    if symbol_filter:
        positions_to_process = [p for p in positions_to_process if p.symbol == symbol_filter]
    if limit:
        positions_to_process = positions_to_process[:limit]
    
    for db_pos in positions_to_process:
        symbol = db_pos.symbol
        try:
            # Eligibility Gate
            exchange_pos = ex_pos_by_symbol.get(symbol)
            if not exchange_pos or exchange_pos.get('size', 0) == 0:
                skip_reasons['no_exchange_position'] = skip_reasons.get('no_exchange_position', 0) + 1
                if verbose:
                    logger.debug("Skipped: no exchange position", symbol=symbol)
                skipped_count += 1
                continue
            
            if not db_pos.initial_stop_price or not db_pos.stop_loss_order_id:
                skip_reasons['no_sl'] = skip_reasons.get('no_sl', 0) + 1
                if verbose:
                    logger.debug("Skipped: no SL", symbol=symbol)
                skipped_count += 1
                continue
            
            entry_price = Decimal(str(exchange_pos.get('entry_price', 0)))
            if entry_price == 0:
                entry_price = db_pos.entry_price
            if entry_price == 0:
                skip_reasons['no_entry_price'] = skip_reasons.get('no_entry_price', 0) + 1
                if verbose:
                    logger.debug("Skipped: no entry price", symbol=symbol)
                skipped_count += 1
                continue
            
            current_price = Decimal(str(exchange_pos.get('markPrice') or exchange_pos.get('mark_price') or exchange_pos.get('last', 0)))
            if current_price == 0:
                current_price = db_pos.current_mark_price
            if current_price == 0:
                skip_reasons['no_current_price'] = skip_reasons.get('no_current_price', 0) + 1
                if verbose:
                    logger.debug("Skipped: no current price", symbol=symbol)
                skipped_count += 1
                continue
            
            # CRITICAL: Verify SL order exists on exchange (not just DB)
            # Exception: If position already has TP orders, skip SL verification (it had SL when TP was placed)
            has_existing_tp = bool(db_pos.tp_order_ids and len(db_pos.tp_order_ids) > 0)
            
            if require_sl and config.execution.require_sl_for_tp_backfill and not has_existing_tp:
                # Check orders for this symbol and normalized symbol formats
                symbol_orders = orders_by_symbol.get(symbol, [])
                # Also check normalized formats (PF_XBTUSD <-> BTC/USD:USD)
                normalized_symbols = []
                if symbol.startswith("PF_"):
                    # PF_XBTUSD -> BTC/USD:USD
                    base = symbol[3:-3]  # Remove "PF_" and "USD"
                    if base == "XBT":
                        base = "BTC"
                    normalized_symbols.append(f"{base}/USD:USD")
                elif "/" in symbol and ":" in symbol:
                    # BTC/USD:USD -> PF_XBTUSD
                    base = symbol.split("/")[0]
                    if base == "BTC":
                        base = "XBT"
                    normalized_symbols.append(f"PF_{base}USD")
                
                # Collect all order IDs from all symbol formats
                all_sl_order_ids = set()
                for check_symbol in [symbol] + normalized_symbols:
                    for order in orders_by_symbol.get(check_symbol, []):
                        if order.get('reduceOnly') and ('stop' in str(order.get('type', '')).lower() or order.get('stopPrice')):
                            all_sl_order_ids.add(order.get('id'))
                
                if db_pos.stop_loss_order_id not in all_sl_order_ids:
                    skip_reasons['sl_order_not_on_exchange'] = skip_reasons.get('sl_order_not_on_exchange', 0) + 1
                    logger.warning("Skipped: SL order not on exchange", symbol=symbol, sl_order_id=db_pos.stop_loss_order_id, checked_symbols=[symbol] + normalized_symbols)
                    skipped_count += 1
                    continue
            
            # Compute TP plan
            tp_plan = compute_tp_plan(db_pos, exchange_pos, current_price, config)
            if not tp_plan:
                skip_reasons['tp_computation_failed'] = skip_reasons.get('tp_computation_failed', 0) + 1
                if verbose:
                    logger.debug("Skipped: TP computation failed", symbol=symbol)
                skipped_count += 1
                continue
            
            # Check existing coverage
            symbol_orders = orders_by_symbol.get(symbol, [])
            existing_tp_orders = collect_tp_orders(symbol_orders, db_pos.side)
            tp_price_tolerance = Decimal(str(config.execution.tp_price_tolerance))
            
            is_covered, existing_tp_prices = check_existing_coverage(
                existing_tp_orders,
                tp_plan,
                min_tp_count,
                tp_price_tolerance
            )
            
            if is_covered:
                # Orders exist and match, but check if we need to update DB prices
                needs_price_update = (
                    not db_pos.tp1_price or
                    not db_pos.tp2_price or
                    not db_pos.final_target_price
                )
                
                if needs_price_update and execute:
                    # Update DB with TP prices even though orders already exist
                    try:
                        updated_pos = Position(
                            symbol=db_pos.symbol,
                            side=db_pos.side,
                            size=db_pos.size,
                            size_notional=db_pos.size_notional,
                            entry_price=db_pos.entry_price,
                            current_mark_price=db_pos.current_mark_price,
                            liquidation_price=db_pos.liquidation_price,
                            unrealized_pnl=db_pos.unrealized_pnl,
                            leverage=db_pos.leverage,
                            margin_used=db_pos.margin_used,
                            opened_at=db_pos.opened_at,
                            initial_stop_price=db_pos.initial_stop_price,
                            stop_loss_order_id=db_pos.stop_loss_order_id,
                            tp_order_ids=db_pos.tp_order_ids or [],
                            tp1_price=tp_plan[0] if len(tp_plan) > 0 else None,
                            tp2_price=tp_plan[1] if len(tp_plan) > 1 else None,
                            final_target_price=tp_plan[2] if len(tp_plan) > 2 else None,
                            trade_type=db_pos.trade_type,
                            partial_close_pct=db_pos.partial_close_pct,
                            original_size=db_pos.original_size,
                            is_protected=getattr(db_pos, 'is_protected', True),
                            protection_reason=getattr(db_pos, 'protection_reason', None)
                        )
                        await asyncio.to_thread(save_position, updated_pos)
                        logger.info("Updated TP prices in DB (orders already exist)", symbol=symbol, tp_plan=[str(tp) for tp in tp_plan])
                        success_count += 1
                    except Exception as e:
                        logger.error("Failed to update TP prices", symbol=symbol, error=str(e))
                        failed_count += 1
                else:
                    skip_reasons['already_covered'] = skip_reasons.get('already_covered', 0) + 1
                    if verbose:
                        logger.debug("Skipped: already covered", symbol=symbol, existing_tps=len(existing_tp_orders))
                    skipped_count += 1
                continue
            
            # Log plan
            logger.info(
                "TP plan computed",
                symbol=symbol,
                side=db_pos.side.value,
                entry=str(entry_price),
                sl=str(db_pos.initial_stop_price),
                tp_plan=[str(tp) for tp in tp_plan],
                existing_tp_count=len(existing_tp_orders),
                existing_tp_prices=[str(p) for p in existing_tp_prices],
                execute=execute
            )
            
            if not execute:
                logger.info("DRY-RUN: Would place TP orders", symbol=symbol, tp_plan=[str(tp) for tp in tp_plan])
                success_count += 1
                continue
            
            # Place orders
            try:
                # Get position size notional using canonical helper
                position_size_notional = await futures_adapter.position_size_notional(
                    symbol=symbol,
                    pos_data=exchange_pos,
                    current_price=current_price
                )
                
                if not position_size_notional or position_size_notional == 0:
                    skip_reasons['zero_position_size'] = skip_reasons.get('zero_position_size', 0) + 1
                    logger.warning("Skipped: zero position size", symbol=symbol)
                    skipped_count += 1
                    continue
                
                # Place TP orders (don't modify SL - pass None to keep existing SL)
                new_sl_id, new_tp_ids = await executor.update_protective_orders(
                    symbol=symbol,
                    side=db_pos.side,
                    current_sl_id=db_pos.stop_loss_order_id,
                    new_sl_price=None,  # Don't modify SL - keep existing
                    current_tp_ids=db_pos.tp_order_ids or [],
                    new_tp_prices=tp_plan[:3],
                    position_size_notional=position_size_notional
                )
                
                # Update database - create new Position object with updated TP data
                updated_pos = Position(
                    symbol=db_pos.symbol,
                    side=db_pos.side,
                    size=db_pos.size,
                    size_notional=db_pos.size_notional,
                    entry_price=db_pos.entry_price,
                    current_mark_price=db_pos.current_mark_price,
                    liquidation_price=db_pos.liquidation_price,
                    unrealized_pnl=db_pos.unrealized_pnl,
                    leverage=db_pos.leverage,
                    margin_used=db_pos.margin_used,
                    opened_at=db_pos.opened_at,
                    initial_stop_price=db_pos.initial_stop_price,
                    stop_loss_order_id=db_pos.stop_loss_order_id,
                    tp_order_ids=new_tp_ids,
                    tp1_price=tp_plan[0] if len(tp_plan) > 0 else None,
                    tp2_price=tp_plan[1] if len(tp_plan) > 1 else None,
                    final_target_price=tp_plan[2] if len(tp_plan) > 2 else None,
                    trade_type=db_pos.trade_type,
                    partial_close_pct=db_pos.partial_close_pct,
                    original_size=db_pos.original_size,
                    is_protected=getattr(db_pos, 'is_protected', True),
                    protection_reason=getattr(db_pos, 'protection_reason', None)
                )
                
                await asyncio.to_thread(save_position, updated_pos)
                
                # Post-check: verify orders exist
                verified_count = None
                if execute:
                    # Refetch orders for verification
                    updated_orders = await client.get_futures_open_orders()
                    updated_tp_ids = [o.get('id') for o in updated_orders if o.get('id') in new_tp_ids]
                    verified_count = len(updated_tp_ids)
                
                logger.info(
                    "TP orders placed successfully",
                    symbol=symbol,
                    tp_order_ids=new_tp_ids,
                    verified_on_exchange=verified_count
                )
                
                success_count += 1
                
            except Exception as e:
                logger.error("Failed to place TP orders", symbol=symbol, error=str(e), exc_info=True)
                failed_count += 1
                skip_reasons['placement_failed'] = skip_reasons.get('placement_failed', 0) + 1
        
        except Exception as e:
            logger.error("Failed to process position", symbol=symbol, error=str(e), exc_info=True)
            failed_count += 1
            skip_reasons['processing_error'] = skip_reasons.get('processing_error', 0) + 1
    
    # Summary
    logger.info(
        "TP addition complete",
        total=len(positions_to_process),
        success=success_count,
        failed=failed_count,
        skipped=skipped_count,
        skip_reasons=skip_reasons,
        mode="EXECUTE" if execute else "DRY-RUN"
    )
    
    # Close client
    try:
        await client.close()
    except Exception as e:
        logger.warning("Failed to close client", error=str(e))


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Add TP orders to existing positions. Default: DRY-RUN (preview only).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry-run (default) - preview without placing orders
  python scripts/add_tp_to_positions.py
  
  # Dry-run for specific symbol
  python scripts/add_tp_to_positions.py --symbol BTCUSD-PERP
  
  # Execute for specific symbol (actually place orders)
  python scripts/add_tp_to_positions.py --execute --symbol BTCUSD-PERP
  
  # Execute for all positions (actually place orders)
  python scripts/add_tp_to_positions.py --execute
        """
    )
    
    parser.add_argument(
        '--execute',
        action='store_true',
        help='Actually place orders (default: False = dry-run)'
    )
    
    parser.add_argument(
        '--symbol',
        type=str,
        help='Process only specific symbol (e.g., BTCUSD-PERP)'
    )
    
    parser.add_argument(
        '--min-tp-count',
        type=int,
        default=2,
        help='Minimum TP orders to place (default: 2)'
    )
    
    parser.add_argument(
        '--limit',
        type=int,
        help='Limit number of positions to process'
    )
    
    parser.add_argument(
        '--require-sl',
        action='store_true',
        default=True,
        help='Require SL before placing TP (default: True)'
    )
    
    parser.add_argument(
        '--no-require-sl',
        dest='require_sl',
        action='store_false',
        help='Allow TP even without SL (not recommended)'
    )
    
    parser.add_argument(
        '--verbose',
        action='store_true',
        help='Verbose logging'
    )
    
    args = parser.parse_args()
    
    asyncio.run(add_tp_to_positions(
        execute=args.execute,
        require_sl=args.require_sl,
        min_tp_count=args.min_tp_count,
        symbol_filter=args.symbol,
        limit=args.limit,
        verbose=args.verbose
    ))


if __name__ == "__main__":
    main()
