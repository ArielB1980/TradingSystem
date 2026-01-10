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
            total_wins = sum(t for t in [Decimal("0")])  # Will track wins
            total_losses = sum(t for t in [Decimal("0")])  # Will track losses
            
            if self.winning_trades > 0:
                self.avg_win = total_wins / self.winning_trades if total_wins > 0 else Decimal("0")
            if self.losing_trades > 0:
                self.avg_loss = abs(total_losses / self.losing_trades) if total_losses != 0 else Decimal("0")


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
        """
        Initialize backtest engine.
        
        Args:
            config: System configuration
            kraken_client: Kraken API client for historical data
        """
        self.config = config
        self.client = kraken_client
        
        # Initialize components
        self.smc_engine = SMCEngine(config.strategy)
        self.risk_manager = RiskManager(config.risk)
        self.basis_guard = BasisGuard(config.risk)
        
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
        
        logger.info(
            "Backtest engine initialized",
            starting_equity=str(self.starting_equity),
            taker_fee=f"{self.taker_fee_bps}bps",
            slippage=f"{self.slippage_bps}bps",
        )
    
    async def run(
        self,
        symbol: str,
        start_date: datetime,
        end_date: datetime,
    ) -> BacktestMetrics:
        """
        Run backtest for given date range.
        
        Args:
            symbol: Spot symbol (e.g., "BTC/USD")
            start_date: Start date (UTC)
            end_date: End date (UTC)
        
        Returns:
            BacktestMetrics with results
        """
        logger.info(
            "Starting backtest",
            symbol=symbol,
            start=start_date.isoformat(),
            end=end_date.isoformat(),
        )
        

        # Calculate warmup period (need ~200 days for daily EMA)
        # Fetch extra data prior to start_date
        data_start = start_date - timedelta(days=300)
        logger.info("Fetching historical data (including warmup)...", data_start=data_start.isoformat())
        
        # Fetch data from data_start to end_date
        candles_1d = await self._fetch_historical(symbol, "1d", data_start, end_date)
        candles_4h = await self._fetch_historical(symbol, "4h", data_start, end_date)
        candles_1h = await self._fetch_historical(symbol, "1h", data_start, end_date)
        candles_15m = await self._fetch_historical(symbol, "15m", data_start, end_date)
        
        logger.info(
            "Data fetched",
            candles_1d=len(candles_1d),
            candles_4h=len(candles_4h),
            candles_1h=len(candles_1h),
            candles_15m=len(candles_15m),
        )
        
        # Replay chronologically (use 1h as main timeline)
        for i, current_candle in enumerate(candles_1h):
            # Skip warmup period simulation (but use data)
            # We skip explicit logic if before start_date, EXCEPT we need to ensure history is sufficient
            
            # Get all candles up to this point
            cutoff_time = current_candle.timestamp
            
            # Historical candles for signal generation
            # Optimize: In a real engine, we'd maintain sliding windows. 
            # Here O(N^2) is acceptable for short tests.
            hist_1d = [c for c in candles_1d if c.timestamp <= cutoff_time]
            hist_4h = [c for c in candles_4h if c.timestamp <= cutoff_time]
            hist_1h = [c for c in candles_1h if c.timestamp <= cutoff_time]
            hist_15m = [c for c in candles_15m if c.timestamp <= cutoff_time]
            
            # Need enough history for indicators
            if len(hist_1d) < 200 or len(hist_1h) < 200:
                continue  # Skip until we have enough data
                
            # Wait until requested simulation start
            if current_candle.timestamp < start_date:
                continue
            
            # Check existing position
            if self.position:
                # Simulate stop-loss / take-profit checks
                filled = self._check_exit(self.position, current_candle)
                if filled:
                    continue
            
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
            
            # Update equity curve
            if i % 24 == 0:  # Daily snapshot
                self.metrics.equity_curve.append(self.current_equity)
                
                # Track drawdown
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
        """
        Fetch historical OHLCV data with database caching.
        
        Strategy:
        1. Try to load from DB
        2. If coverage is sufficient (>95%), use DB data
        3. Else, fetch from API (throttled) and save to DB
        """
        from src.storage.repository import get_candles, save_candles_bulk
        
        # 1. Attempt DB Load
        db_candles = get_candles(symbol, timeframe, start_date, end_date)
        
        # Calculate expected vs actual count
        # Approximation: duration / interval
        # Simplified check: if we have significant data, assume it's good for now
        # Ideally we'd look for specific gaps, but we'll trust the range query count
        total_seconds = (end_date - start_date).total_seconds()
        interval_seconds = self._timeframe_to_seconds(timeframe)
        expected_count = total_seconds / interval_seconds
        
        # Allow 5% tolerance for data gaps/maintenance
        if len(db_candles) >= expected_count * 0.95:
            logger.debug("Loaded from DB cache", count=len(db_candles), timeframe=timeframe)
            return db_candles
            
        logger.info(
            "Cache miss - fetching from API", 
            found=len(db_candles), 
            expected=int(expected_count), 
            timeframe=timeframe
        )

        # 2. Fetch from API (Throttled)
        candles = []
        since = int(start_date.timestamp() * 1000)
        end_ts = int(end_date.timestamp() * 1000)
        
        while since < end_ts:
            # Rate limit protection
            await asyncio.sleep(2.0)
            
            try:
                batch = await self.client.get_spot_ohlcv(symbol, timeframe, since=since, limit=720)
            except Exception as e:
                # Retry logic
                if "Too many requests" in str(e) or "DDoSProtection" in str(e):
                    logger.warning("Rate limit hit, cooling down...", error=str(e))
                    await asyncio.sleep(15.0)
                    continue
                else:
                    raise e

            if not batch:
                break
            
            candles.extend(batch)
            since = int(batch[-1].timestamp.timestamp() * 1000) + 1
            
            logger.debug(f"Fetched {len(batch)} {timeframe} candles, total={len(candles)}")
            
            # Save batch immediately to secure progress
            saved_count = save_candles_bulk(batch)
            # logger.debug(f"Cached {saved_count} candles")
        
        return candles

    def _timeframe_to_seconds(self, tf: str) -> int:
        """Helper to estimate candle count."""
        unit = tf[-1]
        value = int(tf[:-1])
        if unit == 'm': return value * 60
        if unit == 'h': return value * 3600
        if unit == 'd': return value * 86400
        return 60  # default

    
    async def _process_signal(self, signal: Signal, current_candle: Candle):
        """Process trading signal and simulate entry."""
        # Use current candle close as futures mark price (simplified)
        futures_mark = current_candle.close
        spot_price = current_candle.close
        
        # Basis guard check
        approved, divergence, reason = self.basis_guard.check_pre_entry(
            spot_price, futures_mark, signal.symbol
        )
        if not approved:
            logger.debug("Basis guard rejected", reason=reason)
            return
        
        # Risk validation
        decision = self.risk_manager.validate_trade(
            signal, self.current_equity, spot_price, futures_mark
        )
        
        if not decision.approved:
            logger.debug("Risk manager rejected", reasons=decision.rejection_reasons)
            return
        
        # Simulate entry with fees + slippage
        entry_price = futures_mark
        total_cost_bps = self.taker_fee_bps + self.slippage_bps
        entry_price_with_cost = entry_price * (Decimal("1") + total_cost_bps / Decimal("10000"))
        
        if signal.signal_type == SignalType.SHORT:
            entry_price_with_cost = entry_price * (Decimal("1") - total_cost_bps / Decimal("10000"))
        
        fees = decision.position_notional * (self.taker_fee_bps / Decimal("10000"))
        self.metrics.total_fees += fees
        
        # Create position
        # Calculate liquidation price for simulation
        # Long: Entry * (1 - 1/Lev + MaintMargin) roughly, but simplified:
        # Bankrupt price = Entry * (1 - 1/Lev) for Long
        maint_margin = Decimal("0.02")  # 2% maintenance margin
        if signal.signal_type == SignalType.LONG:
             liq_price = entry_price_with_cost * (Decimal("1") - (Decimal("1")/decision.leverage) + maint_margin)
        else:
             liq_price = entry_price_with_cost * (Decimal("1") + (Decimal("1")/decision.leverage) - maint_margin)

        # Create position matching models.py definition
        self.position = Position(
            symbol=signal.symbol,
            side=Side.LONG if signal.signal_type == SignalType.LONG else Side.SHORT,
            size=decision.position_notional / entry_price_with_cost,
            size_notional=decision.position_notional,
            entry_price=entry_price_with_cost,
            current_mark_price=entry_price_with_cost,
            liquidation_price=liq_price,
            unrealized_pnl=Decimal("0"),
            leverage=decision.leverage,
            margin_used=decision.margin_required,
            stop_loss_order_id=f"SL-{signal.stop_loss}",
            take_profit_order_id=f"TP-{signal.take_profit}" if signal.take_profit else None,
            opened_at=current_candle.timestamp
        )
        
        logger.info(
            "Position opened",
            side=self.position.side.value,
            entry=str(entry_price_with_cost),
            size=str(decision.position_notional),
            stop=str(signal.stop_loss),
            tp=str(signal.take_profit) if signal.take_profit else "None",
        )
    
    def _check_exit(self, position: Position, candle: Candle) -> bool:
        """Check if position hit stop-loss or take-profit."""
        # Parse levels from order IDs (convention: "SL-{price}", "TP-{price}")
        stop_loss = Decimal(position.stop_loss_order_id.split("-")[1]) if position.stop_loss_order_id else None
        
        take_profit = None
        if position.take_profit_order_id:
            take_profit = Decimal(position.take_profit_order_id.split("-")[1])
            
        # Check stop-loss
        if position.side == Side.LONG:
            if stop_loss and candle.low <= stop_loss:
                self._close_position(stop_loss, "stop_loss", candle.timestamp)
                return True
            if take_profit and candle.high >= take_profit:
                self._close_position(take_profit, "take_profit", candle.timestamp)
                return True
        else:  # SHORT
            if stop_loss and candle.high >= stop_loss:
                self._close_position(stop_loss, "stop_loss", candle.timestamp)
                return True
            if take_profit and candle.low <= take_profit:
                self._close_position(take_profit, "take_profit", candle.timestamp)
                return True
        
        return False
    
    def _close_position(self, exit_price: Decimal, reason: str, timestamp: datetime):
        """Close position and update metrics."""
        if not self.position:
            return
        
        # Calculate P&L
        if self.position.side == Side.LONG:
            pnl = (exit_price - self.position.entry_price) * self.position.size
        else:  # SHORT
            pnl = (self.position.entry_price - exit_price) * self.position.size
        
        # Deduct exit fees
        exit_notional = exit_price * self.position.size
        exit_fees = exit_notional * (self.taker_fee_bps / Decimal("10000"))
        pnl -= exit_fees
        self.metrics.total_fees += exit_fees
        
        # Update equity
        self.current_equity += pnl
        self.metrics.total_pnl += pnl
        
        # Update trade stats
        self.metrics.total_trades += 1
        if pnl > 0:
            self.metrics.winning_trades += 1
        else:
            self.metrics.losing_trades += 1
        
        # Record with risk manager
        self.risk_manager.record_trade_result(pnl)
        
        logger.info(
            "Position closed",
            reason=reason,
            exit=str(exit_price),
            pnl=str(pnl),
            equity=str(self.current_equity),
        )
        
        self.position = None
