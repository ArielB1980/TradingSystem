"""
Test indicator calculations for correctness.
"""
import pytest
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from src.domain.models import Candle
from src.strategy.indicators import Indicators


def create_test_candles(count: int = 50, base_price: float = 50000.0) -> list:
    """Create test candles with predictable pattern."""
    candles = []
    base_time = datetime(2024, 1, 1, tzinfo=timezone.utc)
    
    for i in range(count):
        # Simple uptrend
        close = base_price + (i * 100)
        candles.append(
            Candle(
                timestamp=base_time + timedelta(hours=i),
                symbol="BTC/USD",
                timeframe="1h",
                open=Decimal(str(close - 50)),
                high=Decimal(str(close + 100)),
                low=Decimal(str(close - 100)),
                close=Decimal(str(close)),
                volume=Decimal("100"),
            )
        )
    
    return candles


def test_ema_calculation():
    """Test EMA calculation."""
    candles = create_test_candles(count=50)
    
    ema = Indicators.calculate_ema(candles, period=20)
    
    assert len(ema) == 50
    assert ema.iloc[-1] > 0  # Should have a value
    
    # In uptrend, EMA should generally increase
    assert ema.iloc[-1] > ema.iloc[0]


def test_atr_calculation():
    """Test ATR calculation."""
    candles = create_test_candles(count=30)
    
    atr = Indicators.calculate_atr(candles, period=14)
    
    assert len(atr) == 30
    assert atr.iloc[-1] > 0  # Should have a value
    

def test_rsi_calculation():
    """Test RSI calculation."""
    candles = create_test_candles(count=30)
    
    rsi = Indicators.calculate_rsi(candles, period=14)
    
    assert len(rsi) == 30
    # RSI should be between 0 and 100
    assert 0 <= rsi.iloc[-1] <= 100
    
    # In uptrend, RSI should be > 50
    assert rsi.iloc[-1] > 50


def test_adx_calculation():
    """Test ADX calculation."""
    candles = create_test_candles(count=50)
    
    adx_df = Indicators.calculate_adx(candles, period=14)
    
    assert 'ADX_14' in adx_df.columns
    assert len(adx_df) == 50
    assert adx_df['ADX_14'].iloc[-1] > 0  # Should have a value


def test_ema_slope():
    """Test EMA slope detection."""
    candles_up = create_test_candles(count=30, base_price=50000)
    candles_down = create_test_candles(count=30, base_price=60000)
    
    # Reverse downtrend candles
    for i, c in enumerate(candles_down):
        close = 60000 - (i * 100)
        candles_down[i] = Candle(
            timestamp=c.timestamp,
            symbol=c.symbol,
            timeframe=c.timeframe,
            open=Decimal(str(close + 50)),
            high=Decimal(str(close + 100)),
            low=Decimal(str(close - 100)),
            close=Decimal(str(close)),
            volume=c.volume,
        )
    
    ema_up = Indicators.calculate_ema(candles_up, period=10)
    ema_down = Indicators.calculate_ema(candles_down, period=10)
    
    slope_up = Indicators.get_ema_slope(ema_up)
    slope_down = Indicators.get_ema_slope(ema_down)
    
    assert slope_up == "up"
    assert slope_down == "down"
