import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional
from asyncio import Queue, QueueEmpty
from decimal import Decimal

from src.config.config import Config
from src.monitoring.logger import get_logger, setup_logging
from src.ipc.messages import MarketUpdate, ServiceCommand, ServiceStatus
from src.domain.models import Candle, Signal, SignalType, Position, Side, OrderType, OrderIntent, RiskDecision
from src.strategy.smc_engine import SMCEngine
from src.risk.risk_manager import RiskManager
from src.execution.executor import Executor
from src.data.kraken_client import KrakenClient
from src.execution.price_converter import PriceConverter
from src.execution.futures_adapter import FuturesAdapter
from src.storage.repository import async_record_event, sync_active_positions, save_account_state
from src.execution.position_manager import PositionManager, ActionType, ManagementAction
from src.execution.execution_engine import ExecutionEngine
from src.storage.maintenance import DatabasePruner
from src.monitoring.kill_switch import get_kill_switch

logger = get_logger("TradingService")

class TradingService:
    """
    Async Service for Tick Processing, Strategy Analysis, and Order Execution.
    Runs as a Task within the main event loop.
    """
    def __init__(self, input_queue: Queue, command_queue: Queue, config: Config):
        self.input_queue = input_queue
        self.command_queue = command_queue
        self.config = config
        self.active = True
        
        # Cache State
        self.candles_15m = {}
        self.candles_1h = {}
        self.candles_4h = {}
        self.candles_1d = {}
        
        # Throttling
        self.last_analysis: Dict[str, datetime] = {}
        self.last_account_sync = datetime.min.replace(tzinfo=timezone.utc)
        self.last_maintenance_run = datetime.min.replace(tzinfo=timezone.utc)
        self.last_trace_log: Dict[str, datetime] = {}
        
    async def start(self):
        logger.info("Trading Service Task Starting...")
        try:
            await self._service_loop()
        except asyncio.CancelledError:
            logger.info("Trading Service Cancelled")
        except Exception as e:
            logger.critical(f"Trading Service Crashed: {e}", exc_info=True)

    async def _service_loop(self):
        # Init Components
        self.kraken = KrakenClient(
            api_key=self.config.exchange.api_key,
            api_secret=self.config.exchange.api_secret,
            futures_api_key=self.config.exchange.futures_api_key,
            futures_api_secret=self.config.exchange.futures_api_secret,
            use_testnet=self.config.exchange.use_testnet
        )
        await self.kraken.initialize()
        
        self.price_converter = PriceConverter()
        self.futures_adapter = FuturesAdapter(self.kraken, max_leverage=self.config.risk.max_leverage)
        
        self.executor = Executor(
            self.config.execution, 
            self.futures_adapter
        )
        self.execution_engine = ExecutionEngine(self.config)
        self.risk_manager = RiskManager(self.config.risk)
        self.smc_engine = SMCEngine(self.config.strategy)
        
        # Position Management V2
        self.position_manager = PositionManager()
        self.managed_positions: Dict[str, Position] = {}
        
        # Maintenance
        self.db_pruner = DatabasePruner()
        self.kill_switch = get_kill_switch()
        
        # Initial Sync
        try:
            # Wrap in timeout to prevent startup hang
            await asyncio.wait_for(self.executor.sync_open_orders(), timeout=45.0)
        except asyncio.TimeoutError:
            logger.error("Timeout during initial order sync - proceeding with caution")
        except Exception as e:
            logger.error(f"Failed to sync orders: {e}")
        
        self._send_status("RUNNING", {"msg": "Initialized"})
        
        # Start Independent Position Management Loop
        asyncio.create_task(self._position_management_loop())
        
        while self.active:
            try:
                # 1. Kill Switch Check (HIGHEST PRIORITY)
                if self.kill_switch.is_active():
                    logger.critical("Kill switch active - halting Trading")
                    await asyncio.sleep(60)
                    continue

                # 2. Order Timeout Monitoring
                try:
                    cancelled_count = await self.executor.check_order_timeouts()
                    if cancelled_count > 0:
                        logger.warning(f"Cancelled {cancelled_count} expired orders")
                except Exception as e:
                    logger.error(f"Failed to check order timeouts: {e}")

                # 3. Account Sync (Throttled 60s)
                now = datetime.now(timezone.utc)
                if (now - self.last_account_sync).total_seconds() > 60:
                    await self._sync_account_state()
                    self.last_account_sync = now
                    
                # 4. Drainage Input Queue (Batch Processing)
                processed = 0
                while not self.input_queue.empty() and processed < 100:
                    msg = self.input_queue.get_nowait()
                    if isinstance(msg, MarketUpdate):
                        await self._handle_market_update(msg)
                    processed += 1
                    
                # 5. Process Commands
                while not self.command_queue.empty():
                    cmd = self.command_queue.get_nowait()
                    if cmd.command == "STOP":
                        self.active = False
                        break
                
                # 6. Operational Maintenance (Daily)
                if (now - self.last_maintenance_run).total_seconds() > 86400:
                    try:
                        results = self.db_pruner.run_maintenance()
                        logger.info("Daily database maintenance complete", results=results)
                        self.last_maintenance_run = now
                    except Exception as e:
                        logger.error(f"Maintenance failed: {e}")

            except QueueEmpty:
                pass
            except Exception as e:
                logger.error(f"Error in Trading Loop: {e}")
                
            if not self.active: break
            
            # Explicit Garbage Collection (Help combat fragmentation on small instances)
            # Run every ~100 loops (1 second sleep = 100 seconds) or use timer.
            # Using simple modulo for now.
            if processed > 0: # Only if we did work
                 import gc
                 gc.collect()

            # Yield to loop
            await asyncio.sleep(0.01)
            
        logger.info("Trading Service Shutting Down...")

    async def _fetch_stop_loss_for_position(self, symbol: str, side: Side) -> Optional[Decimal]:
        """
        Fetch existing stop loss order price for a position from exchange.
        
        Returns:
            Stop loss price if found, None otherwise
        """
        try:
            open_orders = await self.kraken.get_futures_open_orders()
            for order in open_orders:
                if order.get('symbol') == symbol:
                    # Check if it's a stop loss order (reduce-only, opposite side)
                    order_side = order.get('side', '').lower()
                    is_reduce_only = order.get('reduceOnly', order.get('reduce_only', False))
                    order_type = order.get('type', '').lower()
                    
                    # Stop loss should be opposite side and reduce-only
                    expected_side = 'sell' if side == Side.LONG else 'buy'
                    if (order_side == expected_side and 
                        is_reduce_only and 
                        ('stop' in order_type or 'stop_loss' in order_type or order_type == 'stop')):
                        price = order.get('price') or order.get('stopPrice') or order.get('triggerPrice')
                        if price:
                            return Decimal(str(price))
        except Exception as e:
            logger.error(f"Failed to fetch stop loss for {symbol}", error=str(e))
        return None

    async def _calculate_emergency_stop_loss(
        self, 
        symbol: str,
        side: Side,
        entry_price: Decimal,
        current_price: Decimal,
        liquidation_price: Decimal
    ) -> Optional[Decimal]:
        """
        Calculate a safe emergency stop loss for an unprotected position.
        
        Uses risk-per-trade to determine stop distance, ensuring it's far enough
        from liquidation price.
        """
        try:
            # Calculate risk per trade (0.3% default)
            risk_pct = Decimal(str(self.config.risk.risk_per_trade_pct))
            
            # For LONG: stop below entry, for SHORT: stop above entry
            if side == Side.LONG:
                # Stop loss should be risk_pct below entry
                stop_price = entry_price * (Decimal("1") - risk_pct)
                
                # Ensure stop is at least 35% above liquidation (safety buffer)
                if liquidation_price > Decimal("0"):
                    min_stop = liquidation_price * Decimal("1.35")
                    if stop_price < min_stop:
                        stop_price = min_stop
                    
            else:  # SHORT
                # Stop loss should be risk_pct above entry
                stop_price = entry_price * (Decimal("1") + risk_pct)
                
                # Ensure stop is at least 35% below liquidation (safety buffer)
                if liquidation_price > Decimal("0"):
                    max_stop = liquidation_price * Decimal("0.65")
                    if stop_price > max_stop:
                        stop_price = max_stop
            
            return stop_price
        except Exception as e:
            logger.error(f"Failed to calculate emergency stop for {symbol}", error=str(e))
            return None

    async def _position_management_loop(self):
        """Continuous loop to monitor and manage active positions."""
        logger.info("Starting Position Management Loop...")
        while self.active:
            try:
                # 1. Fetch Open Positions
                # We fetch fresh from API to ensure Truth
                positions = await self.kraken.get_all_futures_positions()
                
                if positions:
                    logger.info(f"Active Portfolio: {len(positions)} positions", symbols=[p['symbol'] for p in positions])
                
                for pos_data in positions:
                    symbol = pos_data['symbol']
                    
                    # Ensure managed
                    if symbol not in self.managed_positions:
                        # Hydrate minimal managed position (Blind adoption)
                        logger.warning(f"Adopting unmanaged position: {symbol}")
                        
                        entry_price = Decimal(str(pos_data.get('entryPrice', pos_data.get('entry_price', 0))))
                        mark_price = Decimal(str(pos_data.get('markPrice', 0)))
                        liquidation_price = Decimal(str(pos_data.get('liquidationPrice', pos_data.get('liquidation_price', 0))))
                        side = Side.LONG if pos_data['side'] == 'long' else Side.SHORT

                        # CRITICAL FIX: Fetch existing stop loss from exchange
                        stop_loss_price = await self._fetch_stop_loss_for_position(symbol, side)
                        
                        # If no stop loss found, calculate and place emergency stop
                        if not stop_loss_price or stop_loss_price == Decimal("0"):
                            logger.critical(
                                f"🚨 UNPROTECTED POSITION: {symbol} has NO STOP LOSS! Placing emergency stop...",
                                entry=str(entry_price),
                                mark=str(mark_price),
                                liquidation=str(liquidation_price)
                            )
                            
                            # Calculate safe emergency stop
                            emergency_stop = await self._calculate_emergency_stop_loss(
                                symbol,
                                side,
                                entry_price,
                                mark_price,
                                liquidation_price
                            )
                            
                            if emergency_stop:
                                try:
                                    # Place emergency stop loss order
                                    protective_side = 'sell' if side == Side.LONG else 'buy'
                                    sl_order = await self.kraken.place_futures_order(
                                        symbol=symbol,
                                        side=protective_side,
                                        order_type='stop',
                                        size=float(pos_data['size']),
                                        stop_price=float(emergency_stop),
                                        reduce_only=True
                                    )
                                    stop_loss_price = emergency_stop
                                    logger.info(
                                        f"✅ Emergency stop loss placed for {symbol}",
                                        order_id=sl_order.get('id'),
                                        stop_price=str(emergency_stop)
                                    )
                                except Exception as e:
                                    logger.critical(
                                        f"❌ FAILED to place emergency stop for {symbol}",
                                        error=str(e)
                                    )
                                    # Still set the price for monitoring even if order failed
                                    stop_loss_price = emergency_stop

                        self.managed_positions[symbol] = Position(
                            symbol=symbol,
                            side=side,
                            size=Decimal(str(pos_data['size'])),
                            entry_price=entry_price,
                            current_mark_price=mark_price,
                            unrealized_pnl=Decimal(str(pos_data.get('unrealizedPnl', 0))),
                            opened_at=datetime.now(timezone.utc),
                            # CRITICAL FIX: Set actual stop loss price, not 0
                            initial_stop_price=stop_loss_price if stop_loss_price else None,
                            trade_type="MANUAL",
                            
                            # Required fields
                            size_notional=Decimal(str(pos_data['size'])) * mark_price,
                            liquidation_price=liquidation_price if liquidation_price > Decimal("0") else None,
                            leverage=Decimal("1.0"),
                            margin_used=Decimal(str(pos_data.get('margin_used', 0)))
                        )
                    
                    managed_pos = self.managed_positions[symbol]
                    # Update Live Data
                    managed_pos.current_mark_price = Decimal(str(pos_data.get('markPrice', 0)))
                    managed_pos.unrealized_pnl = Decimal(str(pos_data.get('unrealizedPnl', 0)))
                    managed_pos.size = Decimal(str(pos_data['size']))
                    
                    # Update liquidation price if available
                    liq_price = Decimal(str(pos_data.get('liquidationPrice', pos_data.get('liquidation_price', 0))))
                    if liq_price > Decimal("0"):
                        managed_pos.liquidation_price = liq_price
                    
                    # Evaluate Exit Strategy
                    actions = self.position_manager.evaluate(managed_pos, managed_pos.current_mark_price)
                    if actions:
                        await self._execute_management_actions(symbol, actions, managed_pos)
                
                # 2. Position Protection Validation (CRITICAL)
                await self._validate_position_protection(positions)
                        
            except Exception as e:
                logger.error(f"Position Management Error: {e}")
            
            # Check every 10 seconds
            await asyncio.sleep(10.0)

    def _check_data_freshness(self, symbol: str) -> bool:
        """
        Verify data is fresh enough for trading.
        
        Returns:
            True if data is fresh, False if stale
        """
        now = datetime.now(timezone.utc)
        
        # Check 15m data (should be <30 mins old)
        c15m = self.candles_15m.get(symbol, [])
        if not c15m or (now - c15m[-1].timestamp).total_seconds() > 1800:
            logger.warning(
                f"Stale 15m data for {symbol}",
                last_update=c15m[-1].timestamp if c15m else "None",
                age_mins=(now - c15m[-1].timestamp).total_seconds() / 60 if c15m else "N/A"
            )
            return False
        
        # Check 1d data (should be <48 hours old)
        c1d = self.candles_1d.get(symbol, [])
        if not c1d or (now - c1d[-1].timestamp).total_seconds() > 172800:
            logger.warning(
                f"Stale 1d data for {symbol}",
                last_update=c1d[-1].timestamp if c1d else "None",
                age_hours=(now - c1d[-1].timestamp).total_seconds() / 3600 if c1d else "N/A"
            )
            return False
        
        return True

    # ... (rest of methods)

    async def _handle_market_update(self, msg: MarketUpdate):
        symbol = msg.symbol
        tf = msg.timeframe
        
        # 1. Update Cache
        target_map = getattr(self, f"candles_{tf}", None)
        if target_map is None: return 
        
        current = target_map.get(symbol, [])
        new_data = msg.candles
        
        # Simple Merge logic
        if not current:
            target_map[symbol] = new_data
        else:
            merged = {c.timestamp: c for c in current}
            for c in new_data:
                merged[c.timestamp] = c
            target_map[symbol] = sorted(merged.values(), key=lambda x: x.timestamp)
            # Cap size - Reduced from 1000 to 300 to prevent memory exhaustion
            if len(target_map[symbol]) > 300:
                target_map[symbol] = target_map[symbol][-300:]
            
            if tf == "1h":
                 logger.info(f"TradingService: Updated 1h cache for {symbol}", size=len(target_map[symbol]), last_ts=target_map[symbol][-1].timestamp if target_map[symbol] else "None")
                 # Trigger analysis on 1h update too (to clear no_data/stale state if 1h arrived after 15m)
                 if not msg.is_historical:
                     await self._analyze_symbol(symbol)
            
            if tf == "1d":
                 logger.info(f"TradingService: Updated 1d cache for {symbol}", size=len(target_map[symbol]), last_ts=target_map[symbol][-1].timestamp if target_map[symbol] else "None")
            
            if tf == "15m" and len(target_map[symbol]) < 50:
                 logger.warning(f"TradingService: 15m cache shallow for {symbol} ({len(target_map[symbol])} candles)")
            
        # 2. Trigger Strategy (Live Signal)
        if not msg.is_historical and tf == "15m":
             await self._analyze_symbol(symbol)

    async def _analyze_symbol(self, symbol: str):
         # Skip if recently analyzed (Throttling)
         now = datetime.now(timezone.utc)
         last = self.last_analysis.get(symbol, datetime.min.replace(tzinfo=timezone.utc))
         if (now - last).total_seconds() < 10: # Max 1 analysis per 10s per symbol
             return
         self.last_analysis[symbol] = now

         # Ensure enough data
         c15m = self.candles_15m.get(symbol, [])
         if len(c15m) < 50: 
             logger.warning(f"Skipping analysis for {symbol}: Insufficient 15m data ({len(c15m)})")
             return
         
         # DATA STALENESS CHECK: Prevent trading on stale data
         if not self._check_data_freshness(symbol):
             logger.warning(f"Skipping analysis for {symbol}: Stale data detected")
             return
         
         
         signal = None
         # Run SMC Analysis
         try:
             c1h = self.candles_1h.get(symbol, [])
             c4h = self.candles_4h.get(symbol, [])
             c1d = self.candles_1d.get(symbol, [])
             
             last_candle_repr = str(c1h[-1]) if c1h else "None"
             # logger.debug(f"Analyzing {symbol}: 15m={len(c15m)}, 1h={len(c1h)}")

             signal = self.smc_engine.generate_signal(
                 symbol=symbol,
                 exec_candles_15m=c15m,
                 exec_candles_1h=c1h,
                 bias_candles_4h=c4h,
                 bias_candles_1d=c1d
             )
             
             if signal.signal_type != SignalType.NO_SIGNAL:
                 logger.info(f"SIGNAL FOUND: {symbol} {signal.signal_type} {signal.regime} Score={signal.score}")
                 
                 # Current Price (from last candle)
                 trigger_price = c15m[-1].close
             else:
                 logger.info(f"NO SIGNAL for {symbol}: Regime={signal.regime} Reason={signal.reasoning}")

                 
                 # Determine Futures Symbol
                 futures_symbol = self.futures_adapter.map_spot_to_futures(symbol)
                 if futures_symbol:
                     # 1. Fetch Account Equity (Futures Balance)
                     # In V2, we might not have direct client access in same way, but we have self.kraken
                     # However, DataService handles polling. TradingService can fetch balance via client on demand
                     # or rely on an account_state cache. For safety, let's fetch fresh.
                     try:
                         balance = await self.kraken.get_futures_balance()
                         base_currency = getattr(self.config.exchange, "base_currency", "USD")
                         equity = Decimal(str(balance.get('total', {}).get(base_currency, 0)))
                     except Exception as e:
                         logger.error(f"Failed to fetch balance for risk check: {e}")
                         return

                     if equity <= 0:
                         logger.warning(f"Insufficient equity: {equity}")
                         return

                     # 2. Risk Validation (Safety Gate)
                     decision = self.risk_manager.validate_trade(
                        signal, equity, 
                        spot_price=Decimal(str(trigger_price)), 
                        perp_mark_price=Decimal(str(trigger_price)) # Approx if no mark available yet
                     )
                     
                     if not decision.approved:
                        logger.info(f"Trade rejected by Risk: {decision.rejection_reasons}")
                        return

                     # 3. Opportunity Cost Handling
                     if decision.should_close_existing and decision.close_symbol:
                         logger.warning(f"Opportunity Cost Replacement: Closing {decision.close_symbol} for {symbol}")
                         # Close existing
                         # We need to execute immediate close.
                         # self._execute_management_actions(...)
                         # For now, simplistic close:
                         try:
                             # We don't have management actions yet, direct close
                             await self.kraken.close_position(decision.close_symbol)
                             if decision.close_symbol in self.managed_positions:
                                 del self.managed_positions[decision.close_symbol]
                             # Update Risk Manager
                             self.risk_manager.current_positions = [
                                 p for p in self.risk_manager.current_positions 
                                 if p.symbol != decision.close_symbol
                             ]
                         except Exception as e:
                             logger.error(f"Failed to close replacement symbol {decision.close_symbol}: {e}")
                             return

                     # 4. Execute Entry
                     await self._execute_signal(signal, futures_symbol, Decimal(str(trigger_price)), decision)
                 
                 # 5. Record Decision Trace (Throttled 5m)
                 # await self._record_decision_trace(symbol, signal, trigger_price) <- Moved out
                          
         except Exception as e:
             logger.error(f"Analysis failed for {symbol}: {e}")
             
         # Record Trace regardless of signal (Visibility)
         # Trigger price from last candle if not set
         # Record Trace regardless of signal (Visibility)
         # Trigger price from last candle if not set
         if c15m and signal: 
            trigger_price = c15m[-1].close
            await self._record_decision_trace(symbol, signal, trigger_price)

    async def _execute_signal(self, signal: Signal, futures_symbol: str, price: Decimal, decision: RiskDecision):
        """Execute trade via Executor."""
        try:
             # 1. Generate Entry Plan
             # We use the execution engine helper (method from V1 we can assume is on engine or recreated here)
             order_intent_dict = self.execution_engine.generate_entry_plan(
                 signal,
                 decision.position_notional,
                 Decimal(str(signal.entry_price)),
                 price,
                 decision.leverage
             )
             
             # Convert to OrderIntent Object
             intent_model = OrderIntent(
                timestamp=datetime.now(timezone.utc),
                signal=signal,
                side=Side.LONG if signal.signal_type == SignalType.LONG else Side.SHORT,
                size_notional=decision.position_notional,
                leverage=decision.leverage,
                # Spot levels
                entry_price_spot=signal.entry_price,
                stop_loss_spot=signal.stop_loss,
                take_profit_spot=signal.take_profit,
                # Futures levels
                entry_price_futures=order_intent_dict['metadata']['fut_entry'],
                stop_loss_futures=order_intent_dict['metadata']['fut_sl'],
                take_profit_futures=order_intent_dict['take_profits'][0]['price'] if order_intent_dict['take_profits'] else None
             )

             logger.info(f"[EXECUTION] Placing trade for {futures_symbol} Size=${decision.position_notional:.2f}")

             # 2. Execute Entry
             entry_order = await self.executor.execute_signal(intent_model, price, [])
             
             if entry_order:
                 logger.info("Entry order placed", order_id=entry_order.order_id)
                 
                 # 3. Place Protective Orders
                 tps = order_intent_dict.get('take_profits', [])
                 tp1 = tps[0]['price'] if len(tps) > 0 else None
                 
                 sl_order, tp_order = await self.executor.place_protective_orders(
                     entry_order,
                     intent_model.stop_loss_futures,
                     take_profit_price=tp1
                 )

                 sl_id = sl_order.order_id if sl_order else None
                 tp_ids = []
                 if tp_order:
                     tp_ids.append(tp_order.order_id)
                     
                 if not sl_id:
                     logger.critical(f"FAILED TO PLACE STOP LOSS for {futures_symbol}!")

                 # 4. Initialize Position State
                 v3_pos = Position(
                     symbol=futures_symbol,
                     side=intent_model.side,
                     size=Decimal("0"), # Pending Fill
                     size_notional=intent_model.size_notional,
                     entry_price=price,
                     current_mark_price=price,
                     liquidation_price=Decimal("0"),
                     unrealized_pnl=Decimal("0"),
                     leverage=intent_model.leverage,
                     margin_used=Decimal("0"),
                     opened_at=datetime.now(timezone.utc),
                     
                     # Immutable Parameters
                     initial_stop_price=intent_model.stop_loss_futures,
                     trade_type=signal.regime,
                     tp1_price=tp1,
                     tp2_price=tps[1]['price'] if len(tps) > 1 else None,
                     partial_close_pct=Decimal("0.5"),
                     
                     # ID Linking
                     stop_loss_order_id=sl_id,
                     tp_order_ids=tp_ids
                 )
                 
                 self.managed_positions[futures_symbol] = v3_pos
                 logger.info("Position State initialized", symbol=futures_symbol)
                 
                 # Persist Event
                 await async_record_event("TRADE_EXECUTION", futures_symbol, {
                     "signal": signal.model_dump_json(),
                     "order_id": entry_order.order_id
                 })

        except Exception as e:
             logger.error(f"Execution failed: {e}")

    async def _execute_management_actions(self, symbol: str, actions: List[ManagementAction], position: Position):
        """Execute logic actions decided by PositionManager."""
        for action in actions:
            logger.info(f"Action: {action.type.value}", symbol=symbol, reason=action.reason)
            
            try:
                if action.type == ActionType.CLOSE_POSITION:
                    # Market Close
                    await self.kraken.close_position(symbol)
                    # State update handled on next loop (position gone)
                    
                elif action.type == ActionType.PARTIAL_CLOSE:
                    # Place market reduce-only order
                    exit_side = 'sell' if position.side == Side.LONG else 'buy'
                    await self.kraken.place_futures_order(
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
                    if position.stop_loss_order_id:
                        await self.kraken.edit_futures_order(
                            order_id=position.stop_loss_order_id,
                            symbol=symbol,
                            stop_price=float(action.price)
                        )
                    else:
                        logger.warning("Cannot update stop - no SL Order ID tracked", symbol=symbol)
                        
            except Exception as e:
                logger.error(f"Failed to execute {action.type}", symbol=symbol, error=str(e))

    async def _calculate_effective_equity(self, balance: Dict) -> tuple[Decimal, Decimal, Decimal]:
        """
        Calculate effective equity/margin from balance dict.
        Handlers:
        1. Multi-Collateral (Flex) - Using 'info' from Kraken
        2. Single-Collateral (Inverse) - Valuing crypto collateral manually
        3. Standard - Using base currency balance
        
        Returns:
            (equity, available_margin, margin_used)
        """
        # Default to standard CCXT total['USD']
        base_currency = getattr(self.config.exchange, "base_currency", "USD")
        total = balance.get('total', {})
        
        equity = Decimal(str(total.get(base_currency, 0)))
        avail_margin = Decimal(str(balance.get('free', {}).get(base_currency, 0)))
        margin_used = Decimal(str(balance.get('used', {}).get(base_currency, 0)))
        
        # 2. Check for Kraken Futures Multi-Collateral ("flex")
        info = balance.get('info', {})
        if info and 'accounts' in info and 'flex' in info['accounts']:
            flex = info['accounts']['flex']
            
            pv = flex.get('portfolioValue')
            am = flex.get('availableMargin')
            im = flex.get('initialMargin')
            
            if pv is not None:
                equity = Decimal(str(pv))
            if am is not None:
                avail_margin = Decimal(str(am))
            if im is not None:
                margin_used = Decimal(str(im))
                
            return equity, avail_margin, margin_used
        
        # 3. Logic for Single-Collateral (Inverse) or incomplete Flex data
        # If Equity is suspiciously low (< 10 USD) but we have crypto balance, calculate approximate equity
        if equity < 10:
            # Check for XBT/BTC/ETH
            for asset in ['XBT', 'BTC', 'ETH', 'SOL', 'USDT', 'USDC']:
                if asset == base_currency: 
                    continue
                    
                asset_qty = Decimal(str(total.get(asset, 0)))
                if asset_qty > 0:
                    try:
                        # Fetch price for valuation
                        ticker_symbol = f"{asset}/USD"
                        if asset == 'XBT': ticker_symbol = "BTC/USD"
                        
                        ticker = await self.kraken.get_ticker(ticker_symbol)
                        price = Decimal(str(ticker['last']))
                        
                        asset_equity = asset_qty * price
                        
                        equity += asset_equity
                        # Approximate available margin if strictly 0 (risky but better than 0)
                        if avail_margin == 0:
                            avail_margin += asset_equity
                            
                        logger.info(f"Valued non-USD collateral: {asset_qty} {asset} (~${asset_equity:,.2f})")
                    except Exception as ex:
                         logger.warning(f"Could not value collateral {asset}", error=str(ex))
                         
        return equity, avail_margin, margin_used

    async def _sync_account_state(self):
        """Update account balance and equity in DB."""
        try:
            balance_data = await self.kraken.get_futures_balance()
            
            # Use shared equity calculation logic (handles flex accounts)
            equity, available, used = await self._calculate_effective_equity(balance_data)
            
            # Unrealized PnL (if available)
            unrealized = Decimal(str(balance_data.get('unrealized_pnl', 0)))
            if unrealized == 0:
                unrealized = Decimal(str(balance_data.get('unrealizedPnl', 0)))

            # Persist for Dashboard
            def _save():
                save_account_state(
                    equity=equity,
                    balance=equity - unrealized, # Proxy for balance
                    margin_used=used,
                    available_margin=available,
                    unrealized_pnl=unrealized
                )
            
            await asyncio.to_thread(_save)
            
            # Update Risk Manager
            self.risk_manager.reset_daily_metrics(equity)
            
        except Exception as e:
            logger.error(f"Account state sync failed: {e}")

    async def _validate_position_protection(self, positions: List[Dict]):
        """Ensure every active position has a stop-loss order."""
        for pos_data in positions:
            symbol = pos_data['symbol']
            managed_pos = self.managed_positions.get(symbol)
            
            if not managed_pos:
                continue  # Will be handled by adoption logic
            
            # CRITICAL CHECK: Stop loss price must be set
            if not managed_pos.initial_stop_price or managed_pos.initial_stop_price == Decimal("0"):
                logger.critical(
                    f"🚨 UNPROTECTED POSITION: {symbol} has NO STOP LOSS PRICE!",
                    size=str(managed_pos.size),
                    entry=str(managed_pos.entry_price),
                    mark=str(managed_pos.current_mark_price),
                    unrealized_pnl=str(managed_pos.unrealized_pnl)
                )
                
                # Try to place emergency stop loss
                try:
                    liquidation_price = managed_pos.liquidation_price or Decimal(str(pos_data.get('liquidationPrice', pos_data.get('liquidation_price', 0))))
                    emergency_stop = await self._calculate_emergency_stop_loss(
                        symbol,
                        managed_pos.side,
                        managed_pos.entry_price,
                        managed_pos.current_mark_price,
                        liquidation_price
                    )
                    
                    if emergency_stop:
                        protective_side = 'sell' if managed_pos.side == Side.LONG else 'buy'
                        sl_order = await self.kraken.place_futures_order(
                            symbol=symbol,
                            side=protective_side,
                            order_type='stop',
                            size=float(managed_pos.size),
                            stop_price=float(emergency_stop),
                            reduce_only=True
                        )
                        managed_pos.initial_stop_price = emergency_stop
                        managed_pos.stop_loss_order_id = sl_order.get('id')
                        logger.info(
                            f"✅ Emergency stop loss placed for {symbol}",
                            order_id=sl_order.get('id'),
                            stop_price=str(emergency_stop)
                        )
                except Exception as e:
                    logger.critical(
                        f"❌ FAILED to place emergency stop for {symbol}",
                        error=str(e)
                    )
            
            # Also verify stop loss order exists on exchange
            elif not managed_pos.stop_loss_order_id:
                # Check if order exists on exchange
                existing_sl = await self._fetch_stop_loss_for_position(symbol, managed_pos.side)
                if existing_sl:
                    # Update our tracking
                    managed_pos.initial_stop_price = existing_sl
                    logger.info(f"✅ Found existing stop loss for {symbol}", price=str(existing_sl))
                else:
                    logger.warning(
                        f"⚠️  {symbol} has stop price but no order ID - verifying on exchange",
                        stop_price=str(managed_pos.initial_stop_price)
                    )

    async def _record_decision_trace(self, symbol: str, signal: Signal, price: Decimal):
        """Log throttled decision trace for debugging."""
        last = self.last_trace_log.get(symbol, datetime.min.replace(tzinfo=timezone.utc))
        now = datetime.now(timezone.utc)
        
        # Dynamic Throttle
        if signal.signal_type == SignalType.NO_SIGNAL:
            throttle = 600 # 10 mins for Heartbeats (Revised from 1 hour)
        else:
            throttle = 300 # 5 mins for Active Signals

        if (now - last).total_seconds() > throttle:
            # Get candle count for data depth
            c15m = self.candles_15m.get(symbol, [])
            candle_count = len(c15m)
            
            await async_record_event(
                "DECISION_TRACE", 
                symbol, 
                {
                    "spot_price": float(price),
                    "signal": signal.signal_type.value,
                    "regime": signal.regime,
                    "bias": signal.higher_tf_bias if hasattr(signal, 'higher_tf_bias') else "neutral",
                    "adx": float(signal.adx) if signal.adx else 0.0,
                    "atr": float(signal.atr) if signal.atr else 0.0,
                    "ema200_slope": signal.ema200_slope if hasattr(signal, 'ema200_slope') else "flat",
                    "setup_quality": signal.score if hasattr(signal, 'score') else 0.0,
                    "score_breakdown": signal.score_breakdown if hasattr(signal, 'score_breakdown') else {},
                    "candle_count": candle_count,
                    "status": "active",
                    "reasoning": signal.reasoning if hasattr(signal, 'reasoning') else ""
                }
            )
            self.last_trace_log[symbol] = now

    def _send_status(self, status: str, details: Dict = None):
        msg = ServiceStatus(
            service_name="TradingService",
            status=status,
            timestamp=datetime.now(timezone.utc),
            details=details
        )
        # We don't have an output queue for status?
        # TradingProcess is a consumer.
        # It can log status.
        logger.info(f"STATUS UPDATE: {status} {details}")

