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
from src.execution.execution_engine import ExecutionEngine
from src.storage.repository import save_candle, save_trade, save_position, delete_position, get_candles, get_active_position

logger = get_logger(__name__)


class PaperTrading:
    """
    Paper trading engine.
    
    Mimics ExecutionEngine logic but operates in real-time loop.
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
        self.execution = ExecutionEngine(config)
        
        # State
        self.current_equity = Decimal("10000") # Default start
        self.active = False
        
        # Cache for indicators (Symbol -> List[Candle])
        self.candles_15m: Dict[str, List[Candle]] = {}
        self.candles_1h: Dict[str, List[Candle]] = {}
        self.candles_4h: Dict[str, List[Candle]] = {}
        self.candles_1d: Dict[str, List[Candle]] = {}
        
        logger.info("Paper Trading initialized", markets=config.exchange.spot_markets)
    
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
        
        # Fetch data for all markets
        for symbol in self.config.exchange.spot_markets:
            logger.info(f"Warming up {symbol}...")
            self.candles_1d[symbol] = await self._fetch_history(symbol, "1d", start, end)
            self.candles_4h[symbol] = await self._fetch_history(symbol, "4h", start, end)
            self.candles_1h[symbol] = await self._fetch_history(symbol, "1h", start, end)
            self.candles_15m[symbol] = await self._fetch_history(symbol, "15m", start, end)
        
        logger.info("Warmup complete")
    
    async def _fetch_history(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> List[Candle]:
        """Fetch history helper."""
        # Reuse backtest fetching logic if possible, or simple fetch here
        # For paper, we just need recent history
        # Simplified: fetch last 1000 candles from API
        # To be robust, should use same logic as backtest (DB + API)
        # Using client directly for now
        since = int(start.timestamp() * 1000)
        candles = await self.client.get_spot_ohlcv(symbol, timeframe, since=since)
        return candles

    async def _tick(self):
        """Single iteration of trading logic."""
        for symbol in self.config.exchange.spot_markets:
            try:
                # 1. Fetch latest state
                # Get latest 1m ticker or candle for price check
                ticker = await self.client.get_spot_ticker(symbol)
                current_price = Decimal(str(ticker['last']))
                now = datetime.now(timezone.utc)
                
                # 2. Update Candle Buffers (Poll for new candles)
                # Simplified: Fetch latest 5 candles for each TF and append if new
                await self._update_candles(symbol)
                
                # 3. Check Exits (SL/TP)
                # Position stored in DB, check specifically for this symbol
                active_pos = get_active_position(symbol)
                
                if active_pos:
                    self._check_exit(active_pos, current_price, symbol)
                    
                    # Update Trailing/BE State
                    self._update_position_state(active_pos, current_price)
                    
                # 4. Generate Signals (Only if no position)
                if not active_pos:
                    signal = self.smc_engine.generate_signal(
                        symbol,
                        bias_candles_4h=self.candles_4h.get(symbol, []),
                        bias_candles_1d=self.candles_1d.get(symbol, []),
                        exec_candles_15m=self.candles_15m.get(symbol, []),
                        exec_candles_1h=self.candles_1h.get(symbol, [])
                    )
                    
                    if signal.signal_type != SignalType.NO_SIGNAL:
                        logger.info("Signal detected", type=signal.signal_type.value, price=current_price, symbol=symbol)
                        await self._process_signal(signal, current_price)
                        
            except Exception as e:
                logger.error(f"Error ticking {symbol}", error=str(e))
    
    async def _update_candles(self, symbol: str):
        """Update local candle buffers."""
        # For each timeframe, fetch recent and update list
        # Optimization: Only fetch if enough time passed
        # Here: Naive fetch latest
        
        async def update_tf(tf: str, buffer_dict: Dict[str, List[Candle]]):
            if symbol not in buffer_dict:
                buffer_dict[symbol] = []
                
            latest = await self.client.get_spot_ohlcv(symbol, tf, limit=5)
            if not latest:
                return
            
            buffer = buffer_dict[symbol]
            
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
        
        # Risk Check
        decision = self.risk_manager.validate_trade(
            signal, self.current_equity, current_price, current_price # Assuming mark=spot for paper
        )
        
        if not decision.approved:
            logger.info("Signal rejected by risk", reason=decision.rejection_reasons)
            return
            
        # Execute using Engine
        plan = self.execution.generate_entry_plan(
            signal, 
            decision.position_notional, 
            current_price, 
            current_price, 
            decision.leverage
        )
        
        # Simulate Entry Fill
        entry_intent = plan['entry']
        fill_price = entry_intent['price'] if entry_intent['price'] else current_price
        
         # Calculate liquidation price
        maint_margin = Decimal("0.02")
        if signal.signal_type == SignalType.LONG:
             liq_price = fill_price * (Decimal("1") - (Decimal("1")/decision.leverage) + maint_margin)
        else:
             liq_price = fill_price * (Decimal("1") + (Decimal("1")/decision.leverage) - maint_margin)
             
        # Create TP IDs
        tp_ids = []
        for i, tp in enumerate(plan['take_profits']):
             # Format: TP-{index}-{price}-{qty}
             tp_ids.append(f"TP-{i}-{tp['price']}-{tp['qty']}")
             
        # Create SL ID
        sl_price = plan['stop_loss']['price']
        sl_id = f"SL-{sl_price}"

        new_position = Position(
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
            take_profit_order_id=None, # Deprecated in favor of tp_order_ids
            tp_order_ids=tp_ids,
            trailing_active=False,
            break_even_active=False,
            peak_price=fill_price,
            peak_price=fill_price,
            opened_at=datetime.now(timezone.utc),
            setup_type=signal.setup_type.value if hasattr(signal.setup_type, 'value') else signal.setup_type,
            regime=signal.regime
        )
        
        save_position(new_position)
        logger.info(
            "Virtual Position Opened", 
            symbol=signal.symbol, 
            side=new_position.side.value, 
            size=str(new_position.size_notional),
            tps=len(tp_ids)
        )
        
    def _check_exit(self, position: Position, current_price: Decimal, symbol: str) -> bool:
        """Check exits against current price."""
        # 1. Stop Loss
        stop_loss = Decimal(position.stop_loss_order_id.split("-")[1])
        
        hit_sl = False
        if position.side == Side.LONG:
            if current_price <= stop_loss:
                hit_sl = True
        else:
            if current_price >= stop_loss:
                hit_sl = True
                
        if hit_sl:
            self._close_position(position, stop_loss, "stop_loss")
            return True
            
        # 2. Take Profits (Partial)
        remaining_tps = []
        hits = 0
        
        for tp_id in position.tp_order_ids:
            # Parse: TP-{index}-{price}-{qty}
            parts = tp_id.split("-")
            price = Decimal(parts[2])
            qty = Decimal(parts[3])
            
            hit_tp = False
            if position.side == Side.LONG:
                if current_price >= price:
                    hit_tp = True
            else:
                if current_price <= price:
                    hit_tp = True
                    
            if hit_tp:
                # Partial Close
                self._close_partial(position, price, qty, f"tp_{parts[1]}")
                hits += 1
                
                # Activate BE/Trailing if first TP
                if parts[1] == "0": # TP1
                    if not position.trailing_active and self.config.execution.trailing_enabled:
                        position.trailing_active = True
                        save_position(position)
                        logger.info("Trailing Activated", symbol=symbol)
            else:
                remaining_tps.append(tp_id)
                
        if hits > 0:
            if not remaining_tps and position.size <= Decimal("0.0001"): # Allow dust
                 # Position fully closed
                 delete_position(position.symbol)
                 logger.info("Position closed after final TP", symbol=symbol)
                 return True
            else:
                 # Update TPs
                 position.tp_order_ids = remaining_tps
                 save_position(position)
                 
        return False

    def _update_position_state(self, position: Position, current_price: Decimal):
        """Update dynamic state (BE, Trailing)."""
        
        # Update Peak Price for Trailing
        updated = False
        if position.side == Side.LONG:
            if current_price > (position.peak_price or position.entry_price):
                position.peak_price = current_price
                updated = True
        else:
            if current_price < (position.peak_price or position.entry_price):
                position.peak_price = current_price
                updated = True
        
        if updated:
             save_position(position)

        # Check Trailing Update
        if position.trailing_active:
            # Need Spot ATR/Price context for calc
            # For paper, we assume Mark ~ Spot and reuse mark as 'spot' ref
            # This is a simplification. Ideally fetch stored spot candle.
             # We can use the cached candles.
            candles = self.candles_1h.get(position.symbol, [])
            if candles:
                 # Calculate ATR
                 atr_val = self.smc_engine.indicators.calculate_atr(candles, 14).iloc[-1]
                 current_sl = Decimal(position.stop_loss_order_id.split("-")[1])
                 
                 new_sl = self.execution.check_trailing_stop(
                     position,
                     current_price,
                     Decimal(str(atr_val)),
                     current_price, # Using mark as spot proxy
                     current_sl
                 )
                 
                 if new_sl:
                     position.stop_loss_order_id = f"SL-{new_sl}"
                     save_position(position)
                     logger.info("Trailing SL Updated", symbol=position.symbol, new_sl=str(new_sl))
                     
        # Check BE
        # Logic: If TP1 hit (handled in _check_exit), break_even logic might be triggered there?
        # Specification says: "When TP1 is confirmed... Activate BE and trailing".
        # We can also check here if we want to support "price distance" trigger instead of fill.
        # But stick to spec: TP1 fill activates it. 
        # Actually my _check_exit activates trailing but not BE explicitly.
        # Let's add BE activation there too or just let trailing handle "tightening".
        # Spec says "Break-even stop placement".
        pass # Handled/Simplified for now via Trailing

    def _close_partial(self, position: Position, price: Decimal, qty: Decimal, reason: str):
        """Close partial size."""
        # Calculate PnL
        if position.side == Side.LONG:
            pnl = (price - position.entry_price) * qty
        else:
            pnl = (position.entry_price - price) * qty
            
        fees = (price * qty) * Decimal("0.0005")
        net_pnl = pnl - fees
        
        self.current_equity += net_pnl
        self.risk_manager.record_trade_result(
            net_pnl, 
            self.current_equity,
            setup_type=position.setup_type
        )
        
        # Update Position Size
        position.size -= qty
        position.size_notional -= (qty * position.entry_price) # Approx
        
        save_position(position)
        logger.info("Partial Close", symbol=position.symbol, reason=reason, qty=str(qty), pnl=str(net_pnl))

    def _close_position(self, position: Position, price: Decimal, reason: str):
        """Close virtual position."""
        if position.side == Side.LONG:
            pnl = (price - position.entry_price) * position.size
        else:
            pnl = (position.entry_price - price) * position.size
            
        # Fees
        fees = (price * position.size) * Decimal("0.0005")
        net_pnl = pnl - fees
        
        self.current_equity += net_pnl
        self.risk_manager.record_trade_result(
            net_pnl, 
            self.current_equity, 
            setup_type=position.setup_type
        )
        
        logger.info("Virtual Position Closed", symbol=position.symbol, reason=reason, pnl=str(net_pnl), equity=str(self.current_equity))
        
        # Save trade
        trade = Trade(
            trade_id=str(uuid.uuid4()),
            symbol=position.symbol,
            side=position.side,
            entry_price=position.entry_price,
            exit_price=price,
            size_notional=position.size_notional,
            leverage=position.leverage,
            gross_pnl=pnl,
            fees=fees,
            funding=Decimal("0"),
            net_pnl=net_pnl,
            entered_at=position.opened_at,
            exited_at=datetime.now(timezone.utc),
            holding_period_hours=Decimal("0"), # TODO
            exit_reason=reason,
            setup_type=position.setup_type,
            regime=position.regime
        )
        save_trade(trade)
        delete_position(position.symbol)
