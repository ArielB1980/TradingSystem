"""
Spot-to-futures price conversion.

Implements percentage-based conversion: spot levels → futures mark price distances.
"""
from decimal import Decimal
from src.domain.models import Signal, OrderIntent, Side
from src.monitoring.logger import get_logger

logger = get_logger(__name__)


class PriceConverter:
    """
    Convert spot-derived levels to futures order prices.
    
    Default method: Percentage distances from spot entry applied to futures mark price.
    """
    
    @staticmethod
    def convert_signal_to_futures(
        signal: Signal,
        futures_mark_price: Decimal,
        position_notional: Decimal,
        leverage: Decimal,
    ) -> OrderIntent:
        """
        Convert spot signal to futures order intent.
        
        Args:
            signal: Signal from spot analysis
            futures_mark_price: Current futures mark price
            position_notional: Position size in USD notional
            leverage: Actual leverage to use
        
        Returns:
            OrderIntent with futures prices
        """
        # Calculate percentage distances from spot entry
        entry_price_spot = signal.entry_price
        stop_distance_pct = abs(entry_price_spot - signal.stop_loss) / entry_price_spot
        
        if signal.take_profit:
            tp_distance_pct = abs(signal.take_profit - entry_price_spot) / entry_price_spot
        else:
            tp_distance_pct = None
        
        # Apply distances to futures mark price
        # Entry at current mark (or slightly better if using limit orders)
        entry_price_futures = futures_mark_price
        
        # Determine side
        if signal.signal_type.value in ["long", "exit_short"]:
            side = Side.LONG
            # Stop below entry
            stop_loss_futures = futures_mark_price * (Decimal("1") - stop_distance_pct)
            # TP above entry
            if tp_distance_pct:
                take_profit_futures = futures_mark_price * (Decimal("1") + tp_distance_pct)
            else:
                take_profit_futures = None
        else:  # short or exit_long
            side = Side.SHORT
            # Stop above entry
            stop_loss_futures = futures_mark_price * (Decimal("1") + stop_distance_pct)
            # TP below entry
            if tp_distance_pct:
                take_profit_futures = futures_mark_price * (Decimal("1") - tp_distance_pct)
            else:
                take_profit_futures = None
        
        logger.info(
            "Price conversion complete",
            symbol=signal.symbol,
            spot_entry=str(entry_price_spot),
            futures_entry=str(entry_price_futures),
            stop_distance_pct=f"{stop_distance_pct:.2%}",
        )
        
        return OrderIntent(
            timestamp=signal.timestamp,
            signal=signal,
            side=side,
            size_notional=position_notional,
            leverage=leverage,
            entry_price_spot=entry_price_spot,
            stop_loss_spot=signal.stop_loss,
            take_profit_spot=signal.take_profit,
            # Converted Futures Prices
            entry_price_futures=entry_price_futures,
            stop_loss_futures=stop_loss_futures,
            take_profit_futures=take_profit_futures
        )
