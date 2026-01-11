"""
Backtesting engine for SMC strategy on historical spot data.

Simulates futures execution with realistic costs.
"""
from datetime import datetime, timezone, timedelta
import asyncio
from decimal import Decimal
from typing import List, Dict, Optional
from dataclasses import dataclass, field
from src.config.config import Config
from src.data.kraken_client import KrakenClient
from src.strategy.smc_engine import SMCEngine
from src.risk.risk_manager import RiskManager
from src.risk.basis_guard import BasisGuard
from src.domain.models import Candle, Signal, SignalType, Position, Trade, Side
from src.storage.repository import save_candle, save_trade
from src.storage.db import init_db
from src.monitoring.logger import get_logger

logger = get_logger(__name__)


from src.execution.execution_engine import ExecutionEngine

@dataclass
class BacktestMetrics:
    """Performance metrics for backtest."""
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: Decimal = Decimal("0")
    total_fees: Decimal = Decimal("0")
    max_drawdown: Decimal = Decimal("0")
    win_rate: float = 0.0
    avg_win: Decimal = Decimal("0")
    avg_loss: Decimal = Decimal("0")
    profit_factor: float = 0.0
    sharpe_ratio: float = 0.0
    
    # Tracking
    equity_curve: List[Decimal] = field(default_factory=list)
    peak_equity: Decimal = Decimal("0")
    
    def update(self):
        """Update calculated metrics."""
        if self.total_trades > 0:
            self.win_rate = (self.winning_trades / self.total_trades) * 100
        
        if self.winning_trades > 0 and self.losing_trades > 0:
            # Simplified tracking - would need list of trade results for accurate avg/sharpe
            pass 


class BacktestEngine:
    """
    Backtest engine for SMC strategy.
    
    Workflow:
    1. Fetch historical spot data (multiple timeframes)
    2. Replay chronologically
    3. Generate signals on each new candle
    4. Simulate futures fills with realistic costs
    5. Track performance
    """
    
    def __init__(self, config: Config, kraken_client: KrakenClient):
        """Initialize backtest engine."""
        self.config = config
        self.client = kraken_client
        
        # Initialize components
        self.smc_engine = SMCEngine(config.strategy)
        self.risk_manager = RiskManager(config.risk)
        self.basis_guard = BasisGuard(config.risk)
        self.execution = ExecutionEngine(config)
        
        # Backtest state
        self.starting_equity = Decimal(str(config.backtest.starting_equity))
        self.current_equity = self.starting_equity
        self.position: Optional[Position] = None
        self.metrics = BacktestMetrics()
        self.metrics.equity_curve.append(self.starting_equity)
        self.metrics.peak_equity = self.starting_equity
        
        # Cost assumptions
        self.taker_fee_bps = Decimal(str(config.backtest.taker_fee_bps))
        self.slippage_bps = Decimal(str(config.backtest.slippage_bps))
        
        logger.info("Backtest engine initialized")
    
    async def run(
        self,
        symbol: str,
        start_date: datetime,
        end_date: datetime,
    ) -> BacktestMetrics:
        """Run backtest for given date range."""
        logger.info("Starting backtest", symbol=symbol, start=start_date.isoformat(), end=end_date.isoformat())
        
        # Calculate warmup period (need ~200 days for daily EMA)
        data_start = start_date - timedelta(days=300)
        logger.info("Fetching historical data...", data_start=data_start.isoformat())
        
        # Fetch data
        candles_1d = await self._fetch_historical(symbol, "1d", data_start, end_date)
        candles_4h = await self._fetch_historical(symbol, "4h", data_start, end_date)
        candles_1h = await self._fetch_historical(symbol, "1h", data_start, end_date)
        candles_15m = await self._fetch_historical(symbol, "15m", data_start, end_date)
        
        logger.info("Data fetched")
        
        # Replay chronologically (use 1h as main timeline)
        for i, current_candle in enumerate(candles_1h):
            cutoff_time = current_candle.timestamp
            
            # Historical candles for signal generation
            hist_1d = [c for c in candles_1d if c.timestamp <= cutoff_time]
            hist_4h = [c for c in candles_4h if c.timestamp <= cutoff_time]
            hist_1h = [c for c in candles_1h if c.timestamp <= cutoff_time]
            hist_15m = [c for c in candles_15m if c.timestamp <= cutoff_time]
            
            # Need enough history for indicators
            if len(hist_1d) < 200 or len(hist_1h) < 200:
                continue 
                
            # Wait until requested simulation start
            if current_candle.timestamp < start_date:
                continue
            
            # Check existing position
            if self.position:
                # Accrue funding costs (simulated every hour, applied 3x daily in reality)
                # Funding typically charged every 8 hours
                hours_since_open = (current_candle.timestamp - self.position.opened_at).total_seconds() / 3600
                funding_intervals = int(hours_since_open / 8)  # Every 8 hours
                
                if not hasattr(self.position, '_last_funding_interval'):
                    self.position._last_funding_interval = 0
                
                # Apply funding if we crossed a new 8-hour interval
                if funding_intervals > self.position._last_funding_interval:
                    intervals_to_charge = funding_intervals - self.position._last_funding_interval
                    
                    # Funding rate per 8h interval = daily_bps / 3
                    funding_rate_per_interval = Decimal(str(self.config.risk.funding_rate_daily_bps)) / Decimal("3") / Decimal("10000")
                    funding_cost = self.position.size_notional * funding_rate_per_interval * intervals_to_charge
                    
                    # Deduct from equity
                    self.current_equity -= funding_cost
                    self.metrics.total_fees += funding_cost  # Track as fee
                    
                    self.position._last_funding_interval = funding_intervals
                    
                    logger.debug(
                        "Funding cost applied",
                        intervals=intervals_to_charge,
                        cost=str(funding_cost),
                        total_fees=str(self.metrics.total_fees)
                    )
                
                # Simulate updates
                # Use current candle High/Low/Close to simulate price movement within the hour
                # Ideally we check High/Low for Exits, and Close for trailing updates?
                # Simplified: Check exits first based on High/Low.
                # If safe, update Trailing based on Close.
                
                filled = self._check_exit(self.position, current_candle)
                
                if not filled and self.position: # If still open
                     # Update State (Trailing/BE)
                     # Using candle close as 'current price' for state update
                     spot_price = current_candle.close
                     
                     # Update Peak
                     if self.position.side == Side.LONG:
                         if spot_price > (self.position.peak_price or self.position.entry_price):
                             self.position.peak_price = spot_price
                     else:
                         if spot_price < (self.position.peak_price or self.position.entry_price):
                             self.position.peak_price = spot_price
                     
                     # Check Trailing
                     if self.position.trailing_active:
                         # Calculate ATR using historical context
                         atr_val = self.smc_engine.indicators.calculate_atr(hist_1h, 14).iloc[-1]
                         current_sl = Decimal(self.position.stop_loss_order_id.split("-")[1])
                         
                         new_sl = self.execution.check_trailing_stop(
                             self.position,
                             spot_price,
                             Decimal(str(atr_val)),
                             spot_price,
                             current_sl
                         )
                         if new_sl:
                             self.position.stop_loss_order_id = f"SL-{new_sl}"
                             # logger.debug(f"Trailing SL Updated: {new_sl}")

            # Generate signal (only if no position)
            if not self.position:
                signal = self.smc_engine.generate_signal(
                    symbol,
                    bias_candles_4h=hist_4h,
                    bias_candles_1d=hist_1d,
                    exec_candles_15m=hist_15m,
                    exec_candles_1h=hist_1h,
                )
                
                # Process signal
                if signal.signal_type != SignalType.NO_SIGNAL:
                    await self._process_signal(signal, current_candle)
            
            # Update equity curve (Daily snapshot)
            if i % 24 == 0:
                self.metrics.equity_curve.append(self.current_equity)
                if self.current_equity > self.metrics.peak_equity:
                    self.metrics.peak_equity = self.current_equity
                else:
                    drawdown = (self.metrics.peak_equity - self.current_equity) / self.metrics.peak_equity
                    if drawdown > self.metrics.max_drawdown:
                        self.metrics.max_drawdown = drawdown
        
        # Finalize metrics
        self.metrics.update()
        
        logger.info(
            "Backtest complete",
            trades=self.metrics.total_trades,
            win_rate=f"{self.metrics.win_rate:.1f}%",
            total_pnl=str(self.metrics.total_pnl),
            max_dd=f"{self.metrics.max_drawdown:.1%}",
        )
        
        return self.metrics
    
    async def _fetch_historical(
        self,
        symbol: str,
        timeframe: str,
        start_date: datetime,
        end_date: datetime,
    ) -> List[Candle]:
        """Fetch historical OHLCV data with database caching."""
        from src.storage.repository import get_candles, save_candles_bulk
        
        # 1. Attempt DB Load
        db_candles = get_candles(symbol, timeframe, start_date, end_date)
        
        total_seconds = (end_date - start_date).total_seconds()
        interval_seconds = self._timeframe_to_seconds(timeframe)
        expected_count = total_seconds / interval_seconds
        
        if len(db_candles) >= expected_count * 0.95:
            return db_candles
            
        logger.info("Cache miss - fetching from API", found=len(db_candles), timeframe=timeframe)

        # 2. Fetch from API (Throttled)
        candles = []
        since = int(start_date.timestamp() * 1000)
        end_ts = int(end_date.timestamp() * 1000)
        
        while since < end_ts:
            await asyncio.sleep(0.5) # Faster for backtest
            try:
                batch = await self.client.get_spot_ohlcv(symbol, timeframe, since=since, limit=720)
            except Exception as e:
                logger.warning("Rate limit hit", error=str(e))
                await asyncio.sleep(5.0)
                continue

            if not batch:
                break
            
            candles.extend(batch)
            since = int(batch[-1].timestamp.timestamp() * 1000) + 1
            save_candles_bulk(batch)
        
        return candles

    def _timeframe_to_seconds(self, tf: str) -> int:
        """Helper to estimate candle count."""
        unit = tf[-1]
        value = int(tf[:-1])
        if unit == 'm': return value * 60
        if unit == 'h': return value * 3600
        if unit == 'd': return value * 86400
        return 60
    
    async def _process_signal(self, signal: Signal, current_candle: Candle):
        """Process trading signal and simulate entry."""
        futures_mark = current_candle.close
        spot_price = current_candle.close
        
        # Risk validation
        decision = self.risk_manager.validate_trade(
            signal, self.current_equity, spot_price, futures_mark
        )
        
        if not decision.approved:
            return
        
        # Execute using Engine
        plan = self.execution.generate_entry_plan(
            signal, 
            decision.position_notional, 
            spot_price, 
            futures_mark, 
            decision.leverage
        )
        
        # Simulate Entry Fill
        entry_intent = plan['entry']
        fill_price = entry_intent['price'] if entry_intent['price'] else futures_mark
        
        # Add costs
        total_cost_bps = self.taker_fee_bps + self.slippage_bps
        cost_mult = Decimal("1") + (total_cost_bps / Decimal("10000"))
        if signal.signal_type == SignalType.LONG:
            fill_price_w_cost = fill_price * cost_mult
        else:
            fill_price_w_cost = fill_price / cost_mult # Price is better for short but cost makes it worse? No, buying back higher.
            # Entry Short: Sell. Price received = Price * (1 - cost). 
            # Entry Long: Buy. Price paid = Price * (1 + cost).
            # Wait, `entry_price` in position usually tracks the "average entry price" for PnL.
            # If I sell at 100, fees deducted from margin usually, but effective price?
            # Let's track raw Fill Price and track fees separately in metrics.
            fill_price_w_cost = fill_price # Keep raw price for PnL base
        
        # Start Fee
        entry_fees = decision.position_notional * (self.taker_fee_bps / Decimal("10000"))
        self.metrics.total_fees += entry_fees
        
        # Calculate liquidation price
        maint_margin = Decimal("0.02")
        if signal.signal_type == SignalType.LONG:
             liq_price = fill_price * (Decimal("1") - (Decimal("1")/decision.leverage) + maint_margin)
        else:
             liq_price = fill_price * (Decimal("1") + (Decimal("1")/decision.leverage) - maint_margin)

        # Create TP IDs
        tp_ids = []
        for i, tp in enumerate(plan['take_profits']):
             tp_ids.append(f"TP-{i}-{tp['price']}-{tp['qty']}")
             
        sl_price = plan['stop_loss']['price']
        sl_id = f"SL-{sl_price}"

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
            stop_loss_order_id=sl_id,
            take_profit_order_id=None,
            tp_order_ids=tp_ids,
            trailing_active=False,
            break_even_active=False,
            peak_price=fill_price,
            opened_at=current_candle.timestamp
        )
        
        # logger.info("Position opened", side=self.position.side.value, size=str(decision.position_notional))
    
    def _check_exit(self, position: Position, candle: Candle) -> bool:
        """Check if position hit stop-loss or take-profit."""
        # Check SL (High/Low)
        stop_loss = Decimal(position.stop_loss_order_id.split("-")[1])
        
        hit_sl = False
        if position.side == Side.LONG:
            if candle.low <= stop_loss:
                hit_sl = True
        else:
            if candle.high >= stop_loss:
                hit_sl = True
        
        if hit_sl:
            self._close_position(stop_loss, "stop_loss", candle.timestamp, position.size)
            return True
            
        # Check TPs (Partial)
        remaining_tps = []
        hits = 0
        current_price_high = candle.high
        current_price_low = candle.low
        
        # Copy list to iterate safely
        tps = list(position.tp_order_ids)
        
        for tp_id in tps:
            parts = tp_id.split("-")
            price = Decimal(parts[2])
            qty = Decimal(parts[3])
            
            # Check if this TP is hit in this candle
            hit_tp = False
            if position.side == Side.LONG:
                if current_price_high >= price:
                    hit_tp = True
            else:
                if current_price_low <= price:
                    hit_tp = True
            
            if hit_tp:
                # Partial Close
                # Ensure we don't close more than current size
                close_qty = min(qty, position.size)
                self._close_partial(position, price, close_qty, f"tp_{parts[1]}", candle.timestamp)
                hits += 1
                
                 # Activate Trailing if TP1
                if parts[1] == "0":
                    if not position.trailing_active and self.config.execution.trailing_enabled:
                        position.trailing_active = True
            else:
                remaining_tps.append(tp_id)
        
        if hits > 0:
            position.tp_order_ids = remaining_tps
            # If size ~ 0, close full
            if position.size <= Decimal("0.0001"):
                self.position = None
                return True
                
        return False
        
    def _close_partial(self, position: Position, price: Decimal, qty: Decimal, reason: str, timestamp: datetime):
        """Close partial size."""
        if position.side == Side.LONG:
            pnl = (price - position.entry_price) * qty
        else:
            pnl = (position.entry_price - price) * qty
            
        exit_fees = (price * qty) * (self.taker_fee_bps / Decimal("10000"))
        net_pnl = pnl - exit_fees
        self.metrics.total_fees += exit_fees
        self.metrics.total_pnl += net_pnl
        self.current_equity += net_pnl
        
        position.size -= qty
        position.size_notional -= (qty * position.entry_price)
        
        if net_pnl > 0: self.metrics.winning_trades += 1 # Tracking individual fills as "trades"? Or just PnL?
        # Metric counting is tricky with partials. 
        # For simple metrics, we count a "Winning Trade" if the full roundtrip is positive?
        # Here we just pump PnL.
        
    def _close_position(self, exit_price: Decimal, reason: str, timestamp: datetime, size: Decimal):
        """Close remaining position."""
        if not self.position: return
        
        if self.position.side == Side.LONG:
            pnl = (exit_price - self.position.entry_price) * size
        else:
            pnl = (self.position.entry_price - exit_price) * size
            
        exit_fees = (exit_price * size) * (self.taker_fee_bps / Decimal("10000"))
        net_pnl = pnl - exit_fees
        self.metrics.total_fees += exit_fees
        self.metrics.total_pnl += net_pnl
        self.current_equity += net_pnl
         
        self.metrics.total_trades += 1
        if net_pnl > 0: 
            self.metrics.winning_trades += 1
        else:
            self.metrics.losing_trades += 1
            
        self.risk_manager.record_trade_result(net_pnl, self.current_equity)
        self.position = None
