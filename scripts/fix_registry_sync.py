"""
Quick fix script to sync position registry with exchange.

Manually imports positions that exist on exchange but not in registry.
"""
import asyncio
import os
import sys
from decimal import Decimal
from datetime import datetime, timezone

sys.path.insert(0, os.getcwd())

from src.config.config import load_config
from src.data.kraken_client import KrakenClient
from src.execution.position_state_machine import (
    ManagedPosition,
    PositionState,
    PositionRegistry,
    FillRecord,
    Side,
    get_position_registry,
)
from src.monitoring.logger import get_logger

logger = get_logger(__name__)


async def main():
    print("=" * 60)
    print("REGISTRY SYNC FIX")
    print("=" * 60)
    
    # Load config
    config = load_config()
    
    # Initialize client with config credentials
    client = KrakenClient(
        api_key=config.exchange.api_key,
        api_secret=config.exchange.api_secret,
        futures_api_key=config.exchange.futures_api_key,
        futures_api_secret=config.exchange.futures_api_secret,
        use_testnet=config.exchange.use_testnet,
    )
    await client.initialize()
    
    # Get exchange positions
    print("\nFetching exchange positions...")
    positions = await client.get_all_futures_positions()
    orders = await client.get_futures_open_orders()
    
    print(f"Found {len(positions)} positions on exchange")
    
    # Get registry
    registry = get_position_registry()
    print(f"Registry has {len(registry.get_all_active())} active positions")
    
    # Find positions that need to be imported
    for pos in positions:
        symbol = pos.get("symbol", "")
        size = abs(float(pos.get("size", 0)))
        
        if size == 0:
            continue
            
        # Check if in registry
        reg_pos = registry.get_position(symbol)
        if reg_pos and reg_pos.remaining_qty > 0:
            print(f"  {symbol}: Already in registry (qty={reg_pos.remaining_qty})")
            continue
        
        # Need to import
        print(f"\n  {symbol}: NEEDS IMPORT (exchange has {size})")
        
        # Get position details
        side_str = pos.get("side", "long").lower()
        side = Side.LONG if side_str == "long" else Side.SHORT
        entry_price = Decimal(str(pos.get("entryPrice", pos.get("entry_price", 0))))
        qty = Decimal(str(size))
        
        print(f"    Side: {side}, Entry: {entry_price}, Qty: {qty}")
        
        # Find stop order for this position
        stop_order = None
        for order in orders:
            order_symbol = order.get("symbol", "")
            order_type = order.get("type", "").lower()
            is_reduce = order.get("reduceOnly", False)
            
            # Match by symbol (normalize both)
            sym_match = (
                symbol.replace("PF_", "").replace("USD", "") in 
                order_symbol.replace("/USD:USD", "").replace("/USD", "")
            )
            
            if sym_match and "stop" in order_type and is_reduce:
                stop_order = order
                break
        
        if stop_order:
            stop_price = Decimal(str(stop_order.get("stopPrice", stop_order.get("price", 0))))
            stop_id = stop_order.get("id", "")
            print(f"    Found stop: {stop_price} (id={stop_id})")
        else:
            # Calculate a default stop (2% from entry)
            pct = Decimal("0.02")
            if side == Side.LONG:
                stop_price = entry_price * (1 - pct)
            else:
                stop_price = entry_price * (1 + pct)
            stop_id = None
            print(f"    No stop found, using default: {stop_price}")
        
        # Create position
        pid = f"pos-{symbol.replace('/', '')}-sync-{int(datetime.now().timestamp())}"
        
        managed_pos = ManagedPosition(
            symbol=symbol,
            side=side,
            position_id=pid,
            initial_size=qty,
            initial_entry_price=entry_price,
            initial_stop_price=stop_price,
            initial_tp1_price=None,
            initial_tp2_price=None,
            initial_final_target=None,
        )
        
        # Set state
        managed_pos.entry_acknowledged = True
        managed_pos.intent_confirmed = True
        managed_pos.state = PositionState.PROTECTED if stop_id else PositionState.PENDING_PROTECTION
        managed_pos.current_stop_price = stop_price
        managed_pos.stop_order_id = stop_id
        managed_pos.setup_type = "SYNC_IMPORT"
        managed_pos.trade_type = "UNKNOWN"
        
        # Add dummy fill
        fill = FillRecord(
            fill_id=f"sync-fill-{pid}",
            order_id="SYNC_IMPORT",
            side=side,
            qty=qty,
            price=entry_price,
            timestamp=datetime.now(timezone.utc),
            is_entry=True,
        )
        managed_pos.entry_fills.append(fill)
        
        # Register
        try:
            registry.register_position(managed_pos)
            print(f"    ✅ Registered {symbol} in registry")
        except Exception as e:
            print(f"    ❌ Failed to register: {e}")
    
    await client.close()
    
    print("\n" + "=" * 60)
    print(f"Registry now has {len(registry.get_all_active())} active positions")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
