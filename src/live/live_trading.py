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
from src.execution.position_manager import PositionManager, ActionType
from src.utils.kill_switch import KillSwitch, KillSwitchReason
from src.domain.models import Candle, Signal, SignalType, Position, Side
from src.storage.repository import save_candle, get_active_position, save_account_state, sync_active_positions, record_event, load_candles_map
from src.storage.maintenance import DatabasePruner

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
        self.position_manager = PositionManager()
        self.kill_switch = KillSwitch(self.client)
        
        # State
        self.managed_positions: Dict[str, Position] = {}  # V3 State Tracking
        self.active = False
        self.candles_1d: Dict[str, List[Candle]] = {}
        self.candles_4h: Dict[str, List[Candle]] = {}
        self.candles_1h: Dict[str, List[Candle]] = {}
        self.candles_1h: Dict[str, List[Candle]] = {}
        self.candles_15m: Dict[str, List[Candle]] = {}
        self.last_candle_update: Dict[str, Dict[str, datetime]] = {} # Cache tracking
        self.last_trace_log: Dict[str, datetime] = {} # Dashboard update throttling
        self.last_account_sync = datetime.min.replace(tzinfo=timezone.utc)
        self.last_maintenance_run = datetime.min.replace(tzinfo=timezone.utc)
        self.db_pruner = DatabasePruner()
        
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
            try:
                await self._sync_positions()
            except Exception as e:
                logger.warning("Initial position sync failed", error=str(e))
            
            # Phase 10.5: Fast Startup Hydration
            try:
                logger.info("Hydrating candle cache from database...")
                start_hydrate = datetime.now()
                
                # Load all required timeframes
                self.candles_15m = load_candles_map(self.markets, "15m", days=7)
                self.candles_1h = load_candles_map(self.markets, "1h", days=14)
                self.candles_4h = load_candles_map(self.markets, "4h", days=30)
                self.candles_1d = load_candles_map(self.markets, "1d", days=60)
                
                # Set last update timestamps to prevent immediate re-fetch
                now = datetime.now(timezone.utc)
                for symbol in self.markets:
                    self.last_candle_update[symbol] = {} # Initialize for symbol
                    
                    last_15m = self.candles_15m[symbol][-1].timestamp if self.candles_15m.get(symbol) and self.candles_15m[symbol] else datetime.min.replace(tzinfo=timezone.utc)
                    self.last_candle_update[symbol]["15m"] = last_15m
                    
                    last_1h = self.candles_1h[symbol][-1].timestamp if self.candles_1h.get(symbol) and self.candles_1h[symbol] else datetime.min.replace(tzinfo=timezone.utc)
                    self.last_candle_update[symbol]["1h"] = last_1h
                    
                    last_4h = self.candles_4h[symbol][-1].timestamp if self.candles_4h.get(symbol) and self.candles_4h[symbol] else datetime.min.replace(tzinfo=timezone.utc)
                    self.last_candle_update[symbol]["4h"] = last_4h
                    
                    last_1d = self.candles_1d[symbol][-1].timestamp if self.candles_1d.get(symbol) and self.candles_1d[symbol] else datetime.min.replace(tzinfo=timezone.utc)
                    self.last_candle_update[symbol]["1d"] = last_1d

                logger.info(f"Hydration complete in {(datetime.now() - start_hydrate).total_seconds():.2f}s")
                
            except Exception as e:
                logger.error("Failed to hydrate cache from DB", error=str(e))
                # Fallback: continue empty, will fetch from API
            
            await self.data_acq.start() # Start data acquisition directly
            
            # Main Loop
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
            
    def _convert_to_position(self, data: Dict) -> Position:
        """Convert raw exchange position dict to Position domain object."""
        # Handle key variations (CCXT vs Raw vs Internal)
        symbol = data.get('symbol')
        
        # Parse Side
        side_raw = data.get('side', 'long').lower()
        side = Side.LONG if side_raw in ['long', 'buy'] else Side.SHORT
        
        # Parse Numerics
        size = Decimal(str(data.get('size', 0)))
        entry_price = Decimal(str(data.get('entryPrice', data.get('entry_price', 0))))
        mark_price = Decimal(str(data.get('markPrice', data.get('mark_price', 0))))
        liq_price = Decimal(str(data.get('liquidationPrice', data.get('liquidation_price', 0))))
        unrealized_pnl = Decimal(str(data.get('unrealizedPnl', data.get('unrealized_pnl', 0))))
        leverage = Decimal(str(data.get('leverage', 1)))
        margin_used = Decimal(str(data.get('initialMargin', data.get('margin_used', 0))))
        
        if mark_price == 0:
            # Fallback for mark price if missing
             mark_price = entry_price
             
        # Calculate Notional
        size_notional = size * mark_price
        
        return Position(
            symbol=symbol,
            side=side,
            size=size,
            size_notional=size_notional,
            entry_price=entry_price,
            current_mark_price=mark_price,
            liquidation_price=liq_price,
            unrealized_pnl=unrealized_pnl,
            leverage=leverage,
            margin_used=margin_used,
            opened_at=datetime.now(timezone.utc) # Approximate if missing
        )

    async def _sync_positions(self) -> List[Dict]:
        """
        Sync active positions from exchange and update RiskManager.
        
        Returns:
            List of active positions (dicts)
        """
        raw_positions = await self.client.get_all_futures_positions()
        
        # Convert to Domain Objects
        active_positions = []
        for p in raw_positions:
            try:
                pos_obj = self._convert_to_position(p)
                active_positions.append(pos_obj)
            except Exception as e:
                logger.error("Failed to convert position object", data=str(p), error=str(e))
        
        # Update Risk Manager
        self.risk_manager.update_position_list(active_positions)
        
        # Persist to DB for Dashboard
        try:
             sync_active_positions(self.risk_manager.current_positions)
        except Exception as e:
             logger.error("Failed to sync positions to DB", error=str(e))
        
        # ALWAYS log position count for debugging
        logger.info(
            f"Active Portfolio: {len(active_positions)} positions", 
            symbols=[p.symbol for p in active_positions]
        )
        
        return raw_positions

    async def _tick(self):
        """
        Single iteration of live trading logic.
        Optimized for batch processing (Phase 10).
        """
        # 1. Check Data Health
        if not self.data_acq.is_healthy():
            logger.error("Data acquisition unhealthy")
            return

        # 2. Sync Active Positions (Global Sync)
        try:
            # This updates global state in Repository and internal trackers
            # We also get the raw list here to use for the tick
            all_raw_positions = await self.client.get_all_futures_positions()
            # We still run sync to ensure DB peristence is up to date
            await self._sync_positions()
        except Exception as e:
            logger.error("Failed to sync positions", error=str(e))
            return

        # 3. Batch Data Fetching (Optimization)
        try:
            # Fetch ALL spot tickers (chunked inside client)
            map_spot_tickers = await self.client.get_spot_tickers_bulk(self.markets)
            
            # Fetch ALL futures mark prices
            map_futures_tickers = await self.client.get_futures_tickers_bulk()
            
            # Map positions by symbol for O(1) loopup
            map_positions = {p['symbol']: p for p in all_raw_positions}
            
        except Exception as e:
            logger.error("Failed batch data fetch", error=str(e))
            return

        # 4. Parallel Analysis Loop
        # Semaphore to control concurrency (e.g. 20 coins at a time for candle fetching)
        sem = asyncio.Semaphore(20)
        
        async def process_coin(spot_symbol: str):
            async with sem:
                try:
                    # Context
                    futures_symbol = self.futures_adapter.map_spot_to_futures(spot_symbol)
                    
                    # Get Data from Bulk Cache
                    if spot_symbol not in map_spot_tickers:
                        return # Skip if no data
                        
                    spot_ticker = map_spot_tickers[spot_symbol]
                    spot_price = Decimal(str(spot_ticker['last']))
                    
                    # Resolve futures mark price
                    # Try direct match or mapped match
                    mark_price = None
                    if futures_symbol in map_futures_tickers:
                        mark_price = map_futures_tickers[futures_symbol]
                    else:
                        # Try logic lookup (e.g. PF_XBTUSD)
                        # Quick dirty check for specific mapping if known keys differ
                        # For now rely on exact or adapter map
                        pass
                        
                    if not mark_price:
                        # Fallback for critical pricing (or skip)
                        # Logging too much here might spam, so only debug
                        # logger.debug(f"Missing mark price for {futures_symbol}")
                        return 

                    # Update Candles (This still does I/O but is parallelized now)
                    await self._update_candles(spot_symbol)
                    
                    # Position Management
                    position_data = map_positions.get(futures_symbol)
                    if position_data:
                        # Management Logic
                        symbol = position_data['symbol']
                         # Ensure tracked
                        if symbol not in self.managed_positions:
                             self.managed_positions[symbol] = self._init_managed_position(position_data, mark_price)
                        
                        managed_pos = self.managed_positions[symbol]
                        managed_pos.current_mark_price = mark_price
                        managed_pos.unrealized_pnl = Decimal(str(position_data.get('unrealized_pnl', 0))) # Key corrected from raw API
                        managed_pos.size = Decimal(str(position_data['size']))
                        
                        actions = self.position_manager.evaluate(managed_pos, mark_price)
                        if actions:
                            await self._execute_management_actions(symbol, actions, managed_pos)
                            
                    # Signal Generation (SMC)
                    # Use 15m candles (primary timeframe)
                    # NOTE: _update_candles ensures self.candles_15m is populated
                    candles = self.candles_15m.get(spot_symbol, [])
                    if len(candles) < 50:
                        return

                    signal = self.smc_engine.generate_signal(
                        spot_symbol,
                        candles,
                        self.candles_1h.get(spot_symbol, []),
                        self.candles_4h.get(spot_symbol, []),
                        self.candles_1d.get(spot_symbol, [])
                    )
                    
                    # Pass context to signal for execution (mark price for futures)
                    # Signal is spot-based, execution is futures-based.
                    
                    if signal.signal_type != SignalType.NO_SIGNAL:
                         await self._handle_signal(signal, spot_price, mark_price)
                    
                    # Trace Logging (Throttled)
                    now = datetime.now(timezone.utc)
                    last_trace = self.last_trace_log.get(spot_symbol, datetime.min.replace(tzinfo=timezone.utc))
                    
                    if (now - last_trace).total_seconds() > 300: # 5 minutes
                        try:
                            trace_details = {
                                "signal": signal.signal_type.value,
                                "regime": signal.regime,
                                "bias": signal.higher_tf_bias,
                                "adx": float(signal.adx) if signal.adx else 0.0,
                                "atr": float(signal.atr) if signal.atr else 0.0,
                                "ema200_slope": signal.ema200_slope,
                                "spot_price": float(spot_price),
                                "setup_quality": sum(float(v) for v in (signal.score_breakdown or {}).values()),
                                "score_breakdown": signal.score_breakdown or {}
                            }
                            
                            record_event(
                                event_type="DECISION_TRACE",
                                symbol=spot_symbol,
                                details=trace_details,
                                timestamp=now
                            )
                            self.last_trace_log[spot_symbol] = now
                        except Exception as e:
                            logger.error("Failed to record decision trace", symbol=spot_symbol, error=str(e))

                except Exception as e:
                    logger.error(f"Error processing {spot_symbol}", error=str(e))

        # Execute parallel processing
        await asyncio.gather(*[process_coin(s) for s in self.markets], return_exceptions=True)
        
        # 4.5 CRITICAL: Validate all positions have stop loss protection
        await self._validate_position_protection()
        
        # 5. Account Sync (Throttled)
        now = datetime.now(timezone.utc)
        if (now - self.last_account_sync).total_seconds() > 15:
            await self._sync_account_state()
            self.last_account_sync = now
            
        # 7. Operational Maintenance (Daily)
        if (now - self.last_maintenance_run).total_seconds() > 86400: # 24 hours
            try:
                results = self.db_pruner.run_maintenance()
                logger.info("Daily database maintenance complete", results=results)
                self.last_maintenance_run = now
            except Exception as e:
                logger.error("Daily maintenance failed", error=str(e))

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
    
    async def _validate_position_protection(self):
        """CRITICAL: Ensure all open positions have stop loss orders."""
        try:
            all_positions = await self.client.get_all_futures_positions()
            
            for pos in all_positions:
                symbol = pos['symbol']
                
                # Check if position has protective orders in managed_positions
                if symbol in self.managed_positions:
                    managed_pos = self.managed_positions[symbol]
                    
                    # CRITICAL CHECK: Stop loss must be set
                    if not managed_pos.initial_stop_price:
                        logger.critical(
                            f"🚨 UNPROTECTED POSITION: {symbol} has NO STOP LOSS!",
                            size=str(pos['size']),
                            entry=str(pos['entry_price']),
                            unrealized_pnl=str(pos.get('unrealized_pnl', 0))
                        )
                        # TODO: Emergency stop loss placement could go here
                        # For now, just alert loudly
                else:
                    # Position exists but not in managed_positions - this is also critical
                    logger.critical(
                        f"🚨 UNMANAGED POSITION: {symbol} exists but not tracked!",
                        size=str(pos['size']),
                        entry=str(pos['entry_price'])
                    )
        except Exception as e:
            logger.error("Failed to validate position protection", error=str(e))
    
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
            
        if decision.approved:
            # OPPORTUNITY COST REPLACEMENT
            if decision.should_close_existing and decision.close_symbol:
                logger.warning(
                    "Executing Opportunity Cost Replacement",
                    closing=decision.close_symbol,
                    opening=signal.symbol
                )
                try:
                    await self.client.close_position(decision.close_symbol)
                    # Force remove from local state to clear slot immediately
                    if decision.close_symbol in self.managed_positions:
                        del self.managed_positions[decision.close_symbol]
                    # Also update RiskManager immediately
                    self.risk_manager.current_positions = [
                        p for p in self.risk_manager.current_positions 
                        if p.symbol != decision.close_symbol
                    ]
                except Exception as e:
                    logger.error("Failed to execute replacement close", symbol=decision.close_symbol, error=str(e))
                    # Proceed anyway? Or abort? 
                    # If close failed, we might exceed limits. Better to abort.
                    logger.error("Aborting new position entry due to failed replacement")
                    return

            # Execute Entry
            order_intent = self.execution_engine.generate_entry_plan( # Reverted to original method name and args
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
             logger.info("Entry order placed", order_id=entry_order.order_id)
             
             # 5. Place Protective Orders (Immediate Safety)
             # We place SL immediately to prevent naked positions.
             tps = order_intent.get('take_profits', [])
             tp1 = tps[0]['price'] if len(tps) > 0 else None
             tp2 = tps[1]['price'] if len(tps) > 1 else None
             
             sl_order, tp_order = await self.executor.place_protective_orders(
                 entry_order,
                 intent_model.stop_loss_futures,
                 take_profit_price=tp1 # Primary TP on exchange
             )
             
             sl_id = sl_order.order_id if sl_order else None
             if sl_id:
                 logger.info("Protective SL placed", order_id=sl_id)
             else:
                 logger.critical("FAILED TO PLACE STOP LOSS", symbol=signal.symbol)
             
             tp_ids = []
             if tp_order:
                 tp_ids.append(tp_order.order_id)
             
             # V3: Initialize Active Trade Management State
             # We optimisticly track the position with its immutable intents
             futures_symbol = self.futures_adapter.map_spot_to_futures(signal.symbol)
             tps = order_intent['take_profits']
             tp1 = tps[0]['price'] if len(tps) > 0 else None
             tp2 = tps[1]['price'] if len(tps) > 1 else None
             
             v3_pos = Position(
                 symbol=futures_symbol,
                 side=intent_model.side,
                 size=Decimal("0"), # Pending Fill
                 size_notional=intent_model.size_notional,
                 entry_price=mark_price, # Est
                 current_mark_price=mark_price,
                 liquidation_price=Decimal("0"),
                 unrealized_pnl=Decimal("0"),
                 leverage=intent_model.leverage,
                 margin_used=Decimal("0"),
                 opened_at=datetime.now(timezone.utc),
                 
                 # V3 Immutable Parameters
                 initial_stop_price=intent_model.stop_loss_futures,
                 trade_type=signal.regime,
                 tp1_price=tp1,
                 tp2_price=tp2,
                 partial_close_pct=Decimal("0.5"), # Default config
                 
                 # ID Linking
                 stop_loss_order_id=sl_id, 
                 tp_order_ids=tp_ids
             )
             
             self.managed_positions[futures_symbol] = v3_pos
             logger.info("V3 Position State initialized", symbol=futures_symbol)
             
             # Trade persistence happens on exit defined in Rules 11


    async def _update_candles(self, symbol: str):
        """Update local candle caches from acquisition with throttling."""
        
        now = datetime.now(timezone.utc)
        if symbol not in self.last_candle_update:
            self.last_candle_update[symbol] = {}
            
        async def fetch_tf(tf: str, buffer: Dict[str, List[Candle]], interval_min: int):
            # Check cache
            last_update = self.last_candle_update[symbol].get(tf, datetime.min.replace(tzinfo=timezone.utc))
            if (now - last_update).total_seconds() < (interval_min * 60):
                return # Cache hit
                
            candles = await self.client.get_spot_ohlcv(symbol, tf, limit=10)
            if not candles: return
            
            # Update Cache
            self.last_candle_update[symbol][tf] = now
            
            existing = buffer.get(symbol, [])
            # ... (rest of merge logic would be here, but we just replace or append)
            # For simplicity/robustness in live, we can just replace usage with latest snippet
            # BUT we need history. 
            # Smart merge:
            if not existing:
                buffer[symbol] = candles
            else:
                 # Append new ones
                 last_ts = existing[-1].timestamp
                 new_candles = [c for c in candles if c.timestamp > last_ts]
                 buffer[symbol].extend(new_candles)
                 # Limit buffer size
                 if len(buffer[symbol]) > 500:
                     buffer[symbol] = buffer[symbol][-500:]

        # Parallel fetch with thresholds
        # 15m -> 1 min update
        # 1h -> 5 min update 
        # 4h -> 15 min update
        # 1d -> 60 min update
        await asyncio.gather(
            fetch_tf("15m", self.candles_15m, 1),
            fetch_tf("1h", self.candles_1h, 5),
            fetch_tf("4h", self.candles_4h, 15),
            fetch_tf("1d", self.candles_1d, 60)
        )

    def _init_managed_position(self, exchange_data: Dict, mark_price: Decimal) -> Position:
        """Hydrate Position object from exchange data (for recovery)."""
        logger.warning(f"Hydrating position for {exchange_data['symbol']} without V3 params (Recovery)")
        
        # Defensive: Ensure required keys exist
        if 'entry_price' not in exchange_data:
            logger.error(f"Missing 'entry_price' in exchange data for {exchange_data.get('symbol', 'UNKNOWN')}", data_keys=list(exchange_data.keys()))
            raise ValueError(f"Cannot hydrate position: missing entry_price")
        
        return Position(
            symbol=exchange_data['symbol'],
            side=Side.LONG if exchange_data['side'] == 'long' else Side.SHORT,
            size=Decimal(str(exchange_data['size'])),
            size_notional=Decimal("0"), # Unknown without calc
            entry_price=Decimal(str(exchange_data['entry_price'])),  # FIX: was 'price'
            current_mark_price=mark_price,
            liquidation_price=Decimal(str(exchange_data.get('liquidationPrice', 0))),
            unrealized_pnl=Decimal(str(exchange_data.get('unrealizedPnl', 0))),
            leverage=Decimal("1"), # Approx
            margin_used=Decimal("0"),
            opened_at=datetime.now(timezone.utc),
            
            # Init V3 defaults (safe fallback)
            initial_stop_price=None,
            tp1_price=None,
            tp2_price=None,
            final_target_price=None,
            partial_close_pct=Decimal("0.5"),
            original_size=Decimal(str(exchange_data['size'])),
        )

    async def _execute_management_actions(self, symbol: str, actions: List[ManagementAction], position: Position):
        """Execute logic actions decided by PositionManager."""
        for action in actions:
            logger.info(f"V3 Action: {action.type.value}", symbol=symbol, reason=action.reason)
            
            try:
                if action.type == ActionType.CLOSE_POSITION:
                    # Market Close
                    await self.client.close_position(symbol)
                    # State update handled on next tick (position gone)
                    
                elif action.type == ActionType.PARTIAL_CLOSE:
                    # Place market reduce-only order
                    # Invert side
                    exit_side = 'sell' if position.side == Side.LONG else 'buy'
                    await self.client.place_futures_order(
                         symbol=symbol,
                         side=exit_side,
                         order_type='market',
                         size=float(action.quantity),
                         reduce_only=True
                    )
                    # Update internal state (flags)
                    if "TP1" in action.reason:
                        position.tp1_hit = True
                    if "TP2" in action.reason:
                        position.tp2_hit = True

                elif action.type == ActionType.UPDATE_STATE:
                    if "Intent Confirmed" in action.reason:
                        position.intent_confirmed = True
                        
                elif action.type == ActionType.UPDATE_STOP:
                    # Requires Order Management
                    # If we track SL order ID:
                    if position.stop_loss_order_id:
                        await self.client.edit_futures_order(
                            order_id=position.stop_loss_order_id,
                            symbol=symbol,
                            stop_price=float(action.price)
                        )
                    else:
                        logger.warning("Cannot update stop - no SL Order ID tracked", symbol=symbol)
                        
            except Exception as e:
                logger.error(f"Failed to execute {action.type}", symbol=symbol, error=str(e))
