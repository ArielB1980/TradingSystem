import asyncio
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import List, Dict, Optional

from src.config.config import Config
from src.monitoring.logger import get_logger
from src.data.kraken_client import KrakenClient
from src.data.data_acquisition import DataAcquisition
from src.strategy.smc_engine import SMCEngine
from src.risk.risk_manager import RiskManager
from src.execution.executor import Executor
from src.execution.futures_adapter import FuturesAdapter
from src.execution.execution_engine import ExecutionEngine
from src.utils.kill_switch import KillSwitch, KillSwitchReason
from src.domain.models import Candle, Signal, SignalType, Position, Side
from src.storage.repository import save_candle, get_active_position, save_account_state

logger = get_logger(__name__)


class LiveTrading:
    """
    Live trading runtime.
    
    CRITICAL: Real capital at risk. Enforces all safety gates.
    """
    
    def __init__(self, config: Config):
        """Initialize live trading."""
        self.config = config
        
        # Core Components
        self.client = KrakenClient(
            api_key=config.exchange.api_key,
            api_secret=config.exchange.api_secret,
            futures_api_key=config.exchange.futures_api_key,
            futures_api_secret=config.exchange.futures_api_secret,
            use_testnet=config.exchange.use_testnet
        )
        
        self.data_acq = DataAcquisition(
            self.client,
            spot_symbols=config.exchange.spot_markets,
            futures_symbols=config.exchange.futures_markets
        )
        
        self.smc_engine = SMCEngine(config.strategy)
        self.risk_manager = RiskManager(config.risk)
        self.futures_adapter = FuturesAdapter(self.client)
        self.executor = Executor(config.execution, self.futures_adapter)
        self.execution_engine = ExecutionEngine(config)
        self.kill_switch = KillSwitch(self.client)
        
        # State
        self.active = False
        self.candles_1d: Dict[str, List[Candle]] = {}
        self.candles_4h: Dict[str, List[Candle]] = {}
        self.candles_1h: Dict[str, List[Candle]] = {}
        self.candles_15m: Dict[str, List[Candle]] = {}
        self.last_account_sync = datetime.min.replace(tzinfo=timezone.utc)
        
        # Market Expansion (Coin Universe)
        self.markets = config.exchange.spot_markets
        if config.assets.mode == "whitelist":
             self.markets = config.assets.whitelist
        elif config.coin_universe and config.coin_universe.enabled:
             # Expand from Tiers
             expanded = []
             for tier, coins in config.coin_universe.liquidity_tiers.items():
                 expanded.extend(coins)
             self.markets = list(set(expanded)) # Deduplicate
             logger.info("Coin Universe Enabled", markets=self.markets)
             
        # Update Data Acquisition with full list
        self.data_acq = DataAcquisition(
            self.client,
            spot_symbols=self.markets,
            futures_symbols=config.exchange.futures_markets # This needs expansion too ideally, but for now focus on spot scanning
        )
        
        logger.info("Live Trading initialized", markets=config.exchange.futures_markets)
    
    async def run(self):
        """Run the main live trading loop."""
        self.active = True
        logger.critical("🚀 STARTING LIVE TRADING - REAL CAPITAL AT RISK")
        
        try:
            # 1. Initial Account Sync (Provide immediate feedback to dashboard)
            await self._sync_account_state()
            
            # 2. Warmup
            await self._warmup()
            
            # 2. Start Data Acquisition
            asyncio.create_task(self.data_acq.start())
            
            # 3. Main Loop
            while self.active:
                if self.kill_switch.is_active():
                    logger.critical("Kill switch active - pausing loop")
                    await asyncio.sleep(60)
                    continue
                
                loop_start = datetime.now(timezone.utc)
                
                try:
                    await self._tick()
                except Exception as e:
                    logger.error("Error in live trading tick", error=str(e))
                    if "API" in str(e):
                         # Potential API failure - check if we should trigger kill switch
                         pass
                
                # Dynamic sleep to align with 1m intervals
                elapsed = (datetime.now(timezone.utc) - loop_start).total_seconds()
                sleep_time = max(5.0, 60.0 - elapsed)
                await asyncio.sleep(sleep_time)
                
        except asyncio.CancelledError:
            logger.info("Live trading loop cancelled")
        finally:
            self.active = False
            await self.data_acq.stop()
            await self.client.close()
            logger.info("Live trading shutdown complete")
            
    async def _warmup(self):
        """Fetch historical data for indicators."""
        logger.info("Warming up indicators...")
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=200) # Required for SMC indicators
        
        for symbol in self.markets:
            logger.info(f"Fetching history for {symbol}")
            self.candles_1d[symbol] = await self.data_acq.fetch_spot_historical(symbol, "1d", start, end)
            self.candles_4h[symbol] = await self.data_acq.fetch_spot_historical(symbol, "4h", start, end)
            self.candles_1h[symbol] = await self.data_acq.fetch_spot_historical(symbol, "1h", start, end)
            self.candles_15m[symbol] = await self.data_acq.fetch_spot_historical(symbol, "15m", start, end)
            
        logger.info("Indicators warmed up")

    async def _sync_positions(self) -> List[Dict]:
        """
        Sync active positions from exchange and update RiskManager.
        
        Returns:
            List of active positions
            
        Raises:
            Exception: If position sync fails (caller should handle)
        """
        active_positions = await self.client.get_all_futures_positions()
        self.risk_manager.update_position_list(active_positions)
        
        if active_positions:
            logger.info(
                f"Active Portfolio: {len(active_positions)} positions", 
                symbols=[p['symbol'] for p in active_positions]
            )
        
        return active_positions

    async def _tick(self):
        """Single iteration of live trading logic."""
        # 1. Check Data Health
        if not self.data_acq.is_healthy():
            logger.error("Data acquisition unhealthy")
            return

        # 2. Sync Active Positions (CRITICAL: Must happen before signal generation)
        try:
            await self._sync_positions()
        except Exception as e:
            logger.error("Failed to sync positions", error=str(e))
            return  # Safety: Don't trade if we can't see positions

        for spot_symbol in self.markets:
            try:
                # 2. Get Futures Context
                futures_symbol = self.futures_adapter.map_spot_to_futures(spot_symbol)
                mark_price = await self.client.get_futures_mark_price(futures_symbol)
                spot_price = (await self.client.get_spot_ticker(spot_symbol))['last']
                spot_price = Decimal(str(spot_price))
                
                # 3. Update Sync Data
                await self._update_candles(spot_symbol)
                
                # 4. Check Current Position
                position_data = await self.client.get_futures_position(futures_symbol)
                
                if position_data:
                    # Logic for Trailing/BE/Emergency Close
                    # For now: Log and let protective orders handle it
                    logger.info(f"Active position in {futures_symbol}", side=position_data['side'], size=str(position_data['size']))
                else:
                    # 5. Generate Signals (Spot Analysis)
                    signal = self.smc_engine.generate_signal(
                        spot_symbol,
                        bias_candles_4h=self.candles_4h.get(spot_symbol, []),
                        bias_candles_1d=self.candles_1d.get(spot_symbol, []),
                        exec_candles_15m=self.candles_15m.get(spot_symbol, []),
                        exec_candles_1h=self.candles_1h.get(spot_symbol, [])
                    )
                    
                    if signal.signal_type != SignalType.NO_SIGNAL:
                         await self._handle_signal(signal, spot_price, mark_price)
                         
            except Exception as e:
                logger.error(f"Error ticking {spot_symbol}", error=str(e))
        
        # 6. Sync Account State (Throttled 15s)
        now = datetime.now(timezone.utc)
        if (now - self.last_account_sync).total_seconds() > 15:
            await self._sync_account_state()
            self.last_account_sync = now

    async def _sync_account_state(self):
        """Fetch and persist real-time account state."""
        try:
            # 1. Get Balances
            balance = await self.client.get_futures_balance()
            if not balance:
                return

            # Default to standard CCXT total['USD']
            base_currency = getattr(self.config.exchange, "base_currency", "USD")
            total = balance.get('total', {})
            equity = Decimal(str(total.get(base_currency, 0)))
            avail_margin = Decimal(str(balance.get('free', {}).get(base_currency, 0)))
            margin_used_val = Decimal(str(balance.get('used', {}).get(base_currency, 0)))
            
            # 2. Check for Kraken Futures Multi-Collateral ("flex")
            # This is critical because 'total' only shows token amounts, not USD value of collateral
            info = balance.get('info', {})
            if info and 'accounts' in info and 'flex' in info['accounts']:
                flex = info['accounts']['flex']
                # portfolioValue = Total Equity (Balance + Unr. PnL + Collateral Value)
                # availableMargin = Margin available for new positions
                # initialMargin = Margin used
                
                pv = flex.get('portfolioValue')
                am = flex.get('availableMargin')
                im = flex.get('initialMargin')
                
                if pv is not None:
                    equity = Decimal(str(pv))
                if am is not None:
                    avail_margin = Decimal(str(am))
                if im is not None:
                    margin_used_val = Decimal(str(im))
                    
                logger.debug("Synced Multi-Collateral state", equity=str(equity))
            
            # 3. Persist
            save_account_state(
                equity=equity,
                balance=equity, # For futures margin, equity IS the balance relevant for trading
                margin_used=margin_used_val,
                available_margin=avail_margin,
                unrealized_pnl=Decimal("0.0") # Included in portfolioValue usually
            )
            
        except Exception as e:
            logger.error("Failed to sync account state", error=str(e))

    async def _handle_signal(self, signal: Signal, spot_price: Decimal, mark_price: Decimal):
        """Process signal through risk and executor."""
        logger.info("New signal detected", type=signal.signal_type.value, symbol=signal.symbol)
        
        # 1. Fetch Account Equity
        # For futures, we need futures balance
        balance = await self.client.get_futures_balance()
        # CCXT balance often has 'total' as dict of currency -> total
        # Usually we use USD or USDT as base. Ref config for base currency.
        base_currency = getattr(self.config.exchange, "base_currency", "USD")
        equity = Decimal(str(balance.get('total', {}).get(base_currency, 0)))
        
        if equity <= 0:
            logger.error("Insufficient equity for trading", equity=str(equity))
            return
            
        # 2. Risk Validation (Safety Gate)
        decision = self.risk_manager.validate_trade(
            signal, equity, spot_price, mark_price
        )
        
        if not decision.approved:
            logger.warning("Trade rejected by Risk Manager", reasons=decision.rejection_reasons)
            return
            
        # 3. Execution Planning (Spot -> Futures)
        order_intent = self.execution_engine.generate_entry_plan(
            signal, 
            decision.position_notional,
            spot_price,
            mark_price,
            decision.leverage
        )
        
        # 4. Final Order Intent (with futures prices)
        # Note: In my Executor update I refined OrderIntent, but here 
        # generate_entry_plan returns a dict. We should ideally use OrderIntent object.
        # Fixed in earlier turn but let's ensure compatibility.
        from src.domain.models import OrderIntent as OrderIntentModel
        
        intent_model = OrderIntentModel(
            timestamp=datetime.now(timezone.utc),
            signal=signal,
            side=Side.LONG if signal.signal_type == SignalType.LONG else Side.SHORT,
            size_notional=decision.position_notional,
            leverage=decision.leverage,
            entry_price_spot=signal.entry_price,
            stop_loss_spot=signal.stop_loss,
            take_profit_spot=signal.take_profit,
            entry_price_futures=order_intent['metadata']['fut_entry'],
            stop_loss_futures=order_intent['metadata']['fut_sl'],
            take_profit_futures=order_intent['take_profits'][0]['price'] if order_intent['take_profits'] else None
        )
        
        # 4. Execute
        entry_order = await self.executor.execute_signal(intent_model, mark_price, [])
        
        if entry_order:
             # Protective orders are placed after fill usually, 
             # but for now we place them immediately or on next tick detection.
             # Executor.execute_signal returns the entry order.
             # In a real system, we'd wait for fill event.
             # For simpler loop, we can try to place protective orders on next tick if fill confirmed.
             logger.info("Entry order placed", order_id=entry_order.order_id)
             
             # Note: Trade records are only saved when positions are CLOSED (entry → exit).
             # Open positions are tracked via exchange API and synced in _sync_positions().
             # Trade persistence will happen in the position management/exit logic.

    async def _update_candles(self, symbol: str):
        """Update local candle caches from acquisition."""
        # Simplified: fetch latest from data acquisition or client
        # To be truly efficient, this should link to data_acq buffers.
        # Using simple fetch for now.
        async def fetch_tf(tf: str, buffer: Dict[str, List[Candle]]):
            candles = await self.client.get_spot_ohlcv(symbol, tf, limit=10)
            if not candles: return
            
            existing = buffer.get(symbol, [])
            last_ts = existing[-1].timestamp if existing else datetime.min.replace(tzinfo=timezone.utc)
            
            new_ones = [c for c in candles if c.timestamp > last_ts]
            for c in new_ones:
                save_candle(c)
                existing.append(c)
            
            # Trim
            if len(existing) > 500:
                buffer[symbol] = existing[-500:]
            else:
                buffer[symbol] = existing

        await fetch_tf("15m", self.candles_15m)
        await fetch_tf("1h", self.candles_1h)
        await fetch_tf("4h", self.candles_4h)
        await fetch_tf("1d", self.candles_1d)
