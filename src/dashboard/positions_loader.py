"""
Load active positions for dashboard display.
"""
from typing import List, Dict, Optional
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from src.storage.db import get_db
from src.storage.repository import PositionModel
from src.monitoring.logger import get_logger

logger = get_logger(__name__)


def load_active_positions() -> List[Dict]:
    """
    Load all active positions for dashboard display.
    
    Returns:
        List of position dictionaries with formatted data
    """
    try:
        db = get_db()
        with db.get_session() as session:
            position_models = session.query(PositionModel).all()
            
            positions = []
            now = datetime.now(timezone.utc)
            
            for pm in position_models:
                # Calculate change % from entry
                entry_price = float(pm.entry_price)
                current_price = float(pm.current_mark_price)
                
                if pm.side.upper() == 'LONG':
                    change_pct = ((current_price - entry_price) / entry_price) * 100
                else:  # SHORT
                    change_pct = ((entry_price - current_price) / entry_price) * 100
                
                # Convert futures symbol to spot symbol for display
                spot_symbol = pm.symbol.replace(":USD", "/USD").replace("PF_", "")
                
                # Calculate holding time
                opened_at = pm.opened_at.replace(tzinfo=timezone.utc) if pm.opened_at else now
                holding_time = now - opened_at
                hours = holding_time.total_seconds() / 3600
                
                position_data = {
                    'symbol': spot_symbol,
                    'futures_symbol': pm.symbol,
                    'side': pm.side.upper(),
                    'entry_price': float(entry_price),
                    'current_price': float(current_price),
                    'change_pct': change_pct,
                    'unrealized_pnl': float(pm.unrealized_pnl),
                    'size_notional': float(pm.size_notional),
                    'leverage': float(pm.leverage),
                    'opened_at': opened_at,
                    'holding_hours': hours,
                    'stop_loss_order_id': pm.stop_loss_order_id,
                    'take_profit_order_id': pm.take_profit_order_id,
                    'liquidation_price': float(pm.liquidation_price) if pm.liquidation_price is not None else None,
                    'margin_used': float(pm.margin_used) if pm.margin_used else 0.0,
                    # Position fields
                    'initial_stop_price': float(pm.initial_stop_price) if pm.initial_stop_price else None,
                    'tp1_price': float(pm.tp1_price) if pm.tp1_price else None,
                    'tp2_price': float(pm.tp2_price) if pm.tp2_price else None,
                    'final_target_price': float(pm.final_target_price) if pm.final_target_price else None,
                }
                
                positions.append(position_data)
            
            # Sort by opening time (newest first)
            positions.sort(key=lambda x: x['opened_at'], reverse=True)
            
            logger.debug(f"Loaded {len(positions)} active positions for dashboard")
            return positions
            
    except Exception as e:
        logger.error("Failed to load active positions", error=str(e))
        return []
