"""
Paper trading runtime.

Implements real-time trading with virtual execution.
"""
import asyncio
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional, List, Dict
import uuid

from src.config.config import Config
from src.monitoring.logger import get_logger
from src.data.kraken_client import KrakenClient
from src.strategy.smc_engine import SMCEngine
from src.risk.risk_manager import RiskManager
from src.risk.basis_guard import BasisGuard
from src.domain.models import Candle, Signal, SignalType, Position, Side, Trade
from src.storage.repository import save_candle, save_trade, save_position, delete_position, get_candles

logger = get_logger(__name__)


class PaperTrading:
    """
    Paper trading engine.
    
    Mimics BacktestEngine logic but operates in real-time loop.
    """
    
    def __init__(self, config: Config):
        """Initialize paper trading."""
        self.config = config
        
        # Components
        self.client = KrakenClient(
            api_key=config.exchange.api_key if hasattr(config.exchange, "api_key") else "",
            api_secret=config.exchange.api_secret if hasattr(config.exchange, "api_secret") else "",
        )
        self.smc_engine = SMCEngine(config.strategy)
        self.risk_manager = RiskManager(config.risk)
        self.basis_guard = BasisGuard(config.risk)
        
        # State
        self.current_equity = Decimal("10000") # Default start
        self.position: Optional[Position] = None
        self.active = False
        
        # Cache for indicators
        self.candles_15m: List[Candle] = []
        self.candles_1h: List[Candle] = []
        self.candles_4h: List[Candle] = []
        self.candles_1d: List[Candle] = []
        
        logger.info("Paper Trading initialized")
    
    async def run(self):
        """Run the main paper trading loop."""
        self.active = True
        logger.info("Starting Paper Trading Loop...")
        
        try:
            # 1. Warmup
            await self._warmup()
            
            # 2. Main Loop
            while self.active:
                loop_start = datetime.now(timezone.utc)
                
                try:
                    await self._tick()
                except Exception as e:
                    logger.error("Error in paper trading tick", error=str(e))
                
                # Sleep until next minute check (simplified)
                # In production, use robust scheduler
                elapsed = (datetime.now(timezone.utc) - loop_start).total_seconds()
                sleep_time = max(10.0, 60.0 - elapsed)
                await asyncio.sleep(sleep_time)
                
        except asyncio.CancelledError:
            logger.info("Paper trading stopped")
        finally:
            await self.client.close()
    
    async def _warmup(self):
        """Fetch historical data to prime indicators."""
        logger.info("Warming up indicators (300 days)...")
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=300)
        
        # Fetch data
        # Note: In real app, run these concurrently
        self.candles_1d = await self._fetch_history("1d", start, end)
        self.candles_4h = await self._fetch_history("4h", start, end)
        self.candles_1h = await self._fetch_history("1h", start, end)
        self.candles_15m = await self._fetch_history("15m", start, end)
        
        logger.info("Warmup complete")
    
    async def _fetch_history(self, timeframe: str, start: datetime, end: datetime) -> List[Candle]:
        """Fetch history helper."""
        # Reuse backtest fetching logic if possible, or simple fetch here
        # For paper, we just need recent history
        # Simplified: fetch last 1000 candles from API
        # To be robust, should use same logic as backtest (DB + API)
        # Using client directly for now
        since = int(start.timestamp() * 1000)
        candles = await self.client.get_spot_ohlcv("BTC/USD", timeframe, since=since)
        return candles

    async def _tick(self):
        """Single iteration of trading logic."""
        symbol = "BTC/USD"
        
        # 1. Fetch latest state
        # Get latest 1m ticker or candle for price check
        ticker = await self.client.get_spot_ticker(symbol)
        current_price = Decimal(str(ticker['last']))
        now = datetime.now(timezone.utc)
        
        # 2. Update Candle Buffers (Poll for new candles)
        # Simplified: Fetch latest 5 candles for each TF and append if new
        await self._update_candles(symbol)
        
        # 3. Check Exits (SL/TP)
        if self.position:
            # Create a synthetic candle for the current minute to check exits
            # In real system, stream trades. Here we use 1m resolution check.
            dummy_candle = Candle(
                timestamp=now,
                symbol=symbol,
                timeframe="1m",
                open=current_price,
                high=current_price, # Optimistic/Pessimistic check needed?
                low=current_price,
                close=current_price,
                volume=Decimal("0")
            )
            
            # Check exit
            self._check_exit(self.position, dummy_candle)
            
        # 4. Generate Signals (Only if no position)
        if not self.position:
            signal = self.smc_engine.generate_signal(
                symbol,
                bias_candles_4h=self.candles_4h,
                bias_candles_1d=self.candles_1d,
                exec_candles_15m=self.candles_15m,
                exec_candles_1h=self.candles_1h
            )
            
            if signal.signal_type != SignalType.NO_SIGNAL:
                logger.info("Signal detected", type=signal.signal_type.value, price=current_price)
                await self._process_signal(signal, current_price)
    
    async def _update_candles(self, symbol: str):
        """Update local candle buffers."""
        # For each timeframe, fetch recent and update list
        # Optimization: Only fetch if enough time passed
        # Here: Naive fetch latest
        
        async def update_tf(tf: str, buffer: List[Candle]):
            latest = await self.client.get_spot_ohlcv(symbol, tf, limit=5)
            if not latest:
                return
            
            # Append new ones
            last_ts = buffer[-1].timestamp if buffer else datetime.min.replace(tzinfo=timezone.utc)
            for c in latest:
                if c.timestamp > last_ts:
                    buffer.append(c)
                    save_candle(c) # Persist
                    
            # Trim buffer
            if len(buffer) > 500:
                buffer[:] = buffer[-500:]
                
        await  update_tf("15m", self.candles_15m)
        await  update_tf("1h", self.candles_1h)
        await  update_tf("4h", self.candles_4h)
        await  update_tf("1d", self.candles_1d)

    async def _process_signal(self, signal: Signal, current_price: Decimal):
        """Execute virtual trade."""
        # Logic same as BacktestEngine
        
        # Risk Check
        decision = self.risk_manager.validate_trade(
            signal, self.current_equity, current_price, current_price
        )
        
        if not decision.approved:
            logger.info("Signal rejected by risk", reason=decision.rejection_reasons)
            return
            
        # Execute
        # Simulate fills
        taker_fee_bps = Decimal("5") # 0.05%
        slippage_bps = Decimal("2")
        
        total_cost_bps = taker_fee_bps + slippage_bps
        entry_price = current_price
        
        if signal.signal_type == SignalType.LONG:
            fill_price = entry_price * (Decimal("1") + total_cost_bps/Decimal("10000"))
        else:
            fill_price = entry_price * (Decimal("1") - total_cost_bps/Decimal("10000"))
            
         # Calculate liquidation price
        maint_margin = Decimal("0.02")
        if signal.signal_type == SignalType.LONG:
             liq_price = fill_price * (Decimal("1") - (Decimal("1")/decision.leverage) + maint_margin)
        else:
             liq_price = fill_price * (Decimal("1") + (Decimal("1")/decision.leverage) - maint_margin)
             
        self.position = Position(
            symbol=signal.symbol,
            side=Side.LONG if signal.signal_type == SignalType.LONG else Side.SHORT,
            size=decision.position_notional / fill_price,
            size_notional=decision.position_notional,
            entry_price=fill_price,
            current_mark_price=fill_price,
            liquidation_price=liq_price,
            unrealized_pnl=Decimal("0"),
            leverage=decision.leverage,
            margin_used=decision.margin_required,
            stop_loss_order_id=f"SL-{signal.stop_loss}",
            take_profit_order_id=f"TP-{signal.take_profit}" if signal.take_profit else None,
            opened_at=datetime.now(timezone.utc)
        )
        
        save_position(self.position)
        logger.info("Virtual Position Opened", side=self.position.side.value, size=str(self.position.size_notional))
        
    def _check_exit(self, position: Position, candle: Candle) -> bool:
        """Check exits."""
        # Parsing logic from BacktestEngine
        stop_loss = Decimal(position.stop_loss_order_id.split("-")[1]) if position.stop_loss_order_id else None
        take_profit = None
        if position.take_profit_order_id:
            take_profit = Decimal(position.take_profit_order_id.split("-")[1])
            
        # Use simple Close vs Level check for now (since we use ticker price as close)
        price = candle.close
        
        if position.side == Side.LONG:
            if stop_loss and price <= stop_loss:
                self._close_position(stop_loss, "stop_loss")
                return True
            if take_profit and price >= take_profit:
                self._close_position(take_profit, "take_profit")
                return True
        else:
            if stop_loss and price >= stop_loss:
                self._close_position(stop_loss, "stop_loss")
                return True
            if take_profit and price <= take_profit:
                self._close_position(take_profit, "take_profit")
                return True
        return False

    def _close_position(self, price: Decimal, reason: str):
        """Close virtual position."""
        if not self.position: return
        
        if self.position.side == Side.LONG:
            pnl = (price - self.position.entry_price) * self.position.size
        else:
            pnl = (self.position.entry_price - price) * self.position.size
            
        # Fees
        fees = (price * self.position.size) * Decimal("0.0005")
        net_pnl = pnl - fees
        
        self.current_equity += net_pnl
        self.risk_manager.record_trade_result(net_pnl)
        
        logger.info("Virtual Position Closed", reason=reason, pnl=str(net_pnl), equity=str(self.current_equity))
        
        # Save trade
        trade = Trade(
            trade_id=str(uuid.uuid4()),
            symbol=self.position.symbol,
            side=self.position.side,
            entry_price=self.position.entry_price,
            exit_price=price,
            size_notional=self.position.size_notional,
            leverage=self.position.leverage,
            gross_pnl=pnl,
            fees=fees,
            funding=Decimal("0"),
            net_pnl=net_pnl,
            entered_at=self.position.opened_at,
            exited_at=datetime.now(timezone.utc),
            holding_period_hours=Decimal("0"), # TODO
            exit_reason=reason
        )
        save_trade(trade)
        delete_position(self.position.symbol)
        
        self.position = None
