#!/usr/bin/env python3
"""
Backfill initial_stop_price for existing positions.

CRITICAL: Never invents stops without placing them on exchange.
- If SL order exists → extract stop price, update DB
- If no SL order and place_missing_sl=False → mark UNPROTECTED, alert
- If no SL order and place_missing_sl=True → compute default, place SL order, then persist
"""
import sys
import os
import asyncio
from decimal import Decimal
from typing import List, Dict, Optional
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.storage.db import get_db
from src.storage.repository import PositionModel, save_position
from src.domain.models import Position, Side
from src.data.kraken_client import KrakenClient
from src.config.config import load_config
from src.monitoring.logger import get_logger
from src.execution.futures_adapter import FuturesAdapter
from src.execution.executor import Executor

logger = get_logger(__name__)


def detect_sl_order(orders: List[Dict], symbol: str) -> Optional[Dict]:
    """
    Detect stop loss order from open orders for a symbol.
    
    Returns first matching reduce-only stop order, or None.
    """
    for order in orders:
        if order.get('symbol') != symbol:
            continue
        
        is_reduce_only = order.get('reduceOnly', False)
        order_type = str(order.get('type', '')).lower()
        has_stop_price = order.get('stopPrice') is not None
        is_stop_type = any(stop_term in order_type for stop_term in ['stop', 'stop-loss', 'stop_loss', 'stp'])
        
        if is_reduce_only and (has_stop_price or is_stop_type):
            return order
    
    return None


async def backfill_initial_stop_prices(place_missing_sl: bool = False):
    """
    Backfill initial_stop_price for existing positions.
    
    Args:
        place_missing_sl: If True, place default 2% SL orders for unprotected positions.
                          If False, only mark as UNPROTECTED and alert.
    """
    logger.info("Starting initial_stop_price backfill", place_missing_sl=place_missing_sl)
    
    # Load config
    config = load_config()
    
    # Initialize client
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
    
    # 1. Load positions missing SL (extract data while in session)
    db = get_db()
    positions_data = []
    with db.get_session() as session:
        positions_missing_sl = session.query(PositionModel).filter(
            PositionModel.initial_stop_price.is_(None)
        ).all()
        
        # Extract all needed data while in session
        for pm in positions_missing_sl:
            import json
            positions_data.append({
                'symbol': pm.symbol,
                'side': pm.side,
                'size': pm.size,
                'size_notional': pm.size_notional,
                'entry_price': pm.entry_price,
                'current_mark_price': pm.current_mark_price,
                'liquidation_price': pm.liquidation_price,
                'unrealized_pnl': pm.unrealized_pnl,
                'leverage': pm.leverage,
                'margin_used': pm.margin_used,
                'opened_at': pm.opened_at,
                'initial_stop_price': pm.initial_stop_price,
                'stop_loss_order_id': pm.stop_loss_order_id,
                'tp_order_ids': json.loads(pm.tp_order_ids) if pm.tp_order_ids else [],
            })
    
    if not positions_data:
        logger.info("No positions missing initial_stop_price")
        return
    
    logger.info("Found positions missing initial_stop_price", count=len(positions_data))
    
    # 2. Fetch open orders once, index by symbol
    try:
        open_orders = await client.get_futures_open_orders()
        orders_by_symbol: Dict[str, List[Dict]] = {}
        for order in open_orders:
            sym = order.get('symbol')
            if sym:
                if sym not in orders_by_symbol:
                    orders_by_symbol[sym] = []
                orders_by_symbol[sym].append(order)
        logger.info("Fetched open orders", total_orders=len(open_orders), symbols_with_orders=len(orders_by_symbol))
    except Exception as e:
        logger.error("Failed to fetch open orders", error=str(e))
        return
    
    # 3. Process each position
    recovered_count = 0
    unprotected_count = 0
    placed_count = 0
    errors = []
    
    for pos_data_dict in positions_data:
        symbol = pos_data_dict['symbol']
        try:
            # Convert to Position object
            pos = Position(
                symbol=pos_data_dict['symbol'],
                side=Side(pos_data_dict['side']),
                size=Decimal(str(pos_data_dict['size'])),
                size_notional=Decimal(str(pos_data_dict['size_notional'])),
                entry_price=Decimal(str(pos_data_dict['entry_price'])),
                current_mark_price=Decimal(str(pos_data_dict['current_mark_price'])),
                liquidation_price=Decimal(str(pos_data_dict['liquidation_price'])),
                unrealized_pnl=Decimal(str(pos_data_dict['unrealized_pnl'])),
                leverage=Decimal(str(pos_data_dict['leverage'])),
                margin_used=Decimal(str(pos_data_dict['margin_used'])),
                opened_at=pos_data_dict['opened_at'].replace(tzinfo=timezone.utc) if isinstance(pos_data_dict['opened_at'], datetime) else datetime.now(timezone.utc),
                initial_stop_price=Decimal(str(pos_data_dict['initial_stop_price'])) if pos_data_dict['initial_stop_price'] else None,
                stop_loss_order_id=pos_data_dict['stop_loss_order_id'],
                tp_order_ids=pos_data_dict['tp_order_ids'],
            )
            
            # Try to recover from orders
            orders_for_symbol = orders_by_symbol.get(symbol, [])
            sl_order = detect_sl_order(orders_for_symbol, symbol)
            
            if sl_order:
                # Extract stop price
                stop_price = sl_order.get('stopPrice') or sl_order.get('price')
                if stop_price:
                    pos.initial_stop_price = Decimal(str(stop_price))
                    pos.stop_loss_order_id = sl_order.get('id')
                    pos.is_protected = True
                    pos.protection_reason = None
                    
                    save_position(pos)
                    recovered_count += 1
                    logger.info("Recovered SL from order", symbol=symbol, sl_price=str(pos.initial_stop_price), order_id=pos.stop_loss_order_id)
                    continue
            
            # No SL order found
            if place_missing_sl:
                # Option B: Compute default and place SL order
                entry = pos.entry_price
                if entry > 0:
                    # Compute 2% stop
                    stop_pct = Decimal("0.02")
                    if pos.side == Side.LONG:
                        default_sl = entry * (Decimal("1") - stop_pct)
                    else:
                        default_sl = entry * (Decimal("1") + stop_pct)
                    
                    # Place SL order on exchange
                    try:
                        # Get current price for size calculation
                        positions = await client.get_all_futures_positions()
                        pos_data = next((p for p in positions if p.get('symbol') == symbol), None)
                        if not pos_data:
                            logger.warning("Position not found on exchange", symbol=symbol)
                            errors.append({'symbol': symbol, 'error': 'Position not on exchange'})
                            continue
                        
                        # Calculate position size notional
                        current_price = Decimal(str(pos_data.get('markPrice', pos_data.get('mark_price', entry))))
                        position_size_notional = await futures_adapter.position_size_notional(
                            symbol=symbol,
                            pos_data=pos_data,
                            current_price=current_price
                        )
                        
                        if not position_size_notional or position_size_notional == 0:
                            logger.warning("Cannot place SL - zero position size", symbol=symbol)
                            errors.append({'symbol': symbol, 'error': 'Zero position size'})
                            continue
                        
                        # Place SL order
                        sl_order_id, _ = await executor.update_protective_orders(
                            symbol=symbol,
                            side=pos.side,
                            current_sl_id=None,
                            new_sl_price=default_sl,
                            current_tp_ids=[],
                            new_tp_prices=[],
                            position_size_notional=position_size_notional
                        )
                        
                        if sl_order_id:
                            pos.initial_stop_price = default_sl
                            pos.stop_loss_order_id = sl_order_id
                            pos.is_protected = True
                            pos.protection_reason = None
                            
                            save_position(pos)
                            placed_count += 1
                            logger.info("Placed default SL order", symbol=symbol, sl_price=str(default_sl), order_id=sl_order_id)
                        else:
                            logger.error("Failed to place SL order", symbol=symbol)
                            errors.append({'symbol': symbol, 'error': 'Failed to place SL order'})
                    except Exception as e:
                        logger.error("Failed to place SL order", symbol=symbol, error=str(e))
                        errors.append({'symbol': symbol, 'error': str(e)})
                else:
                    logger.warning("Cannot compute default SL - invalid entry price", symbol=symbol, entry=str(entry))
                    errors.append({'symbol': symbol, 'error': 'Invalid entry price'})
            else:
                # Option A: Mark UNPROTECTED
                pos.is_protected = False
                pos.protection_reason = "NO_SL_ORDER_OR_PRICE"
                
                save_position(pos)
                unprotected_count += 1
                logger.warning("Marked position as UNPROTECTED", symbol=symbol, reason=pos.protection_reason)
        
        except Exception as e:
            logger.error("Failed to process position", symbol=symbol, error=str(e))
            errors.append({'symbol': symbol, 'error': str(e)})
    
    # 4. Summary report
    logger.info(
        "Backfill complete",
        total=len(positions_data),
        recovered=recovered_count,
        placed=placed_count,
        unprotected=unprotected_count,
        errors=len(errors)
    )
    
    # Close client resources
    try:
        await client.close()
    except Exception as e:
        logger.warning("Failed to close client", error=str(e))
    
    if errors:
        logger.error("Errors during backfill", error_count=len(errors))
        for err in errors[:10]:
            logger.error("Backfill error", symbol=err['symbol'], error=err['error'])


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Backfill initial_stop_price for existing positions")
    parser.add_argument(
        "--place-missing-sl",
        action="store_true",
        help="Place default 2% SL orders for unprotected positions (default: only mark as UNPROTECTED)"
    )
    args = parser.parse_args()
    
    asyncio.run(backfill_initial_stop_prices(place_missing_sl=args.place_missing_sl))
