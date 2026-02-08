import asyncio
import os
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import List, Dict, Optional, Any

from src.config.config import Config
from src.services.market_discovery import MarketDiscoveryService
from src.monitoring.logger import get_logger
from src.data.fiat_currencies import has_disallowed_base
from src.data.kraken_client import KrakenClient
from src.data.data_acquisition import DataAcquisition
from src.data.candle_manager import CandleManager
from src.strategy.smc_engine import SMCEngine
from src.risk.risk_manager import RiskManager
from src.execution.executor import Executor
from src.execution.futures_adapter import FuturesAdapter
from src.execution.execution_engine import ExecutionEngine
# Production-Grade Position State Machine
from src.execution.position_state_machine import (
    ManagedPosition,
    PositionState,
    PositionRegistry,
    ExitReason,
    OrderEvent,
    OrderEventType,
    get_position_registry,
    reset_position_registry
)
from src.execution.position_manager_v2 import (
    PositionManagerV2,
    ManagementAction as ManagementActionV2,
    ActionType as ActionTypeV2
)
from src.execution.equity import calculate_effective_equity
from src.execution.execution_gateway import ExecutionGateway
from src.execution.position_persistence import PositionPersistence
from src.execution.production_safety import (
    SafetyConfig,
    ProtectionEnforcer,
    PositionProtectionMonitor,
)

from src.utils.kill_switch import KillSwitch, KillSwitchReason
from src.domain.models import Candle, Signal, SignalType, Position, Side
from src.storage.repository import save_candle, save_candles_bulk, get_active_position, save_account_state, sync_active_positions, record_event, record_metrics_snapshot, load_candles_map, get_candles, get_latest_candle_timestamp
from src.storage.maintenance import DatabasePruner
from src.live.startup_validator import ensure_all_coins_have_traces
from src.live.maintenance import periodic_data_maintenance
from src.reconciliation.reconciler import Reconciler

# Production Hardening Layer V2 (Issue #1-5 fixes + V2 hardening)
from src.safety.integration import (
    ProductionHardeningLayer,
    init_hardening_layer,
    HardeningDecision,
)

logger = get_logger(__name__)

def _exchange_position_side(pos_data: Dict[str, Any]) -> str:
    """
    Determine position side from exchange position dict.

    IMPORTANT: Our Kraken Futures client normalizes `size` to ALWAYS be positive and
    provides an explicit `side` field ("long" / "short"). Therefore we must prefer
    `side` over inferring from the sign of `size`.

    Falls back to signed-size inference for compatibility with any older/alternate
    exchange adapters that might still return signed sizes.
    """
    side_raw = (pos_data.get("side") or pos_data.get("positionSide") or pos_data.get("direction") or "")
    side = str(side_raw).lower().strip()
    if side in ("long", "short"):
        return side

    # Fallback: infer from signed size if side field is missing.
    try:
        size_val = Decimal(str(pos_data.get("size", 0)))
    except Exception:
        return "long"
    return "long" if size_val > 0 else "short"


class LiveTrading:
    """
    Live trading runtime.
    
    CRITICAL: Real capital at risk. Enforces all safety gates.
    """
    
    def __init__(self, config: Config):
        """Initialize live trading."""
        self.config = config

        # ========== POSITION STATE MACHINE V2 ==========
        # Feature flag for gradual rollout (prod live hard-requires via runtime guard)
        self.use_state_machine_v2 = os.getenv("USE_STATE_MACHINE_V2", "false").lower() == "true"
        
        # CRITICAL: Runtime assertion - detect test mocks in production
        import sys
        from unittest.mock import Mock, MagicMock
        
        # Check if we're in a test environment
        is_test = (
            "pytest" in sys.modules or
            "PYTEST_CURRENT_TEST" in os.environ or
            any("test" in path.lower() for path in sys.path if isinstance(path, str))
        )
        
        if not is_test:
            # Production mode - verify no mocks are being used
            # Informational: confirms runtime is not contaminated by test harness.
            logger.info(
                "PRODUCTION_MODE_VERIFICATION",
                pytest_in_modules="pytest" in sys.modules,
                pytest_env=os.getenv("PYTEST_CURRENT_TEST"),
                sys_path_test_dirs=[p for p in sys.path if isinstance(p, str) and "test" in p.lower()],
            )
        
        # Core Components
        cache_mins = getattr(config.exchange, "market_discovery_cache_minutes", 60)
        cache_mins = int(cache_mins) if isinstance(cache_mins, (int, float)) else 60
        self.client = KrakenClient(
            api_key=config.exchange.api_key,
            api_secret=config.exchange.api_secret,
            futures_api_key=config.exchange.futures_api_key,
            futures_api_secret=config.exchange.futures_api_secret,
            use_testnet=config.exchange.use_testnet,
            market_cache_minutes=cache_mins,
        )
        
        # CRITICAL: Verify client is not a mock
        if not is_test and (isinstance(self.client, Mock) or isinstance(self.client, MagicMock)):
            logger.critical("CRITICAL: KrakenClient is a Mock/MagicMock in production!")
            raise RuntimeError(
                "CRITICAL: KrakenClient is a Mock/MagicMock. "
                "This should never happen in production. Check for test code leaking into runtime."
            )
        
        self.data_acq = DataAcquisition(
            self.client,
            spot_symbols=config.exchange.spot_markets,
            futures_symbols=config.exchange.futures_markets
        )
        
        from src.storage.repository import record_event
        self.smc_engine = SMCEngine(config.strategy, event_recorder=record_event)
        self.risk_manager = RiskManager(config.risk, liquidity_filters=config.liquidity_filters, event_recorder=record_event)
        from src.execution.instrument_specs import InstrumentSpecRegistry
        self.instrument_spec_registry = InstrumentSpecRegistry(
            get_instruments_fn=self.client.get_futures_instruments,
            cache_ttl_seconds=getattr(config.exchange, "instrument_spec_cache_ttl_seconds", 12 * 3600),
            ccxt_exchange=self.client.futures_exchange if hasattr(self.client, 'futures_exchange') else None,
        )
        self.futures_adapter = FuturesAdapter(
            self.client,
            position_size_is_notional=config.exchange.position_size_is_notional,
            instrument_spec_registry=self.instrument_spec_registry,
        )
        
        # Store latest futures tickers for mapping (updated each tick)
        self.latest_futures_tickers: Optional[Dict[str, Decimal]] = None
        
        # ShockGuard: Wick/Flash move protection
        self.shock_guard = None
        if config.risk.shock_guard_enabled:
            from src.risk.shock_guard import ShockGuard
            self.shock_guard = ShockGuard(
                shock_move_pct=config.risk.shock_move_pct,
                shock_range_pct=config.risk.shock_range_pct,
                basis_shock_pct=config.risk.basis_shock_pct,
                shock_cooldown_minutes=config.risk.shock_cooldown_minutes,
                emergency_buffer_pct=config.risk.emergency_buffer_pct,
                trim_buffer_pct=config.risk.trim_buffer_pct,
                shock_marketwide_count=config.risk.shock_marketwide_count,
                shock_marketwide_window_sec=config.risk.shock_marketwide_window_sec,
            )
            logger.info("ShockGuard enabled")
        self.executor = Executor(config.execution, self.futures_adapter)
        self.execution_engine = ExecutionEngine(config)
        self.kill_switch = KillSwitch(self.client)
        self.market_discovery = MarketDiscoveryService(self.client, config)
        self._last_discovery_error_log_time: Optional[datetime] = None

        # Auction mode allocator (if enabled)
        self.auction_allocator = None
        self.auction_signals_this_tick = []  # Collect signals for auction mode
        
        # Signal cooldown: prevent the same signal from firing repeatedly for the same symbol.
        # Key: symbol, Value: (signal_type, structure_timestamp, cooldown_until)
        # A signal is considered "same" if it has the same symbol + signal_type + structure_timestamp.
        self._signal_cooldown: Dict[str, datetime] = {}  # symbol -> cooldown expiry
        self._signal_cooldown_hours: int = 4  # hours before same symbol can signal again
        
        # Auto halt recovery tracking (instance-level, not class-level)
        self._auto_recovery_attempts: list = []
        if config.risk.auction_mode_enabled:
            from src.portfolio.auction_allocator import (
                AuctionAllocator,
                PortfolioLimits,
            )
            limits = PortfolioLimits(
                max_positions=config.risk.auction_max_positions,
                max_margin_util=config.risk.auction_max_margin_util,
                max_per_cluster=config.risk.auction_max_per_cluster,
                max_per_symbol=config.risk.auction_max_per_symbol,
            )
            self.auction_allocator = AuctionAllocator(
                limits=limits,
                swap_threshold=config.risk.auction_swap_threshold,
                min_hold_minutes=config.risk.auction_min_hold_minutes,
                max_trades_per_cycle=config.risk.auction_max_trades_per_cycle,
                max_new_opens_per_cycle=config.risk.auction_max_new_opens_per_cycle,
                max_closes_per_cycle=config.risk.auction_max_closes_per_cycle,
                entry_cost=config.risk.auction_entry_cost,
                exit_cost=config.risk.auction_exit_cost,
            )
            logger.info("Auction mode enabled", max_positions=limits.max_positions)
        
        if self.use_state_machine_v2:
            logger.critical("🚀 POSITION STATE MACHINE V2 ENABLED")
            
            # Initialize the Position Registry (singleton)
            self.position_registry = get_position_registry()
            
            # Initialize Persistence (SQLite)
            self.position_persistence = PositionPersistence("data/positions.db")
            
            # Initialize Position Manager V2
            self.position_manager_v2 = PositionManagerV2(registry=self.position_registry)
            
            # Initialize Execution Gateway - ALL orders flow through here
            self.execution_gateway = ExecutionGateway(
                exchange_client=self.client,
                registry=self.position_registry,
                position_manager=self.position_manager_v2,
                persistence=self.position_persistence,
            )
            
            logger.critical("State Machine V2 running - all orders via gateway")
            self._protection_monitor = None
            self._protection_task = None
            self._order_poll_task = None
        
        self.active = False
        
        # Candle data managed by dedicated service
        from src.data.ohlcv_fetcher import OHLCVFetcher
        _ohlcv_fetcher = OHLCVFetcher(self.client, config)
        self.candle_manager = CandleManager(
            self.client,
            spot_to_futures=self.futures_adapter.map_spot_to_futures,
            use_futures_fallback=getattr(config.exchange, "use_futures_ohlcv_fallback", True),
            ohlcv_fetcher=_ohlcv_fetcher,
        )
        
        self.last_trace_log: Dict[str, datetime] = {} # Dashboard update throttling
        self.last_account_sync = datetime.min.replace(tzinfo=timezone.utc)
        self.last_maintenance_run = datetime.min.replace(tzinfo=timezone.utc)
        self.last_data_maintenance = datetime.min.replace(tzinfo=timezone.utc)
        self.last_recon_time = datetime.min.replace(tzinfo=timezone.utc)
        self.last_metrics_emit = datetime.min.replace(tzinfo=timezone.utc)
        self.ticks_since_emit = 0
        self.signals_since_emit = 0
        self.last_fetch_latency_ms: Optional[int] = None
        self.db_pruner = DatabasePruner()
        
        # TP Backfill cooldown tracking (symbol -> last_backfill_time)
        self.tp_backfill_cooldowns: Dict[str, datetime] = {}
        
        # Coin processing tracking
        self.coin_processing_stats: Dict[str, Dict] = {}  # Track processing stats per coin
        self.last_status_summary = datetime.min.replace(tzinfo=timezone.utc)
        
        # Market Expansion (Coin Universe)
        # V3: Use get_all_candidates() - config tiers are for universe selection only
        self.markets = config.exchange.spot_markets
        if config.assets.mode == "whitelist":
             self.markets = config.assets.whitelist
        elif config.coin_universe and config.coin_universe.enabled:
             # Get all candidate symbols (flattened from tiers or direct list)
             expanded = config.coin_universe.get_all_candidates()
             # Deduplicate and exclude disallowed bases (fiat + stablecoin).
             self.markets = [s for s in list(set(expanded)) if not has_disallowed_base(s)]
             logger.info("Coin Universe Enabled (V3 - dynamic tier classification)", 
                        market_count=len(self.markets))
             
        # Update Data Acquisition with full list
        self.data_acq = DataAcquisition(
            self.client,
            spot_symbols=self.markets,
            futures_symbols=config.exchange.futures_markets
        )
        
        # ===== PRODUCTION HARDENING LAYER =====
        # Integrates: InvariantMonitor, CycleGuard, PositionDeltaReconciler, DecisionAuditLogger
        # This provides hard safety limits, timing protection, and decision-complete logging
        try:
            self.hardening = init_hardening_layer(
                config=config,
                kill_switch=self.kill_switch,
            )
            logger.info(
                "ProductionHardeningLayer initialized",
                trading_allowed=self.hardening.is_trading_allowed(),
            )
        except Exception as e:
            logger.warning("Failed to initialize ProductionHardeningLayer", error=str(e))
            self.hardening = None
        
        logger.info("Live Trading initialized", 
                   markets=config.exchange.futures_markets,
                   state_machine_v2=self.use_state_machine_v2,
                   hardening_enabled=self.hardening is not None)

    def _market_symbols(self) -> List[str]:
        """Return list of spot symbols. Handles both list (initial) and dict (after discovery). Excludes blocklist."""
        blocklist = set(
            s.strip().upper() for s in getattr(self.config.exchange, "spot_ohlcv_blocklist", []) or []
        )
        # Also honor assets.blacklist (exists in config model but was not enforced here previously).
        blocklist |= set(s.strip().upper() for s in getattr(self.config.assets, "blacklist", []) or [])
        # Also honor execution entry blocklist for universe filtering (analysis + new entries).
        blocklist |= set(
            s.strip().upper().split(":")[0] for s in getattr(self.config.execution, "entry_blocklist_spot_symbols", []) or []
        )
        blocked_bases = set(
            b.strip().upper() for b in getattr(self.config.execution, "entry_blocklist_bases", []) or []
        )
        if isinstance(self.markets, dict):
            raw = list(self.markets.keys())
        else:
            raw = list(self.markets)
        if not blocklist:
            if not blocked_bases:
                return raw
        out: List[str] = []
        for s in raw:
            key = (s.strip().upper().split(":")[0] if s else "")
            if not key:
                continue
            if key in blocklist:
                continue
            # Global exclusion: never include fiat/stablecoin-base instruments in the trading universe.
            if has_disallowed_base(key):
                continue
            if blocked_bases:
                base = key.split("/")[0].strip() if "/" in key else key
                if base in blocked_bases:
                    continue
            out.append(s)
        return out

    def _get_static_tier(self, symbol: str) -> Optional[str]:
        """
        DEPRECATED: Debug-only legacy tier lookup.
        
        This method looks up the symbol in config coin_universe.liquidity_tiers,
        which are now CANDIDATE GROUPS, not tier assignments.
        
        For authoritative tier classification, use:
            self.market_discovery.get_symbol_tier(symbol)
        
        This is kept only for transitional logging and debugging.
        Returns "A", "B", "C", or None if not in any config group.
        """
        if not getattr(self.config, "coin_universe", None) or not getattr(self.config.coin_universe, "enabled", False):
            return None
        tiers = getattr(self.config.coin_universe, "liquidity_tiers", None) or {}
        for tier in ("A", "B", "C"):
            if symbol in tiers.get(tier, []):
                return tier
        return None
    
    async def _update_market_universe(self):
        """Discover and update trading universe."""
        if not self.config.exchange.use_market_discovery:
            return
            
        try:
            logger.info("Executing periodic market discovery...")
            mapping = await self.market_discovery.discover_markets()

            if not mapping:
                cooldown_min = getattr(
                    self.config.exchange, "market_discovery_failure_log_cooldown_minutes", 60
                )
                now = datetime.now(timezone.utc)
                should_log = (
                    self._last_discovery_error_log_time is None
                    or (now - self._last_discovery_error_log_time).total_seconds()
                    >= cooldown_min * 60
                )
                if should_log:
                    logger.critical(
                        "Market discovery empty; using existing universe; "
                        "check spot/futures market fetch (get_spot_markets/get_futures_markets)."
                    )
                    self._last_discovery_error_log_time = now
                return
            
            # Shrink protection: if new universe is <50% of LAST DISCOVERED universe,
            # something is wrong (API issue, temporary outage). Keep old universe.
            # Only applies after first successful discovery (initial config list is much larger).
            last_discovered_count = getattr(self, "_last_discovered_count", 0)
            new_count = len(mapping)
            if last_discovered_count > 10 and new_count < last_discovered_count * 0.5:
                logger.critical(
                    "UNIVERSE_SHRINK_REJECTED: new universe is <50% of last discovery — likely API issue, keeping old universe",
                    last_discovered=last_discovered_count,
                    new_count=new_count,
                    dropped_pct=f"{(1 - new_count / last_discovered_count) * 100:.0f}%",
                )
                try:
                    from src.monitoring.alerting import send_alert
                    await send_alert(
                        "UNIVERSE_SHRINK",
                        f"Discovery returned {new_count} coins vs {last_discovered_count} last discovery — rejected",
                        urgent=True,
                    )
                except Exception:
                    pass
                return

            # Track last successful discovery count for future shrink checks
            self._last_discovered_count = new_count

            # Log added/removed symbols vs current universe
            prev_symbols = set(self._market_symbols())
            supported = set(mapping.keys())
            dropped = prev_symbols - supported
            added = supported - prev_symbols
            for sym in sorted(dropped):
                logger.warning("SYMBOL_REMOVED", symbol=sym)
            for sym in sorted(added):
                logger.info("SYMBOL_ADDED", symbol=sym)
            
            # Update internal state (Maintain Spot -> Futures mapping)
            self.markets = mapping
            self.futures_adapter.set_spot_to_futures_override(mapping)

            # Update Data Acquisition
            new_spot_symbols = list(mapping.keys())
            new_futures_symbols = list(mapping.values())
            self.data_acq.update_symbols(new_spot_symbols, new_futures_symbols)
            
            # Initialize logic is handled lazily by CandleManager/PositionManager as needed
            # We just ensure DataAcquisition is updated (done above)
            pass
            
            logger.info("Market universe updated", count=len(self.markets))
            
        except Exception as e:
            logger.error("Failed to update market universe", error=str(e))

    async def run(self):
        """
        Main trading loop.
        """
        import os
        import time
        from src.storage.repository import record_event
        
        # 0. Record Startup Event
        try:
            record_event("SYSTEM_STARTUP", "system", {
                "version": self.config.system.version,
                "pid": os.getpid(),
                "mode": "LiveTradingEngine"
            })
        except Exception as e:
            logger.error("Failed to record startup event", error=str(e))
        
        # Smoke Mode / Local Dev Limits
        max_loops = int(os.getenv("MAX_LOOPS", "-1"))
        run_seconds = int(os.getenv("RUN_SECONDS", "-1"))
        start_time = time.time()
        loop_count = 0
        is_smoke_mode = max_loops > 0 or run_seconds > 0
        
        logger.info("Starting run loop", 
                   max_loops=max_loops if max_loops > 0 else "unlimited",
                   run_seconds=run_seconds if run_seconds > 0 else "unlimited",
                   dry_run=self.config.system.dry_run,
                   smoke_mode=is_smoke_mode)

        self.active = True
        self._reconcile_requested = False
        self.trade_paused = False
        # Important but not an error condition.
        logger.warning("🚀 STARTING LIVE TRADING")
        
        # ===== PRODUCTION HARDENING SELF-TEST (V2) =====
        # Must pass before trading can start
        if self.hardening:
            success, errors = self.hardening.self_test()
            if not success:
                logger.critical(
                    "HARDENING_SELF_TEST_FAILED",
                    errors=errors,
                    action="REFUSING_TO_START",
                )
                raise RuntimeError(f"Production hardening self-test failed: {errors}")
            logger.info("Hardening self-test passed", run_id=self.hardening._run_id)
        
        try:
            # 1. Initialize Client
            logger.info("Initializing Kraken client...")
            await self.client.initialize()
            
            # 1.5 Initial Market Discovery
            if self.config.exchange.use_market_discovery:
                logger.info("Performing initial market discovery...")
                await self._update_market_universe()
                self.last_discovery_time = datetime.now(timezone.utc)
            else:
                self.last_discovery_time = datetime.min.replace(tzinfo=timezone.utc)

            # Startup banner: config flags and universe size (same log sink)
            _recon_cfg = getattr(self.config, "reconciliation", None)
            _auction = getattr(self.config.risk, "auction_mode_enabled", False)
            _recon_en = getattr(_recon_cfg, "reconcile_enabled", True) if _recon_cfg else True
            _shock = getattr(self.config.risk, "shock_guard_enabled", False)
            logger.info(
                "STARTUP_BANNER",
                auction_enabled=_auction,
                reconcile_enabled=_recon_en,
                shock_guard_enabled=bool(self.shock_guard or _shock),
                universe_size=len(self._market_symbols()),
            )

            # 1.6 Startup: ensure all monitored coins have DECISION_TRACE (dashboard coverage)
            try:
                await ensure_all_coins_have_traces(self._market_symbols())
            except Exception as e:
                logger.error("Startup trace validation failed", error=str(e))

            # 2. Sync State (skip in dry run if no keys)
            if self.config.system.dry_run and not self.client.has_valid_futures_credentials():
                 logger.warning("Dry Run Mode: No Futures credentials found. Skipping account sync.")
            else:
                # Sync Account
                try:
                    await self._sync_account_state()
                    await self._sync_positions()
                    await self.executor.sync_open_orders()
                except Exception as e:
                    logger.error("Initial sync failed", error=str(e))
                    if not self.config.system.dry_run:
                        raise
            
            # 2.5 Position State Machine V2 Startup Recovery
            if self.use_state_machine_v2 and self.execution_gateway:
                try:
                    logger.info("Starting Position State Machine V2 recovery...")
                    await self.execution_gateway.startup()
                    logger.info("Position State Machine V2 recovery complete",
                               active_positions=len(self.position_registry.get_all_active()) if self.position_registry else 0)
                except Exception as e:
                    logger.error("Position State Machine V2 startup failed", error=str(e))

            # 2.6 Startup takeover & protect (V2 authoritative pass)
            # In V2 mode, do not use the legacy Reconciler (DB-only) as the source of truth.
            if (
                self.use_state_machine_v2
                and self.execution_gateway
                and not (self.config.system.dry_run and not self.client.has_valid_futures_credentials())
            ):
                _recon_cfg = getattr(self.config, "reconciliation", None)
                if _recon_cfg and getattr(_recon_cfg, "reconcile_enabled", True):
                    try:
                        from src.execution.production_takeover import ProductionTakeover, TakeoverConfig
                        takeover = ProductionTakeover(
                            self.execution_gateway,
                            TakeoverConfig(
                                takeover_stop_pct=Decimal(str(os.getenv("TAKEOVER_STOP_PCT", "0.02"))),
                                stop_replace_atomically=True,
                                dry_run=bool(self.config.system.dry_run),
                            ),
                        )
                        logger.critical("Running startup takeover (V2)...")
                        stats = await takeover.execute_takeover()
                        logger.critical("Startup takeover complete", **stats)
                        self.last_recon_time = datetime.now(timezone.utc)
                    except Exception as ex:
                        logger.critical("Startup takeover failed", error=str(ex), exc_info=True)
                        if not self.config.system.dry_run:
                            raise

            # 2.6 PositionProtectionMonitor (Invariant K) - periodic check when V2 live
            if (
                self.use_state_machine_v2
                and self.execution_gateway
                and self.position_registry
            ):
                try:
                    cfg = SafetyConfig()
                    enforcer = ProtectionEnforcer(self.client, cfg)
                    self._protection_monitor = PositionProtectionMonitor(
                        self.client, self.position_registry, enforcer
                    )
                    self._protection_task = asyncio.create_task(
                        self._run_protection_checks(interval_seconds=30)
                    )
                    logger.info("PositionProtectionMonitor started (interval=30s)")
                except Exception as e:
                    logger.error("Failed to start PositionProtectionMonitor", error=str(e))

            # 2.6b Order-status polling: detect entry fills, trigger PLACE_STOP (SL/TP)
            if self.use_state_machine_v2 and self.execution_gateway:
                try:
                    self._order_poll_task = asyncio.create_task(
                        self._run_order_polling(interval_seconds=12)
                    )
                    logger.info("Order-status polling started (interval=12s)")
                except Exception as e:
                    logger.error("Failed to start order poller", error=str(e))

            # 2.6c Daily P&L summary (runs once per day at midnight UTC)
            try:
                self._daily_summary_task = asyncio.create_task(
                    self._run_daily_summary()
                )
                logger.info("Daily summary task started")
            except Exception as e:
                logger.error("Failed to start daily summary task", error=str(e))

            # 2.6d Telegram command handler (/status, /positions, /help)
            try:
                from src.monitoring.telegram_bot import TelegramCommandHandler
                self._telegram_handler = TelegramCommandHandler(
                    data_provider=self._get_system_status
                )
                self._telegram_cmd_task = asyncio.create_task(
                    self._telegram_handler.run()
                )
                logger.info("Telegram command handler started")
            except Exception as e:
                logger.error("Failed to start Telegram command handler", error=str(e))

            # 3. Fast Startup - Load candles
            logger.info("Loading candles from database...")
            try:
                # 3. Fast Startup - Load candles via Manager
                await self.candle_manager.initialize(self._market_symbols())
            except Exception as e:
                logger.error("Failed to hydrate candles", error=str(e))

            # 4. Start Data Acquisition
            await self.data_acq.start()
            
            # 4.5. Run first tick to hydrate runtime state
            if not (self.config.system.dry_run and not self.client.has_valid_futures_credentials()):
                try:
                    await self._tick()
                    logger.info("Initial tick completed - runtime state hydrated")
                except Exception as e:
                    logger.error("Initial tick failed", error=str(e))
            
            # 4.6. Validate position protection (startup safety gate)
            try:
                await self._validate_position_protection()
            except Exception as e:
                logger.error("Position protection validation failed", error=str(e))
            
            # 5. Main Loop
            while self.active:
            # Check Smoke Mode Limits
                if max_loops > 0 and loop_count >= max_loops:
                    logger.info("Smoke mode: Max loops reached", max_loops=max_loops, loops_completed=loop_count)
                    break
                    
                if run_seconds > 0 and (time.time() - start_time) >= run_seconds:
                    elapsed = time.time() - start_time
                    logger.info("Smoke mode: Run time limit reached", run_seconds=run_seconds, elapsed_seconds=f"{elapsed:.1f}")
                    break
                
                loop_count += 1
                self._last_cycle_count = loop_count

                if self.kill_switch.is_active():
                    # Attempt auto-recovery for margin_critical (the most common false-positive halt)
                    recovered = False
                    if self.kill_switch.reason == KillSwitchReason.MARGIN_CRITICAL:
                        recovered = await self._try_auto_recovery()
                    
                    if not recovered:
                        logger.critical("Kill switch active - pausing loop",
                                       reason=self.kill_switch.reason.value if self.kill_switch.reason else "unknown")
                        await asyncio.sleep(60)
                        continue
                
                # Periodic Market Discovery
                if self.config.exchange.use_market_discovery:
                    now = datetime.now(timezone.utc)
                    elapsed_discovery = (now - self.last_discovery_time).total_seconds()
                    refresh_sec = self.config.exchange.discovery_refresh_hours * 3600
                    
                    if elapsed_discovery >= refresh_sec:
                        await self._update_market_universe()
                        self.last_discovery_time = now
                
                loop_start = datetime.now(timezone.utc)
                
                try:
                    await self._tick()
                except Exception as e:
                    logger.error("Error in live trading tick", error=str(e))
                    if "API" in str(e):
                         # Potential API failure - check if we should trigger kill switch
                         pass
                
                self.ticks_since_emit += 1
                now = datetime.now(timezone.utc)
                if (now - self.last_metrics_emit).total_seconds() >= 60.0:
                    try:
                        record_metrics_snapshot({
                            "last_tick_at": now.isoformat(),
                            "ticks_last_min": self.ticks_since_emit,
                            "signals_last_min": self.signals_since_emit,
                            "markets_count": len(self._market_symbols()),
                            "api_fetch_latency_ms": getattr(self, "last_fetch_latency_ms", None),
                            "coins_futures_fallback_used": self.candle_manager.get_futures_fallback_count(),
                        })
                        self.last_metrics_emit = now
                        self.ticks_since_emit = 0
                        self.signals_since_emit = 0
                    except Exception as ex:
                        logger.warning("Failed to emit metrics snapshot", error=str(ex))

                # Periodic reconciliation (positions: system vs exchange)
                _recon_cfg = getattr(self.config, "reconciliation", None)
                if _recon_cfg and getattr(_recon_cfg, "reconcile_enabled", True):
                    recon_interval = getattr(_recon_cfg, "periodic_interval_seconds", 120)
                    run_after_orders = getattr(self, "_reconcile_requested", False)
                    if run_after_orders or (now - self.last_recon_time).total_seconds() >= recon_interval:
                        try:
                            if self.use_state_machine_v2 and self.execution_gateway:
                                # V2: reconcile against registry/exchange (no DB-only authority)
                                res = await self.execution_gateway.sync_with_exchange()
                                logger.info("V2 sync_with_exchange complete", **res)
                            else:
                                recon = self._build_reconciler()
                                await recon.reconcile_all()
                            self.last_recon_time = now
                            if run_after_orders:
                                self._reconcile_requested = False
                        except Exception as ex:
                            logger.warning("Reconciliation failed", error=str(ex))

                # ===== CYCLE SUMMARY (single log line per tick with key metrics) =====
                now = datetime.now(timezone.utc)
                cycle_elapsed = (now - loop_start).total_seconds()
                try:
                    positions_count = 0
                    if self.use_state_machine_v2 and self.execution_gateway:
                        positions_count = len(self.execution_gateway.registry.get_all_active())
                    elif self.position_manager_v2:
                        positions_count = len(self.position_manager_v2.get_all_positions())
                    
                    kill_active = self.kill_switch.is_active() if self.kill_switch else False
                    system_state = "NORMAL"
                    if kill_active:
                        system_state = "KILL_SWITCH"
                    elif self.hardening and hasattr(self.hardening, 'invariant_monitor'):
                        inv_state = self.hardening.invariant_monitor.state.value
                        if inv_state != "active":
                            system_state = inv_state.upper()
                    
                    logger.info(
                        "CYCLE_SUMMARY",
                        cycle=loop_count,
                        duration_ms=int(cycle_elapsed * 1000),
                        positions=positions_count,
                        universe=len(self._market_symbols()),
                        system_state=system_state,
                        cooldowns_active=len(self._signal_cooldown),
                    )
                except Exception as summary_err:
                    logger.warning("CYCLE_SUMMARY_FAILED", error=str(summary_err), error_type=type(summary_err).__name__)

                # Dynamic sleep to align with 1m intervals
                elapsed = cycle_elapsed
                sleep_time = max(5.0, 60.0 - elapsed)
                await asyncio.sleep(sleep_time)
            
            # Smoke mode summary
            if is_smoke_mode:
                total_runtime = time.time() - start_time
                logger.info(
                    "✅ SMOKE TEST COMPLETED SUCCESSFULLY",
                    loops_completed=loop_count,
                    runtime_seconds=f"{total_runtime:.1f}",
                    markets_tracked=len(self.markets),
                    dry_run=self.config.system.dry_run
                )
                
        except asyncio.CancelledError:
            logger.info("Live trading loop cancelled")
        except Exception as e:
            # Log the exception and re-raise to ensure non-zero exit code
            logger.critical("Live trading failed with exception", error=str(e), exc_info=True)
            raise
        finally:
            self.active = False
            if getattr(self, "_protection_monitor", None):
                self._protection_monitor.stop()
            if getattr(self, "_protection_task", None) and not self._protection_task.done():
                self._protection_task.cancel()
                try:
                    await self._protection_task
                except asyncio.CancelledError:
                    pass
            if getattr(self, "_order_poll_task", None) and not self._order_poll_task.done():
                self._order_poll_task.cancel()
                try:
                    await self._order_poll_task
                except asyncio.CancelledError:
                    pass
            if getattr(self, "_daily_summary_task", None) and not self._daily_summary_task.done():
                self._daily_summary_task.cancel()
                try:
                    await self._daily_summary_task
                except asyncio.CancelledError:
                    pass
            if getattr(self, "_telegram_cmd_task", None) and not self._telegram_cmd_task.done():
                if getattr(self, "_telegram_handler", None):
                    self._telegram_handler.stop()
                self._telegram_cmd_task.cancel()
                try:
                    await self._telegram_cmd_task
                except asyncio.CancelledError:
                    pass
            await self.data_acq.stop()
            await self.client.close()
            logger.info("Live trading shutdown complete")

    async def _run_order_polling(self, interval_seconds: int = 12) -> None:
        """Poll pending entry order status, process fills, trigger PLACE_STOP (SL/TP)."""
        while self.active:
            await asyncio.sleep(interval_seconds)
            if not self.active:
                break
            if not self.execution_gateway:
                continue
            try:
                n = await self.execution_gateway.poll_and_process_order_updates()
                if n > 0:
                    logger.info("Order poll processed updates", count=n)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Order poll failed", error=str(e))

    async def _run_protection_checks(self, interval_seconds: int = 30) -> None:
        """
        V2 protection monitor loop with escalation policy.

        If a naked position is detected in prod live, fail closed by activating the kill switch
        (emergency flatten).
        
        Startup grace: The first check is delayed by 3x the normal interval (90s by default)
        to give the main tick loop time to place missing stops after a restart or kill switch
        recovery. Without this, the monitor fires before stops can be placed, causing an
        immediate emergency kill switch on startup.
        """
        # Startup grace period: wait longer on first check so the tick loop can
        # place missing stops before we start enforcing Invariant K.
        startup_grace_seconds = interval_seconds * 3
        logger.info(
            "Protection monitor: startup grace period",
            grace_seconds=startup_grace_seconds,
            enforce_after="first check",
        )
        await asyncio.sleep(startup_grace_seconds)
        
        consecutive_naked_count: Dict[str, int] = {}  # symbol -> consecutive naked detections
        ESCALATION_THRESHOLD = 2  # Require 2 consecutive naked detections before emergency kill
        
        while self.active:
            if not self.active:
                break
            if not getattr(self, "_protection_monitor", None):
                await asyncio.sleep(interval_seconds)
                continue
            try:
                results = await self._protection_monitor.check_all_positions()
                naked = [s for s, ok in results.items() if not ok]
                if naked:
                    # Update consecutive counts
                    for s in naked:
                        consecutive_naked_count[s] = consecutive_naked_count.get(s, 0) + 1
                    
                    # Check if any symbol has been naked for enough consecutive checks
                    persistent_naked = [s for s in naked if consecutive_naked_count.get(s, 0) >= ESCALATION_THRESHOLD]
                    
                    if persistent_naked:
                        logger.critical(
                            "NAKED_POSITIONS_DETECTED (persistent)",
                            naked_symbols=persistent_naked,
                            details=results,
                            consecutive_counts={s: consecutive_naked_count[s] for s in persistent_naked},
                        )
                        is_prod_live = (os.getenv("ENVIRONMENT", "").strip().lower() == "prod") and (not self.config.system.dry_run)
                        if is_prod_live:
                            await self.kill_switch.activate(KillSwitchReason.RECONCILIATION_FAILURE, emergency=True)
                            return
                    else:
                        logger.warning(
                            "NAKED_POSITIONS_DETECTED (first occurrence, giving time to self-heal)",
                            naked_symbols=naked,
                            details=results,
                            consecutive_counts={s: consecutive_naked_count.get(s, 0) for s in naked},
                        )
                else:
                    # All positions protected — reset all counters
                    consecutive_naked_count.clear()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Protection check loop failed", error=str(e), error_type=type(e).__name__)
            
            await asyncio.sleep(interval_seconds)

    async def _get_system_status(self) -> dict:
        """
        Data provider for Telegram command handler.
        Returns current system state for /status and /positions commands.
        """
        from src.execution.equity import calculate_effective_equity
        
        result: dict = {
            "equity": Decimal("0"),
            "margin_used": Decimal("0"),
            "margin_pct": 0.0,
            "positions": [],
            "system_state": "UNKNOWN",
            "kill_switch_active": False,
            "cycle_count": getattr(self, "_last_cycle_count", 0),
            "cooldowns_active": len(self._signal_cooldown),
            "universe_size": len(self._market_symbols()),
        }
        
        try:
            balance = await self.client.get_futures_balance()
            base = getattr(self.config.exchange, "base_currency", "USD")
            equity, available_margin, margin_used = await calculate_effective_equity(
                balance, base_currency=base, kraken_client=self.client
            )
            result["equity"] = equity
            result["margin_used"] = margin_used
            result["margin_pct"] = float((margin_used / equity) * 100) if equity > 0 else 0
        except Exception as e:
            logger.warning("Status: failed to get equity", error=str(e))
        
        try:
            positions = await self.client.get_all_futures_positions()
            result["positions"] = [p for p in positions if p.get("size", 0) != 0]
        except Exception as e:
            logger.warning("Status: failed to get positions", error=str(e))
        
        # System state
        kill_active = self.kill_switch.is_active() if self.kill_switch else False
        result["kill_switch_active"] = kill_active
        if kill_active:
            result["system_state"] = "KILL_SWITCH"
        elif self.hardening and hasattr(self.hardening, 'invariant_monitor'):
            inv_state = self.hardening.invariant_monitor.state.value
            result["system_state"] = inv_state.upper() if inv_state != "active" else "NORMAL"
        else:
            result["system_state"] = "NORMAL"
        
        return result

    async def _run_daily_summary(self) -> None:
        """
        Send a daily P&L summary via Telegram at midnight UTC.
        
        Calculates: equity, daily P&L, open positions, trades today, win rate.
        Runs in a background loop, sleeping until the next midnight.
        """
        from src.monitoring.alerting import send_alert
        
        while self.active:
            try:
                # Calculate seconds until next midnight UTC
                now = datetime.now(timezone.utc)
                tomorrow = (now + timedelta(days=1)).replace(
                    hour=0, minute=0, second=5, microsecond=0
                )
                sleep_seconds = (tomorrow - now).total_seconds()
                await asyncio.sleep(sleep_seconds)
                
                if not self.active:
                    break
                
                # Gather data
                try:
                    account_info = await self.client.get_futures_account_info()
                    equity = Decimal(str(account_info.get("equity", 0)))
                    margin_used = Decimal(str(account_info.get("marginUsed", 0)))
                    margin_pct = float((margin_used / equity) * 100) if equity > 0 else 0
                    
                    # Get open positions
                    positions = await self.client.get_all_futures_positions()
                    open_positions = [p for p in positions if p.get("size", 0) != 0]
                    
                    # Get today's trades from DB
                    today_trades = []
                    try:
                        from src.storage.repository import get_trades_since
                        since = now - timedelta(hours=24)
                        all_trades = await asyncio.to_thread(get_trades_since, since)
                        today_trades = all_trades if all_trades else []
                    except Exception:
                        pass
                    
                    wins = sum(1 for t in today_trades if getattr(t, 'net_pnl', 0) > 0)
                    losses = sum(1 for t in today_trades if getattr(t, 'net_pnl', 0) <= 0)
                    total_pnl = sum(getattr(t, 'net_pnl', Decimal("0")) for t in today_trades)
                    win_rate = f"{(wins / (wins + losses) * 100):.0f}%" if (wins + losses) > 0 else "N/A"
                    
                    pnl_sign = "+" if total_pnl >= 0 else ""
                    pnl_emoji = "📈" if total_pnl >= 0 else "📉"
                    
                    # Build position list
                    pos_lines = []
                    for p in open_positions[:10]:  # Max 10
                        sym = p.get('symbol', '?')
                        side = p.get('side', '?')
                        upnl = Decimal(str(p.get('unrealizedPnl', p.get('unrealized_pnl', 0))))
                        upnl_sign = "+" if upnl >= 0 else ""
                        pos_lines.append(f"  • {sym} ({side}) {upnl_sign}${upnl:.2f}")
                    
                    positions_str = "\n".join(pos_lines) if pos_lines else "  None"
                    
                    summary = (
                        f"{pnl_emoji} Daily Summary ({now.strftime('%Y-%m-%d')})\n"
                        f"\n"
                        f"Equity: ${equity:.2f}\n"
                        f"Margin used: {margin_pct:.1f}%\n"
                        f"\n"
                        f"Trades today: {len(today_trades)}\n"
                        f"Win/Loss: {wins}W / {losses}L ({win_rate})\n"
                        f"Day P&L: {pnl_sign}${total_pnl:.2f}\n"
                        f"\n"
                        f"Open positions ({len(open_positions)}):\n"
                        f"{positions_str}"
                    )
                    
                    await send_alert("DAILY_SUMMARY", summary, urgent=True)
                    logger.info("Daily summary sent", equity=str(equity), trades=len(today_trades))
                    
                    # Reset daily loss tracking for new day
                    self.risk_manager.reset_daily_metrics(equity)
                    
                except Exception as e:
                    logger.warning("Failed to gather daily summary data", error=str(e))
                    
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Daily summary loop error", error=str(e))
                await asyncio.sleep(3600)  # Retry in 1 hour

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

    async def _sync_positions(self, raw_positions: Optional[List[Dict]] = None) -> List[Dict]:
        """
        Sync active positions from exchange and update RiskManager.
        
        Args:
            raw_positions: Optional pre-fetched positions list (to avoid duplicate API calls)
        
        Returns:
            List of active positions (dicts)
        """
        if raw_positions is None:
            try:
                # Add timeout to prevent hanging the main loop
                raw_positions = await asyncio.wait_for(self.client.get_all_futures_positions(), timeout=30.0)
            except asyncio.TimeoutError:
                logger.error("Timeout fetching futures positions during sync")
                raw_positions = []
            except Exception as e:
                logger.error("Failed to fetch futures positions", error=str(e))
                raw_positions = []
        
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
        
        # Persist to DB for Dashboard (Phase 2: Use async wrapper)
        try:
             await asyncio.to_thread(sync_active_positions, self.risk_manager.current_positions)
        except Exception as e:
             logger.error("Failed to sync positions to DB", error=str(e))
        
        # ALWAYS log position count for debugging
        logger.info(
            f"Active Portfolio: {len(active_positions)} positions", 
            symbols=[p.symbol for p in active_positions]
        )
        
        return raw_positions

    def _build_reconciler(self) -> "Reconciler":
        """Build Reconciler with config, place_futures_order, and optional place_protection callback."""
        place_futures = lambda symbol, side, order_type, size, reduce_only: self.client.place_futures_order(
            symbol=symbol, side=side, order_type=order_type, size=size, reduce_only=reduce_only
        )
        place_protection = None  # Adopted positions get protection on next tick via _reconcile_protective_orders
        return Reconciler(
            self.client,
            self.config,
            place_futures_order_fn=place_futures,
            place_protection_callback=place_protection,
        )

    async def _validate_position_protection(self):
        """
        Validate all positions have protection (startup safety gate).
        
        Checks V2 position registry for unprotected positions.
        Emits alerts and optionally pauses trading.
        """
        from src.storage.repository import async_record_event
        
        unprotected = []

        tracked_symbols: set[str] = set()

        # V2: Check registry state (authoritative)
        for p in self.position_registry.get_all_active():
            tracked_symbols.add(p.symbol)
            
            # Skip positions with zero remaining quantity - nothing to protect
            if p.remaining_qty <= 0:
                continue
            
            # Minimal protection invariants (full takeover invariants are handled elsewhere)
            has_stop_price = p.current_stop_price is not None
            has_stop_order = p.stop_order_id is not None
            is_protected = bool(has_stop_price and has_stop_order)
            if not is_protected:
                unprotected.append({
                    "symbol": p.symbol,
                    "source": "registry_v2",
                    "reason": "MISSING_STOP",
                    "has_sl_price": has_stop_price,
                    "has_sl_order": has_stop_order,
                    "is_protected": is_protected,
                    "remaining_qty": str(p.remaining_qty),
                })
        
        if unprotected:
            # This is actionable (positions lack protection) but is not a crash.
            logger.error(
                "UNPROTECTED positions detected",
                count=len(unprotected),
                positions=unprotected
            )
            # Emit alert events
            for up in unprotected:
                await async_record_event(
                    "UNPROTECTED_POSITION",
                    up['symbol'],
                    up
                )
            # Optional: Pause new opens (uncomment if desired)
            # self.trading_paused = True
        else:
            total_tracked = len(tracked_symbols) + (len(db_positions) if db_positions else 0)
            logger.info("All positions are protected", total_positions=total_tracked)

    async def _tick(self):
        """
        Single iteration of live trading logic.
        Optimized for batch processing (Phase 10).
        """
        # 0. Kill Switch Check (HIGHEST PRIORITY)
        ks = self.kill_switch
        
        if ks.is_active():
            logger.critical("Kill switch is active - halting trading")

            # Cancel all pending orders
            try:
                logger.warning("Cancelling all pending orders...")
                cancelled = await self.client.cancel_all_orders()
                logger.info(f"Kill switch: Cancelled {len(cancelled)} orders")
            except Exception as e:
                logger.error("Failed to cancel orders during kill switch", error=str(e))

            # Close all positions
            try:
                logger.critical("Closing all positions due to kill switch")
                positions = await self.client.get_all_futures_positions()
                for pos in positions:
                    if pos.get('size', 0) != 0:  # Only close non-zero positions
                        symbol = pos.get('symbol')
                        try:
                            await self.client.close_position(symbol)
                            logger.warning(f"Kill switch: Closed position for {symbol}")
                        except Exception as e:
                            logger.error(f"Kill switch: Failed to close {symbol}", error=str(e))

                if not positions or all(pos.get('size', 0) == 0 for pos in positions):
                    logger.info("Kill switch: No open positions to close")
            except Exception as e:
                logger.error("Failed to close positions during kill switch", error=str(e))

            # Stop processing
            return
        
        # 0.1 Order Timeout Monitoring (CRITICAL: Check first)
        try:
            cancelled_count = await self.executor.check_order_timeouts()
            if cancelled_count > 0:
                logger.warning("Cancelled expired orders", count=cancelled_count)
        except Exception as e:
            logger.error("Failed to check order timeouts", error=str(e))
        
        # 1. Check Data Health
        if not self.data_acq.is_healthy():
            logger.error("Data acquisition unhealthy")
            return

        # 2. Sync Active Positions (Global Sync)
        # Phase 2 Fix: Pass positions to _sync_positions to avoid duplicate API call
        try:
            # This updates global state in Repository and internal trackers
            if self.config.system.dry_run and not self.client.has_valid_futures_credentials():
                all_raw_positions = []
            else:
                all_raw_positions = await self.client.get_all_futures_positions()
            # Pass positions to sync to avoid duplicate API call
            await self._sync_positions(all_raw_positions)
        except Exception as e:
            logger.error("Failed to sync positions", error=str(e))
            return

        # 2.1 PRODUCTION HARDENING V2: Pre-tick Invariant Check
        # This checks all hard limits (drawdown, positions, margin) and halts if violated
        # Uses HardeningDecision enum for explicit state handling
        if self.hardening:
            try:
                # Get account info for invariant checks
                account_info = await self.client.get_futures_account_info()
                current_equity = Decimal(str(account_info.get("equity", 0)))
                available_margin = Decimal(str(account_info.get("availableMargin", 0)))
                margin_used = Decimal(str(account_info.get("marginUsed", 0)))
                margin_util = margin_used / current_equity if current_equity > 0 else Decimal("0")
                
                # Convert raw positions to Position objects for check
                position_objs = [self._convert_to_position(p) for p in all_raw_positions if p.get('size', 0) != 0]
                
                # Run pre-tick safety checks (returns HardeningDecision)
                decision = await self.hardening.pre_tick_check(
                    current_equity=current_equity,
                    open_positions=position_objs,
                    margin_utilization=margin_util,
                    available_margin=available_margin,
                )
                
                if decision == HardeningDecision.HALT:
                    logger.critical(
                        "TRADING_HALTED_BY_INVARIANT_MONITOR",
                        message="System halted - manual intervention required via clear_halt()",
                    )
                    # Ensure cleanup runs via finally block
                    return
                
                if decision == HardeningDecision.SKIP_TICK:
                    logger.debug("TICK_SKIPPED_BY_CYCLE_GUARD")
                    return
                
                # Log if new entries are blocked but position management allowed
                if not self.hardening.is_trading_allowed():
                    logger.warning(
                        "NEW_ENTRIES_BLOCKED",
                        system_state=self.hardening.invariant_monitor.state.value,
                        management_allowed=self.hardening.is_management_allowed(),
                    )
            except Exception as e:
                logger.error("Production hardening pre-tick check failed", error=str(e))
                # Don't halt trading due to hardening check failure - log and continue

        # 2.5. Cleanup orphan reduce-only orders (SL/TP orders for closed positions)
        try:
            await self._cleanup_orphan_reduce_only_orders(all_raw_positions)
        except Exception as e:
            logger.error("Failed to cleanup orphan orders", error=str(e))
            # Don't return - continue with trading loop

        # 3. Batch Data Fetching (Optimization)
        try:
            import time
            _t0 = time.perf_counter()
            market_symbols = self._market_symbols()
            # Health gate: pause new entries when candle health below threshold
            total_coins = len(market_symbols)
            coins_with_sufficient_candles = sum(
                1 for s in market_symbols if len(self.candle_manager.get_candles(s, "15m")) >= 50
            )
            min_healthy = getattr(self.config.data, "min_healthy_coins", 30)
            min_ratio = getattr(self.config.data, "min_health_ratio", 0.25)
            if total_coins > 0:
                ratio = coins_with_sufficient_candles / total_coins
                # When universe is smaller than min_healthy, require all coins to have data (ratio 1.0)
                effective_min = min(min_healthy, total_coins)
                if coins_with_sufficient_candles < effective_min or ratio < min_ratio:
                    self.trade_paused = True
                    logger.critical(
                        "TRADING PAUSED: candle health insufficient",
                        coins_with_sufficient_candles=coins_with_sufficient_candles,
                        total=total_coins,
                        min_healthy_coins=min_healthy,
                        effective_min=effective_min,
                        min_health_ratio=min_ratio,
                    )
                else:
                    self.trade_paused = False
            else:
                self.trade_paused = False
            map_spot_tickers = await self.client.get_spot_tickers_bulk(market_symbols)
            map_futures_tickers = await self.client.get_futures_tickers_bulk()
            _t1 = time.perf_counter()
            self.last_fetch_latency_ms = round((_t1 - _t0) * 1000)
            map_positions = {p["symbol"]: p for p in all_raw_positions}
            
            # Store latest futures tickers for use in _handle_signal and other call sites
            self.latest_futures_tickers = map_futures_tickers
            # Also store on executor for use in execute_signal
            self.executor.latest_futures_tickers = map_futures_tickers
            # Update adapter cache for use when futures_tickers not explicitly passed
            self.futures_adapter.update_cached_futures_tickers(map_futures_tickers)
            
            # Ensure instrument specs are loaded (used to decide tradability and size/leverage rules).
            # This is TTL-cached; refresh() is a cheap no-op when not stale.
            if getattr(self, "instrument_spec_registry", None):
                try:
                    await self.instrument_spec_registry.refresh()
                except Exception as e:
                    logger.warning("InstrumentSpecRegistry refresh failed (non-fatal)", error=str(e))
            
            # ShockGuard: Evaluate shock conditions and update state
            if self.shock_guard:
                # CRITICAL: Deduplicate futures tickers to one canonical symbol per asset
                # map_futures_tickers contains aliases (PI_*, PF_*, BASE/USD:USD, BASE/USD)
                # We need to pick one canonical format per asset to avoid false triggers
                def extract_base(symbol: str) -> Optional[str]:
                    """Extract base currency from symbol."""
                    for prefix in ["PI_", "PF_", "FI_"]:
                        if symbol.startswith(prefix):
                            symbol = symbol[len(prefix):]
                    # IMPORTANT: match longer suffixes first ("/USD:USD" must not be reduced by "USD").
                    for suffix in ["/USD:USD", "/USD", "USD"]:
                        if symbol.endswith(suffix):
                            symbol = symbol[:-len(suffix)]
                    return symbol.rstrip(":/") if symbol else None
                
                # Build canonical mark prices (prefer CCXT unified BASE/USD:USD, else PF_*)
                canonical_mark_prices = {}
                base_to_symbol = {}  # Track which symbol we chose per base
                for symbol, mark_price in map_futures_tickers.items():
                    base = extract_base(symbol)
                    if not base:
                        continue
                    # Prefer CCXT unified format, else PF_ format
                    if base not in base_to_symbol:
                        base_to_symbol[base] = symbol
                        canonical_mark_prices[symbol] = mark_price
                    elif "/USD:USD" in symbol and "/USD:USD" not in base_to_symbol[base]:
                        # Upgrade to CCXT unified if available
                        canonical_mark_prices.pop(base_to_symbol[base], None)
                        base_to_symbol[base] = symbol
                        canonical_mark_prices[symbol] = mark_price
                    elif symbol.startswith("PF_") and not base_to_symbol[base].startswith("PF_") and "/USD:USD" not in base_to_symbol[base]:
                        # Use PF_ as fallback
                        canonical_mark_prices.pop(base_to_symbol[base], None)
                        base_to_symbol[base] = symbol
                        canonical_mark_prices[symbol] = mark_price
                
                # Spot prices already use canonical format (spot symbols)
                spot_prices_dict = {}
                for symbol, ticker in map_spot_tickers.items():
                    if isinstance(ticker, dict) and "last" in ticker:
                        spot_prices_dict[symbol] = Decimal(str(ticker["last"]))
                
                # Evaluate shock conditions with canonical symbols only
                shock_detected = self.shock_guard.evaluate(
                    mark_prices=canonical_mark_prices,
                    spot_prices=spot_prices_dict if spot_prices_dict else None,
                )
                
                # Run exposure reduction if shock active
                if self.shock_guard.shock_mode_active:
                    # Get positions as Position objects
                    positions_list = []
                    liquidation_prices_dict = {}
                    # Build mark prices keyed by position symbols (exchange symbols like PF_*)
                    # Positions use exchange symbols, so we need mark prices for those symbols
                    mark_prices_for_positions = {}
                    for pos_data in all_raw_positions:
                        pos = self._convert_to_position(pos_data)
                        positions_list.append(pos)
                        liquidation_prices_dict[pos.symbol] = pos.liquidation_price
                        
                        # Get mark price for this position symbol (try multiple formats)
                        pos_symbol = pos.symbol
                        mark_price = None
                        # Try direct lookup first
                        if pos_symbol in map_futures_tickers:
                            mark_price = map_futures_tickers[pos_symbol]
                        else:
                            # Try to find any alias for this symbol
                            for ticker_symbol, ticker_price in map_futures_tickers.items():
                                # Extract base from both and compare
                                pos_base = extract_base(pos_symbol)
                                ticker_base = extract_base(ticker_symbol)
                                if pos_base and ticker_base and pos_base == ticker_base:
                                    mark_price = ticker_price
                                    break
                        
                        # Fallback to position data if available
                        if not mark_price:
                            mark_price = Decimal(str(pos_data.get("markPrice", pos_data.get("mark_price", pos_data.get("entryPrice", 0)))))
                        
                        mark_prices_for_positions[pos_symbol] = mark_price
                    
                    # Get exposure reduction actions
                    actions = self.shock_guard.get_exposure_reduction_actions(
                        positions=positions_list,
                        mark_prices=mark_prices_for_positions,
                        liquidation_prices=liquidation_prices_dict,
                    )
                    
                    # Execute actions
                    for action_item in actions:
                        try:
                            symbol = action_item.symbol
                            if action_item.action.value == "CLOSE":
                                logger.warning(
                                    "ShockGuard: Closing position (emergency)",
                                    symbol=symbol,
                                    buffer_pct=float(action_item.buffer_pct),
                                    reason=action_item.reason,
                                )
                                await self.client.close_position(symbol)
                            elif action_item.action.value == "TRIM":
                                # Get current position size
                                pos_data = map_positions.get(symbol)
                                if pos_data:
                                    # Get position size - check if system uses notional or contracts
                                    current_size_raw = Decimal(str(pos_data.get("size", 0)))
                                    
                                    # Determine if size is in contracts or notional
                                    # If position_size_is_notional is True, size is USD notional
                                    # If False, size is in contracts
                                    position_size_is_notional = getattr(
                                        self.config.exchange, "position_size_is_notional", False
                                    )
                                    
                                    if position_size_is_notional:
                                        # Size is in USD notional - trim by 50%
                                        trim_notional = current_size_raw * Decimal("0.5")
                                        # Convert notional to contracts using proper adapter method
                                        # Use mark_prices_for_positions which is keyed by position symbols
                                        mark_price = mark_prices_for_positions.get(symbol)
                                        if not mark_price or mark_price <= 0:
                                            # Fallback to position data
                                            mark_price = Decimal(str(pos_data.get("markPrice", pos_data.get("mark_price", pos_data.get("entryPrice", 1)))))
                                        if mark_price > 0:
                                            # Use adapter's notional_to_contracts method for proper conversion
                                            trim_size_contracts = self.futures_adapter.notional_to_contracts(
                                                trim_notional, mark_price
                                            )
                                        else:
                                            logger.error("ShockGuard: Cannot trim - invalid mark price", symbol=symbol)
                                            continue
                                    else:
                                        # Size is in contracts - trim by 50%
                                        trim_size_contracts = current_size_raw * Decimal("0.5")
                                    
                                    # Determine side for reduce-only order
                                    side_raw = pos_data.get("side", "long").lower()
                                    close_side = "sell" if side_raw == "long" else "buy"
                                    
                                    logger.warning(
                                        "ShockGuard: Trimming position",
                                        symbol=symbol,
                                        buffer_pct=float(action_item.buffer_pct),
                                        current_size=str(current_size_raw),
                                        trim_size_contracts=str(trim_size_contracts),
                                        position_size_is_notional=position_size_is_notional,
                                        reason=action_item.reason,
                                    )
                                    
                                    # Place reduce-only market order to trim (reduce_only=True: exit, no dust)
                                    futures_symbol = symbol
                                    await self.client.place_futures_order(
                                        symbol=futures_symbol,
                                        side=close_side,
                                        order_type="market",
                                        size=float(trim_size_contracts),
                                        reduce_only=True,
                                    )
                        except Exception as e:
                            logger.error(
                                "ShockGuard: Failed to execute exposure reduction",
                                symbol=action_item.symbol,
                                action=action_item.action.value,
                                error=str(e),
                            )
            
            # 2.4. Fetch open orders once, index by *normalized* symbol (for position hydration)
            # This is critical because positions are PF_* while CCXT orders often use unified symbols (e.g. X/USD:USD).
            from src.data.symbol_utils import normalize_symbol_for_position_match
            orders_by_symbol: Dict[str, List[Dict]] = {}
            try:
                # CRITICAL: Verify client is not a mock before calling
                from unittest.mock import Mock, MagicMock
                import sys
                import os
                is_test = (
                    "pytest" in sys.modules or
                    "PYTEST_CURRENT_TEST" in os.environ or
                    any("test" in path.lower() for path in sys.path if isinstance(path, str))
                )
                if not is_test and (isinstance(self.client, Mock) or isinstance(self.client, MagicMock)):
                    logger.critical("CRITICAL: self.client is a Mock/MagicMock in _tick!")
                    raise RuntimeError("CRITICAL: self.client is a Mock/MagicMock in production")
                
                open_orders = await self.client.get_futures_open_orders()
                for order in open_orders:
                    sym = order.get('symbol')
                    key = normalize_symbol_for_position_match(sym) if sym else ""
                    if key:
                        if key not in orders_by_symbol:
                            orders_by_symbol[key] = []
                        orders_by_symbol[key].append(order)
            except RuntimeError:
                raise  # Re-raise critical errors
            except Exception as e:
                logger.warning("Failed to fetch open orders for hydration", error=str(e))
            
            # 2.5. TP Backfill / Reconciliation (after position sync and price data fetch)
            try:
                # Build current prices map for backfill logic (use futures ticker data)
                current_prices_map = {}
                for pos_data in all_raw_positions:
                    symbol = pos_data.get('symbol')
                    if symbol:
                        # map_futures_tickers is Dict[str, Decimal] - mark price directly
                        mark_price = map_futures_tickers.get(symbol)
                        if mark_price:
                            current_prices_map[symbol] = mark_price
                        else:
                            current_prices_map[symbol] = Decimal(str(pos_data.get('markPrice', pos_data.get('mark_price', pos_data.get('entryPrice', 0)))))
                
                # Reconcile stop loss order IDs from exchange FIRST
                # This updates is_protected flag based on actual exchange orders
                await self._reconcile_stop_loss_order_ids(all_raw_positions)
                
                # THEN do TP backfill (which checks is_protected)
                await self._reconcile_protective_orders(all_raw_positions, current_prices_map)

                # Auto-place missing stops for unprotected positions (rate-limited per tick)
                await self._place_missing_stops_for_unprotected(all_raw_positions, max_per_tick=3)
            except Exception as e:
                logger.error("TP backfill reconciliation failed", error=str(e))
                # Don't return - continue with trading loop
            symbols_with_spot = len([s for s in market_symbols if s in map_spot_tickers])
            # Use futures_tickers for accurate coverage counting
            symbols_with_futures = len([
                s for s in market_symbols
                if self.futures_adapter.map_spot_to_futures(s, futures_tickers=map_futures_tickers) in map_futures_tickers
            ])
            symbols_with_neither = len([
                s for s in market_symbols
                if s not in map_spot_tickers
                and self.futures_adapter.map_spot_to_futures(s, futures_tickers=map_futures_tickers) not in map_futures_tickers
            ])
            self._last_ticker_with = symbols_with_spot
            self._last_ticker_without = len(market_symbols) - symbols_with_spot
            self._last_futures_count = symbols_with_futures
            if symbols_with_neither > 0 or symbols_with_futures < len(market_symbols):
                self._last_ticker_skip_log = getattr(self, "_last_ticker_skip_log", datetime.min.replace(tzinfo=timezone.utc))
                if (datetime.now(timezone.utc) - self._last_ticker_skip_log).total_seconds() >= 300:
                    logger.warning(
                        "Ticker coverage: spot/futures",
                        total=len(market_symbols),
                        with_spot=symbols_with_spot,
                        with_futures=symbols_with_futures,
                        with_neither=symbols_with_neither,
                        hint="Trading requires futures ticker; ensure bulk API keys match discovery (CCXT vs PF_*).",
                    )
                    self._last_ticker_skip_log = datetime.now(timezone.utc)
        except Exception as e:
            logger.error("Failed batch data fetch", error=str(e))
            return

        # 4. Parallel Analysis Loop
        # Semaphore to control concurrency for candle fetching.
        # Most time is I/O-bound (waiting on Kraken API), so higher concurrency is safe.
        sem = asyncio.Semaphore(50)
        
        async def process_coin(spot_symbol: str):
            async with sem:
                try:
                    # Use futures tickers for improved mapping
                    futures_symbol = self.futures_adapter.map_spot_to_futures(spot_symbol, futures_tickers=map_futures_tickers)
                    has_spot = spot_symbol in map_spot_tickers
                    has_futures = futures_symbol in map_futures_tickers
                    
                    # Debug: Log when futures symbol not found
                    if not has_futures and spot_symbol in map_spot_tickers:
                        # Check if similar futures symbols exist
                        similar = [s for s in map_futures_tickers.keys() if spot_symbol.split('/')[0].upper() in s.upper()][:3]
                        logger.debug(
                            "Futures symbol not found for signal",
                            spot_symbol=spot_symbol,
                            mapped_futures=futures_symbol,
                            similar_futures=similar,
                            total_futures_available=len(map_futures_tickers)
                        )
                    
                    if not has_spot and not has_futures:
                        return  # Skip if no ticker (spot or futures)

                    if has_spot:
                        spot_ticker = map_spot_tickers[spot_symbol]
                        spot_price = Decimal(str(spot_ticker["last"]))
                    else:
                        spot_price = None
                    # map_futures_tickers is Dict[str, Decimal] - mark price directly
                    mark_price = map_futures_tickers.get(futures_symbol) if has_futures else None
                    if not mark_price:
                        mark_price = spot_price
                    if not mark_price:
                        return
                    if spot_price is None:
                        spot_price = mark_price  # Use futures mark when no spot ticker (futures-only)
                    
                    # Tradability gate:
                    # - Must have a futures ticker
                    # - Must have an instrument spec (otherwise execution will fail with NO_SPEC)
                    has_spec = True
                    if has_futures and getattr(self, "instrument_spec_registry", None):
                        try:
                            has_spec = self.instrument_spec_registry.get_spec(futures_symbol) is not None
                        except Exception:
                            has_spec = False
                    
                    skip_reason: Optional[str] = None
                    if not has_futures:
                        skip_reason = "no_futures_ticker"
                    elif not has_spec:
                        skip_reason = "no_instrument_spec"
                        # Throttle to avoid log spam if a spot market exists but the futures instrument is not tradeable/known.
                        now = datetime.now(timezone.utc)
                        last_map = getattr(self, "_last_no_spec_log", {})
                        last = last_map.get(futures_symbol, datetime.min.replace(tzinfo=timezone.utc))
                        if (now - last).total_seconds() >= 3600:
                            logger.warning(
                                "Signal skipped (no instrument spec)",
                                spot_symbol=spot_symbol,
                                futures_symbol=futures_symbol,
                                reason="NO_SPEC",
                                hint="Futures ticker exists but instrument specs missing; likely delisted/non-tradeable. Skipping to avoid AUCTION_OPEN_REJECTED spam.",
                            )
                            last_map[futures_symbol] = now
                            self._last_no_spec_log = last_map
                    
                    is_tradable = skip_reason is None

                    # Update Candles (spot first; futures fallback when spot unavailable)
                    await self._update_candles(spot_symbol)
                    
                    # Position Management (V2 State Machine)
                    position_data = map_positions.get(futures_symbol)
                    if position_data:
                        symbol = position_data['symbol']
                        try:
                            v2_actions = self.position_manager_v2.evaluate_position(
                                symbol=symbol,
                                current_price=mark_price,
                                current_atr=None,
                            )
                            if v2_actions:
                                await self.execution_gateway.execute_actions(v2_actions)
                        except Exception as e:
                            logger.error(
                                "V2 position evaluation failed",
                                symbol=symbol,
                                error=str(e),
                                error_type=type(e).__name__,
                            )
                            
                    # ShockGuard: Skip signal generation if entries paused
                    if self.shock_guard and self.shock_guard.should_pause_entries():
                        logger.debug(
                            "ShockGuard: Skipping signal generation (entries paused)",
                            symbol=spot_symbol,
                        )
                        return
                    
                    # Signal Generation (SMC)
                    # Use 15m candles (primary timeframe)
                    # NOTE: _update_candles ensures self.candles_15m is populated
                    candles = self.candle_manager.get_candles(spot_symbol, "15m")
                    candle_count = len(candles)
                    
                    # Update processing stats
                    if spot_symbol not in self.coin_processing_stats:
                        self.coin_processing_stats[spot_symbol] = {
                            "processed_count": 0,
                            "last_processed": datetime.min.replace(tzinfo=timezone.utc),
                            "candle_count": 0
                        }
                    
                    prev_count = self.coin_processing_stats[spot_symbol]["candle_count"]
                        
                    self.coin_processing_stats[spot_symbol]["processed_count"] += 1
                    self.coin_processing_stats[spot_symbol]["last_processed"] = datetime.now(timezone.utc)
                    self.coin_processing_stats[spot_symbol]["candle_count"] = candle_count
                    
                    if prev_count > 50 and candle_count == 0:
                        logger.critical("Data Depth Drop Detected!", symbol=spot_symbol, prev=prev_count, now=0)

                    if candle_count < 50:
                        # Still log trace even if insufficient candles (monitoring status)
                        now = datetime.now(timezone.utc)
                        last_trace = self.last_trace_log.get(spot_symbol, datetime.min.replace(tzinfo=timezone.utc))
                        
                        if (now - last_trace).total_seconds() > 300: # 5 minutes
                            try:
                                from src.storage.repository import async_record_event
                                
                                trace_details = {
                                    "signal": "NO_SIGNAL",
                                    "regime": "unknown",
                                    "bias": "neutral",
                                    "adx": 0.0,
                                    "atr": 0.0,
                                    "ema200_slope": "flat",
                                    "spot_price": float(spot_price),
                                    "setup_quality": 0.0,
                                    "score_breakdown": {},
                                    "status": "monitoring",
                                    "candle_count": candle_count,
                                    "reason": "insufficient_candles"
                                }
                                
                                await async_record_event(
                                    event_type="DECISION_TRACE",
                                    symbol=spot_symbol,
                                    details=trace_details,
                                    timestamp=now
                                )
                                self.last_trace_log[spot_symbol] = now
                            except Exception as e:
                                logger.error("Failed to record monitoring trace", symbol=spot_symbol, error=str(e))
                        return

                    # 4H DECISION AUTHORITY HIERARCHY:
                    # 1D: Regime filter (EMA200 bias)
                    # 4H: Decision authority (OB/FVG/BOS, ATR for stops)
                    # 1H: Refinement (ADX, swing points)
                    # 15m: Refinement (entry timing)
                    signal = self.smc_engine.generate_signal(
                        symbol=spot_symbol,
                        regime_candles_1d=self.candle_manager.get_candles(spot_symbol, "1d"),
                        decision_candles_4h=self.candle_manager.get_candles(spot_symbol, "4h"),
                        refine_candles_1h=self.candle_manager.get_candles(spot_symbol, "1h"),
                        refine_candles_15m=candles,
                    )
                    
                    # Pass context to signal for execution (mark price for futures)
                    # Signal is spot-based, execution is futures-based.
                    order_outcome = None
                    if signal.signal_type != SignalType.NO_SIGNAL:
                        # Signal cooldown: prevent the same symbol from re-signalling
                        # every cycle (which caused the SPX compounding bug).
                        # Once a signal fires for a symbol, suppress re-signals for 4 hours.
                        cooldown_until = self._signal_cooldown.get(spot_symbol)
                        now_cd = datetime.now(timezone.utc)
                        if cooldown_until and now_cd < cooldown_until:
                            pass  # Signal suppressed by cooldown — skip silently
                        else:
                            # Pre-entry spread check (fail-open: if anything fails, allow the trade).
                            # Uses already-fetched spot ticker bid/ask — zero new API calls.
                            spread_ok = True
                            try:
                                st = map_spot_tickers.get(spot_symbol)
                                if st:
                                    bid = Decimal(str(st.get("bid", 0) or 0))
                                    ask = Decimal(str(st.get("ask", 0) or 0))
                                    if bid > 0 and ask > 0:
                                        live_spread = (ask - bid) / bid
                                        max_entry_spread = Decimal("0.010")  # 1.0% — reject extreme spreads only
                                        if live_spread > max_entry_spread:
                                            spread_ok = False
                                            logger.warning(
                                                "SIGNAL_REJECTED_SPREAD: live spread too wide for entry",
                                                symbol=spot_symbol,
                                                spread=f"{live_spread:.3%}",
                                                threshold=f"{max_entry_spread:.3%}",
                                                signal=signal.signal_type.value,
                                            )
                            except Exception:
                                pass  # Fail-open: allow trade if check errors

                            if not spread_ok:
                                return  # Skip this coin — spread too wide right now

                            # Record cooldown for this symbol
                            self._signal_cooldown[spot_symbol] = now_cd + timedelta(
                                hours=self._signal_cooldown_hours
                            )
                        
                            # Collect signal for auction mode (if enabled)
                            if self.auction_allocator and is_tradable:
                                self.auction_signals_this_tick.append((signal, spot_price, mark_price))
                        
                            if not is_tradable:
                                logger.warning(
                                    "Signal skipped (not tradable)",
                                    symbol=spot_symbol,
                                    signal=signal.signal_type.value,
                                    futures_symbol=futures_symbol,
                                    skip_reason=skip_reason,
                                )
                            else:
                                # In auction mode, skip individual signal handling - auction will decide
                                if not self.auction_allocator:
                                    order_outcome = await self._handle_signal(signal, spot_price, mark_price)
                    
                    # Trace Logging (Throttled)
                    now = datetime.now(timezone.utc)
                    last_trace = self.last_trace_log.get(spot_symbol, datetime.min.replace(tzinfo=timezone.utc))
                    
                    if (now - last_trace).total_seconds() > 300: # 5 minutes
                        try:
                            from src.storage.repository import async_record_event
                            
                            trace_details = {
                                "signal": signal.signal_type.value,
                                "regime": signal.regime,
                                "bias": signal.higher_tf_bias,
                                "adx": float(signal.adx) if signal.adx else 0.0,
                                "atr": float(signal.atr) if signal.atr else 0.0,
                                "ema200_slope": signal.ema200_slope,
                                "spot_price": float(spot_price),
                                "setup_quality": sum(float(v) for v in (signal.score_breakdown or {}).values()),
                                "score_breakdown": signal.score_breakdown or {},
                                "status": "active",
                                "candle_count": candle_count,
                                "reason": signal.reasoning,  # CAPTURE REASON
                                "structure": signal.structure_info,
                                "meta": signal.meta_info
                            }
                            if signal.signal_type != SignalType.NO_SIGNAL:
                                trace_details["skipped"] = not is_tradable
                                if not is_tradable:
                                    trace_details["skip_reason"] = skip_reason or "unknown"
                                elif order_outcome is not None:
                                    trace_details["order_placed"] = order_outcome.get("order_placed", False)
                                    if not order_outcome.get("order_placed") and order_outcome.get("reason"):
                                        trace_details["order_fail_reason"] = order_outcome["reason"]

                            if signal.signal_type == SignalType.NO_SIGNAL and signal.reasoning:
                                # Downgrade to debug: this fires for most coins every cycle.
                                # Reasoning is still captured in DECISION_TRACE event below.
                                logger.debug(
                                    "SMC Analysis: NO_SIGNAL",
                                    symbol=spot_symbol,
                                    reasoning=signal.reasoning.replace("\n", " | "),
                                )
                            
                            await async_record_event(
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
        # Clear signal collection for this tick
        if self.auction_allocator:
            self.auction_signals_this_tick = []
        
        # CRITICAL: Only process the filtered universe.
        # `self.markets` may include symbols that must be hard-blocked (e.g. fiat pairs),
        # and `process_coin()` can still trade them via futures tickers even without spot tickers.
        await asyncio.gather(*[process_coin(s) for s in market_symbols], return_exceptions=True)
        
        # Run auction mode allocation (if enabled) - after all signals processed
        if self.auction_allocator:
            signals_count = len(self.auction_signals_this_tick)
            logger.info("AUCTION_START", signals_collected=signals_count)
            logger.info(
                "Auction: About to run allocation",
                signals_collected=signals_count,
                auction_allocator_exists=bool(self.auction_allocator),
            )
            await self._run_auction_allocation(all_raw_positions)
            logger.info("AUCTION_END", signals_collected=signals_count)
        else:
            logger.debug("Auction: Skipped (auction_allocator is None)")
        
        # Phase 2: Batch save all collected candles (grouped by symbol/timeframe)
        # Phase 2: Batch save all collected candles (delegated to Manager)
        await self.candle_manager.flush_pending()
        
        # Log periodic status summary (every 5 minutes)
        now = datetime.now(timezone.utc)
        if (now - self.last_status_summary).total_seconds() > 300:  # 5 minutes
            try:
                total_coins = len(market_symbols)
                coins_with_candles = sum(1 for s in market_symbols if len(self.candle_manager.get_candles(s, "15m")) >= 50)
                coins_processed_recently = sum(
                    1 for s in market_symbols
                    if self.coin_processing_stats.get(s, {}).get("last_processed", datetime.min.replace(tzinfo=timezone.utc)) > (now - timedelta(minutes=10))
                )
                coins_with_traces = len([s for s in market_symbols if s in self.last_trace_log])
                
                summary = {
                    "total_coins": total_coins,
                    "coins_with_sufficient_candles": coins_with_candles,
                    "coins_processed_recently": coins_processed_recently,
                    "coins_with_traces": coins_with_traces,
                    "coins_waiting_for_candles": total_coins - coins_with_candles,
                }
                if getattr(self, "_last_ticker_with", None) is not None:
                    summary["symbols_with_ticker"] = self._last_ticker_with
                    summary["symbols_without_ticker"] = getattr(self, "_last_ticker_without", 0)
                if getattr(self, "_last_futures_count", None) is not None:
                    summary["symbols_with_futures"] = self._last_futures_count
                fc = self.candle_manager.pop_futures_fallback_count()
                if fc > 0:
                    summary["coins_futures_fallback_used"] = fc
                logger.info("Coin processing status summary", **summary)
                self.last_status_summary = now
            except Exception as e:
                logger.error("Failed to log status summary", error=str(e))
        
        # 4.5 CRITICAL: Validate all positions have stop loss protection
        # Legacy validation removed - using new _validate_position_protection after initial tick
        
        # 5. Account Sync (Throttled) - Moved to step 2 to prevent duplicate calls
        # Reference: _sync_positions call in Step 2 handles global state update
            
        # 7. Operational Maintenance (Daily)
        now = datetime.now(timezone.utc)
        if (now - self.last_maintenance_run).total_seconds() > 86400: # 24 hours
            try:
                results = self.db_pruner.run_maintenance()
                logger.info("Daily database maintenance complete", results=results)
                self.last_maintenance_run = now
            except Exception as e:
                logger.error("Daily maintenance failed", error=str(e))

        # 8. Periodic data maintenance (hourly): stale/missing trace recovery
        if (now - self.last_data_maintenance).total_seconds() > 3600:
            try:
                await periodic_data_maintenance(self._market_symbols(), max_age_hours=6.0)
                self.last_data_maintenance = now
            except Exception as e:
                logger.error("Periodic data maintenance failed", error=str(e))

        # 9. PRODUCTION HARDENING V2: Post-tick Cleanup
        # CRITICAL: This must always run, even on exceptions
        # The post_tick_cleanup() method internally uses try/finally to ensure lock release
        if self.hardening:
            self.hardening.post_tick_cleanup()

    # _background_hydration_task removed (Replaced by CandleManager.initialize)

    # ===== AUTO HALT RECOVERY =====
    _AUTO_RECOVERY_MAX_PER_DAY = 2
    _AUTO_RECOVERY_COOLDOWN_SECONDS = 300  # 5 minutes since halt
    _AUTO_RECOVERY_MARGIN_SAFE_PCT = 85  # Must be below this to recover

    async def _try_auto_recovery(self) -> bool:
        """
        Attempt automatic recovery from kill switch (margin_critical only).
        
        Rules (ALL must be true):
        1. Kill switch reason is MARGIN_CRITICAL
        2. At least 5 minutes since the halt was activated
        3. Current margin utilization is below 85% (well below 92% trigger)
        4. Fewer than 2 auto-recoveries in the last 24 hours
        
        Returns:
            True if recovery was successful, False otherwise
        """
        if not self.kill_switch or not self.kill_switch.is_active():
            return False
        
        if self.kill_switch.reason != KillSwitchReason.MARGIN_CRITICAL:
            return False
        
        now = datetime.now(timezone.utc)
        
        # Rule 2: Cooldown since halt activation
        if self.kill_switch.activated_at:
            elapsed = (now - self.kill_switch.activated_at).total_seconds()
            if elapsed < self._AUTO_RECOVERY_COOLDOWN_SECONDS:
                logger.debug(
                    "Auto-recovery: waiting for cooldown",
                    elapsed_seconds=int(elapsed),
                    required_seconds=self._AUTO_RECOVERY_COOLDOWN_SECONDS,
                )
                return False
        
        # Rule 4: Max recoveries per day
        cutoff = now - timedelta(hours=24)
        recent_attempts = [t for t in self._auto_recovery_attempts if t > cutoff]
        self._auto_recovery_attempts = recent_attempts  # Prune old entries
        if len(recent_attempts) >= self._AUTO_RECOVERY_MAX_PER_DAY:
            logger.warning(
                "Auto-recovery: daily limit reached (system needs manual intervention)",
                attempts_today=len(recent_attempts),
                max_per_day=self._AUTO_RECOVERY_MAX_PER_DAY,
            )
            return False
        
        # Rule 3: Check current margin utilization
        try:
            account_info = await self.client.get_futures_account_info()
            equity = Decimal(str(account_info.get("equity", 0)))
            margin_used = Decimal(str(account_info.get("marginUsed", 0)))
            
            if equity <= 0:
                return False
            
            margin_util_pct = float((margin_used / equity) * 100)
            
            if margin_util_pct >= self._AUTO_RECOVERY_MARGIN_SAFE_PCT:
                logger.info(
                    "Auto-recovery: margin still too high",
                    margin_util_pct=f"{margin_util_pct:.1f}",
                    required_below=self._AUTO_RECOVERY_MARGIN_SAFE_PCT,
                )
                return False
            
            # All conditions met — recover!
            self._auto_recovery_attempts.append(now)
            
            logger.critical(
                "AUTO_RECOVERY: Clearing kill switch (margin recovered)",
                margin_util_pct=f"{margin_util_pct:.1f}",
                recovery_attempt=len(self._auto_recovery_attempts),
                max_per_day=self._AUTO_RECOVERY_MAX_PER_DAY,
                halt_duration_seconds=int((now - self.kill_switch.activated_at).total_seconds())
                    if self.kill_switch.activated_at else 0,
            )
            
            # Clear kill switch
            self.kill_switch.acknowledge()
            
            # Also clear hardening halt state if it exists
            if self.hardening and self.hardening.is_halted():
                self.hardening.clear_halt(operator="auto_recovery")
            
            # Send alert
            try:
                from src.monitoring.alerting import send_alert_sync
                send_alert_sync(
                    "AUTO_RECOVERY",
                    f"System auto-recovered from MARGIN_CRITICAL\n"
                    f"Margin utilization: {margin_util_pct:.1f}%\n"
                    f"Recovery #{len(self._auto_recovery_attempts)} of {self._AUTO_RECOVERY_MAX_PER_DAY}/day",
                    urgent=True,
                )
            except Exception:
                pass
            
            return True
            
        except Exception as e:
            logger.warning("Auto-recovery: failed to check margin", error=str(e))
            return False

    async def _sync_account_state(self):
        """Fetch and persist real-time account state."""
        try:
            # 1. Get Balances
            balance = await self.client.get_futures_balance()
            if not balance:
                return

            # 2. Calculate Effective Equity (Shared Logic)
            base = getattr(self.config.exchange, "base_currency", "USD")
            equity, avail_margin, margin_used_val = await calculate_effective_equity(
                balance, base_currency=base, kraken_client=self.client
            )

            # 3. Persist
            save_account_state(
                equity=equity,
                balance=equity, # For futures margin, equity IS the balance relevant for trading
                margin_used=margin_used_val,
                available_margin=avail_margin,
                unrealized_pnl=Decimal("0.0") # Included in portfolioValue usually
            )
            
            # 4. Initialize daily loss tracking if not set
            if self.risk_manager.daily_start_equity <= 0:
                self.risk_manager.reset_daily_metrics(equity)
                logger.info("Daily loss tracking initialized", starting_equity=str(equity))
            
        except Exception as e:
            logger.error("Failed to sync account state", error=str(e))
    
    async def _handle_signal(
        self, 
        signal: Signal, 
        spot_price: Decimal, 
        mark_price: Decimal,
    ) -> dict:
        """
        Process signal through Position State Machine V2.
        
        Args:
            signal: Trading signal
            spot_price: Current spot price
            mark_price: Current futures mark price
        
        Returns:
            dict with keys:
                - order_placed: bool (True if order was placed, False if rejected/failed)
                - reason: str (human-readable reason for success/failure)
                - rejection_reasons: list[str] (if rejected, list of rejection reasons from RiskManager)
        """
        self.signals_since_emit += 1
        logger.info("New signal detected", type=signal.signal_type.value, symbol=signal.symbol)
        
        # Health gate: no new entries when candle health is insufficient
        if getattr(self, "trade_paused", False):
            return {
                "order_placed": False,
                "reason": "TRADING PAUSED: candle health insufficient",
                "rejection_reasons": ["trade_paused"],
            }
        
        return await self._handle_signal_v2(signal, spot_price, mark_price)

    async def _handle_signal_v2(
        self, signal: Signal, spot_price: Decimal, mark_price: Decimal
    ) -> dict:
        """
        Process signal through Position State Machine V2.
        
        CRITICAL: All orders flow through ExecutionGateway.
        No direct exchange calls allowed.

        Returns:
            {"order_placed": bool, "reason": str | None}
            reason is set when order_placed is False (e.g. risk_rejected, state_machine_rejected, entry_failed).
        """
        import uuid

        def _fail(reason: str) -> dict:
            return {"order_placed": False, "reason": reason}

        def _ok() -> dict:
            return {"order_placed": True, "reason": None}
        
        logger.info("Processing signal via State Machine V2", 
                   symbol=signal.symbol, 
                   type=signal.signal_type.value)
        
        # 1. Fetch Account Equity and Available Margin
        balance = await self.client.get_futures_balance()
        base = getattr(self.config.exchange, "base_currency", "USD")
        equity, available_margin, _ = await calculate_effective_equity(
            balance, base_currency=base, kraken_client=self.client
        )
        if equity <= 0:
            logger.error("Insufficient equity for trading", equity=str(equity))
            return _fail("Insufficient equity for trading")
        # 2. Risk Validation (Safety Gate)
        # Get symbol tier for tier-specific sizing
        symbol_tier = self.market_discovery.get_symbol_tier(signal.symbol) if self.market_discovery else "C"
        if symbol_tier != "A":
            static_tier = self._get_static_tier(signal.symbol)
            if static_tier == "A":
                logger.warning(
                    "Tier downgrade detected",
                    symbol=signal.symbol,
                    static_tier=static_tier,
                    dynamic_tier=symbol_tier,
                    reason="Dynamic classification is authoritative",
                )
        
        decision = self.risk_manager.validate_trade(
            signal, equity, spot_price, mark_price,
            available_margin=available_margin,
            symbol_tier=symbol_tier,
        )
        
        if not decision.approved:
            reasons = getattr(decision, "rejection_reasons", []) or []
            detail = reasons[0] if reasons else "Trade rejected by Risk Manager"
            logger.warning("Trade rejected by Risk Manager", symbol=signal.symbol, reasons=reasons)
            return _fail(f"Risk Manager rejected: {detail}")
        logger.info("Risk approved", symbol=signal.symbol, notional=str(decision.position_notional))

        # 3. Map to futures symbol (use latest futures tickers for optimal mapping)
        futures_symbol = self.futures_adapter.map_spot_to_futures(
            signal.symbol,
            futures_tickers=self.latest_futures_tickers
        )
        
        # 4. Generate entry plan to get TP levels
        order_intent = self.execution_engine.generate_entry_plan(
            signal,
            decision.position_notional,
            spot_price,
            mark_price,
            decision.leverage
        )
        
        tps = order_intent.get('take_profits', [])
        tp1_price = tps[0]['price'] if len(tps) > 0 else None
        tp2_price = tps[1]['price'] if len(tps) > 1 else None
        final_target = tps[-1]['price'] if len(tps) > 2 else None
        
        # 5. Calculate position size in contracts
        # Using the order_intent which has the calculated size
        position_size = Decimal(str(order_intent.get('size', 0)))
        if position_size <= 0:
            position_size = decision.position_notional / mark_price
        
        # 6. Evaluate entry via Position Manager V2
        # This enforces Invariant A (single position) and Invariant E (no reversal without close)
        action, position = self.position_manager_v2.evaluate_entry(
            signal=signal,
            entry_price=mark_price,
            stop_price=order_intent['metadata']['fut_sl'],
            tp1_price=tp1_price,
            tp2_price=tp2_price,
            final_target=final_target,
            position_size=position_size,
            trade_type=signal.regime if hasattr(signal, 'regime') else "tight_smc",
            leverage=decision.leverage,
        )
        
        if action.type == ActionTypeV2.REJECT_ENTRY:
            logger.warning("Entry REJECTED by State Machine", symbol=signal.symbol, reason=action.reason)
            return _fail(f"State Machine rejected: {action.reason or 'REJECT_ENTRY'}")
        logger.info("State machine accepted entry", symbol=signal.symbol, client_order_id=action.client_order_id)

        # 7. Handle opportunity cost replacement via V2
        if decision.should_close_existing and decision.close_symbol:
            logger.warning("Opportunity cost replacement via V2",
                          closing=decision.close_symbol,
                          opening=signal.symbol)
            
            # Request reversal close
            close_actions = self.position_manager_v2.request_reversal(
                decision.close_symbol,
                Side.LONG if signal.signal_type == SignalType.LONG else Side.SHORT,
                mark_price
            )
            
            # Execute close actions via gateway
            for close_action in close_actions:
                result = await self.execution_gateway.execute_action(close_action)
                if not result.success:
                    logger.error("Failed to close for replacement", error=result.error)
                    return _fail(f"Failed to close for replacement: {result.error}")
            
            # Confirm reversal is closed
            self.position_registry.confirm_reversal_closed(decision.close_symbol)
        
        # 8. Register position in state machine
        # This is done BEFORE order placement to ensure we track the position
        position.entry_order_id = action.client_order_id
        position.entry_client_order_id = action.client_order_id
        position.futures_symbol = futures_symbol  # For PLACE_STOP / UPDATE_STOP exchange calls
        
        try:
            self.position_registry.register_position(position)
        except Exception as e:
            logger.error("Failed to register position", error=str(e))
            return _fail(f"Failed to register position: {e}")
        
        # 9. Execute entry via Execution Gateway
        # This is the ONLY way to place orders. Use futures symbol for exchange (Kraken expects X/USD:USD).
        logger.info("Submitting entry to gateway", symbol=futures_symbol, client_order_id=action.client_order_id)
        result = await self.execution_gateway.execute_action(action, order_symbol=futures_symbol)
        
        if not result.success:
            logger.error("Entry failed", error=result.error)
            # Mark position as error
            position.mark_error(f"Entry failed: {result.error}")
            return _fail(f"Entry failed: {result.error}")
        
        logger.info("Entry order placed via V2",
                   symbol=futures_symbol,
                   client_order_id=action.client_order_id,
                   exchange_order_id=result.exchange_order_id)
        
        # 10. Persist position state
        if self.position_persistence:
            self.position_persistence.save_position(position)
            self.position_persistence.log_action(
                position.position_id,
                "entry_submitted",
                {
                    "signal_type": signal.signal_type.value,
                    "entry_price": str(mark_price),
                    "stop_price": str(position.initial_stop_price),
                    "size": str(position_size)
                }
            )
        
        # Note: Stop placement happens in the gateway's order event handler
        # when the entry fill is confirmed
        
        # Send alert for new position
        try:
            from src.monitoring.alerting import send_alert_sync
            send_alert_sync(
                "NEW_POSITION",
                f"New {signal.signal_type.value} position\n"
                f"Symbol: {signal.symbol}\n"
                f"Size: {position_size} @ ${mark_price}\n"
                f"Stop: ${position.initial_stop_price}",
            )
        except Exception:
            pass  # Alert failure must never block trading
        
        return _ok()

    async def _update_candles(self, symbol: str):
        """Update local candle caches from acquisition with throttling."""
        await self.candle_manager.update_candles(symbol)

    async def _run_auction_allocation(self, raw_positions: List[Dict]):
        """
        Run auction-based portfolio allocation if auction mode is enabled.
        
        Collects all open positions and candidate signals, runs the auction,
        and executes the allocation plan.
        """
        logger.debug("Auction: _run_auction_allocation called", signals_count=len(self.auction_signals_this_tick))
        try:
            from src.portfolio.auction_allocator import (
                position_to_open_metadata,
                create_candidate_signal,
            )
            from src.execution.equity import calculate_effective_equity
            from src.domain.models import Signal, SignalType
            
            # Get account state
            balance = await self.client.get_futures_balance()
            base = getattr(self.config.exchange, "base_currency", "USD")
            equity, available_margin, _ = await calculate_effective_equity(
                balance, base_currency=base, kraken_client=self.client
            )
            
            # Convert raw positions to Position objects and metadata
            # CRITICAL: Build spot-to-futures mapping for symbol matching
            spot_to_futures_map = {}
            for pos_data in raw_positions:
                futures_sym = pos_data.get('symbol')
                if futures_sym:
                    # Try to reverse-map futures symbol to spot symbol
                    # This is needed because auction uses spot symbols but positions use futures symbols
                    for spot_sym in self.auction_signals_this_tick:
                        mapped_futures = self.futures_adapter.map_spot_to_futures(
                            spot_sym[0].symbol,  # signal.symbol
                            futures_tickers=self.latest_futures_tickers
                        )
                        if mapped_futures == futures_sym:
                            spot_to_futures_map[spot_sym[0].symbol] = futures_sym
                            break
            
            open_positions_meta = []
            for pos_data in raw_positions:
                if pos_data.get('size', 0) == 0:
                    continue
                try:
                    pos = self._convert_to_position(pos_data)
                    futures_symbol = pos.symbol
                    
                    # CRITICAL FIX: Merge protection status from database
                    # _convert_to_position creates a fresh object with is_protected=False
                    # We need to copy over the reconciled protection status from DB
                    try:
                        db_pos = await asyncio.to_thread(get_active_position, futures_symbol)
                        if db_pos:
                            pos.is_protected = db_pos.is_protected
                            pos.protection_reason = db_pos.protection_reason
                            pos.stop_loss_order_id = db_pos.stop_loss_order_id
                            pos.initial_stop_price = db_pos.initial_stop_price
                            if hasattr(db_pos, 'tp_order_ids'):
                                pos.tp_order_ids = db_pos.tp_order_ids
                    except Exception as e:
                        logger.warning("Failed to fetch DB position for protection merge", symbol=futures_symbol, error=str(e))
                    
                    # Check if protective orders are live
                    is_protective_live = (
                        pos.stop_loss_order_id is not None or
                        (hasattr(pos, 'tp_order_ids') and pos.tp_order_ids)
                    )
                    meta = position_to_open_metadata(
                        position=pos,
                        account_equity=equity,
                        is_protective_orders_live=is_protective_live,
                    )
                    # CRITICAL: Store spot symbol for matching against candidate signals
                    # The position uses futures symbol (e.g., "PF_PROMPTUSD"), but candidates use spot (e.g., "PROMPT/USD")
                    # Find matching spot symbol by reverse-mapping
                    spot_symbol = None
                    for spot_sym, fut_sym in spot_to_futures_map.items():
                        if fut_sym == futures_symbol:
                            spot_symbol = spot_sym
                            break
                    # If not found in signals, try to derive from futures symbol
                    if not spot_symbol:
                        # Extract base from futures symbol (e.g., "PF_PROMPTUSD" -> "PROMPT")
                        base = futures_symbol.replace('PF_', '').replace('USD', '').replace('PI_', '').replace('FI_', '')
                        if base:
                            spot_symbol = f"{base}/USD"
                    meta.spot_symbol = spot_symbol
                    open_positions_meta.append(meta)
                except Exception as e:
                    logger.error("Failed to convert position for auction", symbol=pos_data.get('symbol'), error=str(e))
            
            # Log signals collected count
            signals_count = len(self.auction_signals_this_tick)
            logger.info(
                "Auction: Collecting candidate signals",
                signals_count=signals_count,
            )
            
            # Refresh instrument spec registry so auction only plans executable trades
            try:
                await self.instrument_spec_registry.refresh()
            except Exception as e:
                logger.warning("Instrument spec refresh failed before auction", error=str(e))
            
            # Calculate auction budget once (equity * max_margin_util)
            auction_budget_margin = equity * Decimal(str(self.config.risk.auction_max_margin_util))
            
            candidate_signals = []
            signal_to_candidate = {}
            requested_leverage = int(getattr(self.config.risk, "target_leverage", 7) or 7)
            
            # Import symbol cooldown checker
            from src.risk.symbol_cooldown import check_symbol_cooldown
            
            for signal, spot_price, mark_price in self.auction_signals_this_tick:
                try:
                    # Check symbol-level cooldown (repeated losses)
                    if getattr(self.config.strategy, 'symbol_loss_cooldown_enabled', True):
                        is_on_cooldown, cooldown_reason = check_symbol_cooldown(
                            symbol=signal.symbol,
                            lookback_hours=getattr(self.config.strategy, 'symbol_loss_lookback_hours', 24),
                            loss_threshold=getattr(self.config.strategy, 'symbol_loss_threshold', 3),
                            cooldown_hours=getattr(self.config.strategy, 'symbol_loss_cooldown_hours', 12),
                            min_pnl_pct=getattr(self.config.strategy, 'symbol_loss_min_pnl_pct', -0.5),
                        )
                        if is_on_cooldown:
                            logger.warning(
                                "AUCTION_OPEN_REJECTED",
                                symbol=signal.symbol,
                                reason="SYMBOL_COOLDOWN",
                                details=cooldown_reason,
                            )
                            continue
                    
                    futures_symbol = self.futures_adapter.map_spot_to_futures(
                        signal.symbol, futures_tickers=self.latest_futures_tickers
                    )
                    spec = self.instrument_spec_registry.get_spec(futures_symbol)
                    if not spec:
                        logger.warning(
                            "AUCTION_OPEN_REJECTED",
                            symbol=signal.symbol,
                            reason="NO_SPEC",
                            requested_leverage=requested_leverage,
                            spec_summary=None,
                        )
                        continue
                    # Get symbol tier for tier-specific sizing
                    symbol_tier = self.market_discovery.get_symbol_tier(signal.symbol) if self.market_discovery else "C"
                    if symbol_tier != "A":
                        static_tier = self._get_static_tier(signal.symbol)
                        if static_tier == "A":
                            logger.warning(
                                "Tier downgrade detected",
                                symbol=signal.symbol,
                                static_tier=static_tier,
                                dynamic_tier=symbol_tier,
                                reason="Dynamic classification is authoritative",
                            )
                    
                    decision = self.risk_manager.validate_trade(
                        signal, equity, spot_price, mark_price,
                        available_margin=auction_budget_margin,
                        symbol_tier=symbol_tier,
                    )
                    if decision.position_notional > 0 and decision.margin_required > 0:
                        stop_distance = abs(signal.entry_price - signal.stop_loss) / signal.entry_price if signal.stop_loss else Decimal("0")
                        risk_R = decision.position_notional * stop_distance if stop_distance > 0 else Decimal("0")
                        candidate = create_candidate_signal(
                            signal=signal,
                            required_margin=decision.margin_required,
                            risk_R=risk_R,
                            position_notional=decision.position_notional,
                        )
                        candidate_signals.append(candidate)
                        signal_to_candidate[signal.symbol] = candidate
                        logger.info(
                            "Auction candidate created",
                            symbol=signal.symbol,
                            score=signal.score,
                            notional=str(decision.position_notional),
                            margin=str(decision.margin_required),
                            approved=decision.approved,
                        )
                        if not decision.approved:
                            logger.debug(
                                "Candidate included in auction despite rejection (auction can optimize)",
                                symbol=signal.symbol,
                                score=signal.score,
                                rejection_reasons=decision.rejection_reasons,
                            )
                    else:
                        logger.warning(
                            "Signal not added to auction candidates",
                            symbol=signal.symbol,
                            score=signal.score,
                            position_notional=str(decision.position_notional),
                            margin_required=str(decision.margin_required),
                            approved=decision.approved,
                            rejection_reasons=decision.rejection_reasons,
                        )
                except Exception as e:
                    logger.error("Failed to create candidate signal for auction", symbol=signal.symbol, error=str(e))
            
            # Use auction budget margin for portfolio state
            # This represents the total margin the auction is allowed to deploy
            portfolio_state = {
                "available_margin": auction_budget_margin,  # Use auction budget, not current available
                "account_equity": equity,
            }
            
            # Run auction
            plan = self.auction_allocator.allocate(
                open_positions=open_positions_meta,
                candidate_signals=candidate_signals,
                portfolio_state=portfolio_state,
            )
            
            # Log auction plan summary (first positional is event for structlog; do not also pass event=)
            logger.info(
                "Auction plan generated",
                closes_count=len(plan.closes),
                closes_symbols=plan.closes,
                opens_count=len(plan.opens),
                opens_symbols=[s.symbol for s in plan.opens],
                reasons=plan.reasons,
            )
            
            # Execute closes first
            for symbol in plan.closes:
                try:
                    await self.client.close_position(symbol)
                    logger.info("Auction: Closed position", symbol=symbol)
                except Exception as e:
                    logger.error("Auction: Failed to close position", symbol=symbol, error=str(e))
            
            # CRITICAL: Refresh margin after closes
            balance_after_closes = await self.client.get_futures_balance()
            equity_after, refreshed_available_margin, _ = await calculate_effective_equity(
                balance_after_closes, base_currency=base, kraken_client=self.client
            )
            logger.info(
                "Auction: Margin refreshed after closes",
                equity=str(equity_after),
                refreshed_available_margin=str(refreshed_available_margin),
                previous_available_margin=str(available_margin),
            )
            
            # Execute opens from auction plan.
            # Dedupe by symbol so we never open the same coin twice in one plan
            # (defense in depth if allocator ever emits duplicates).
            seen_opens: set[str] = set()
            opens_executed = 0
            opens_failed = 0
            rejection_counts: Dict[str, int] = {}  # reason -> count for summary
            for signal in plan.opens:
                if signal.symbol in seen_opens:
                    logger.warning(
                        "Auction: Skipping duplicate open for same symbol",
                        symbol=signal.symbol,
                    )
                    continue
                seen_opens.add(signal.symbol)
                try:
                    # Hard entry blocklist: do not open on blocked symbols (e.g. USDT/USD).
                    spot_key = (signal.symbol or "").strip().upper().split(":")[0]
                    base = spot_key.split("/")[0].strip() if "/" in spot_key else spot_key
                    blocked_spot = set(
                        s.strip().upper().split(":")[0]
                        for s in getattr(self.config.execution, "entry_blocklist_spot_symbols", []) or []
                    )
                    blocked_base = set(
                        b.strip().upper()
                        for b in getattr(self.config.execution, "entry_blocklist_bases", []) or []
                    )
                    if (spot_key and spot_key in blocked_spot) or (base and base in blocked_base):
                        opens_failed += 1
                        reason = "ENTRY_BLOCKED"
                        rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
                        logger.warning(
                            "Auction: Open blocked by entry blocklist",
                            symbol=signal.symbol,
                            reason=("blocked_spot_symbol" if spot_key in blocked_spot else "blocked_base"),
                        )
                        continue

                    # Find corresponding price data and candidate
                    spot_price = None
                    mark_price = None
                    candidate = signal_to_candidate.get(signal.symbol)
                    
                    for sig, sp, mp in self.auction_signals_this_tick:
                        if sig.symbol == signal.symbol:
                            spot_price = sp
                            mark_price = mp
                            break
                    
                    if spot_price and mark_price and candidate:
                        # Use auction overrides: refreshed margin, pre-computed notional, skip margin check
                        logger.info(
                            "Auction: Executing open with overrides",
                            symbol=signal.symbol,
                            notional_override=str(candidate.position_notional),
                            refreshed_margin=str(refreshed_available_margin),
                        )
                        result = await self._handle_signal(
                            signal, 
                            spot_price, 
                            mark_price,
                        )
                        if result.get("order_placed", False):
                            opens_executed += 1
                            logger.info(
                                "Auction: Opened position",
                                symbol=signal.symbol,
                                reason=result.get("reason", "unknown")
                            )
                        else:
                            opens_failed += 1
                            rejection_reasons = result.get("rejection_reasons", [])
                            for r in rejection_reasons:
                                rejection_counts[r] = rejection_counts.get(r, 0) + 1
                            logger.warning(
                                "Auction: Open rejected/failed",
                                symbol=signal.symbol,
                                reason=result.get("reason", "unknown"),
                                rejection_reasons=rejection_reasons
                            )
                    else:
                        opens_failed += 1
                        missing = []
                        if not spot_price or not mark_price:
                            missing.append("price_data")
                        if not candidate:
                            missing.append("candidate")
                        reason = "missing_data:" + ",".join(missing)
                        rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
                        logger.warning(
                            "Auction: Missing data for signal",
                            symbol=signal.symbol,
                            missing=missing,
                        )
                except ValueError as e:
                    opens_failed += 1
                    err_str = str(e)
                    if "SIZE_BELOW_MIN" in err_str:
                        reason = "SIZE_BELOW_MIN"
                    elif "SIZE_STEP_ROUND_TO_ZERO" in err_str:
                        reason = "SIZE_STEP_ROUND_TO_ZERO"
                    elif "Size validation failed" in err_str:
                        reason = err_str.split(":")[-1].strip() if ":" in err_str else "SIZE_VALIDATION_FAILED"
                    else:
                        reason = "ValueError"
                    rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
                    logger.error(
                        "Auction: Failed to open position (size/validation)",
                        symbol=signal.symbol,
                        error=err_str,
                    )
                except Exception as e:
                    opens_failed += 1
                    reason = type(e).__name__
                    rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
                    logger.error(
                        "Auction: Failed to open position",
                        symbol=signal.symbol,
                        error=str(e),
                        exc_info=True,
                    )
            
            logger.info(
                "Auction allocation executed",
                closes=len(plan.closes),
                opens_planned=len(plan.opens),
                opens_executed=opens_executed,
                opens_failed=opens_failed,
                rejection_counts=rejection_counts if rejection_counts else None,
                reasons=plan.reasons,
            )
            if opens_executed > 0 or len(plan.closes) > 0:
                self._reconcile_requested = True
            
        except Exception as e:
            logger.error("Failed to run auction allocation", error=str(e))
    
    async def _save_trade_history(self, position: Position, exit_price: Decimal, exit_reason: str):
        """
        Save closed position to trade history.
        
        Args:
            position: The position being closed
            exit_price: Exit price
            exit_reason: Reason for exit (stop_loss, take_profit, manual, etc.)
        """
        try:
            from src.domain.models import Trade
            from src.storage.repository import save_trade
            from datetime import datetime, timezone
            import uuid
            
            # Calculate holding period
            now = datetime.now(timezone.utc)
            holding_hours = (now - position.opened_at).total_seconds() / 3600
            
            # Calculate PnL
            if position.side == Side.LONG:
                gross_pnl = (exit_price - position.entry_price) * position.size
            else:  # SHORT
                gross_pnl = (position.entry_price - exit_price) * position.size
            
            # Estimate fees (simplified - should use actual fees if available)
            # Maker: 0.02%, Taker: 0.05% (Kraken Futures)
            entry_fee = position.size_notional * Decimal("0.0002")  # Assume maker
            exit_fee = position.size_notional * Decimal("0.0002")
            fees = entry_fee + exit_fee
            
            # Estimate funding (simplified - should use actual funding if available)
            # Average funding rate ~0.01% per 8 hours
            funding_periods = holding_hours / 8
            funding = position.size_notional * Decimal("0.0001") * Decimal(str(funding_periods))
            
            net_pnl = gross_pnl - fees - funding
            
            # Create Trade object
            trade = Trade(
                trade_id=str(uuid.uuid4()),
                symbol=position.symbol,
                side=position.side,
                entry_price=position.entry_price,
                exit_price=exit_price,
                size_notional=position.size_notional,
                leverage=position.leverage,
                gross_pnl=gross_pnl,
                fees=fees,
                funding=funding,
                net_pnl=net_pnl,
                entered_at=position.opened_at,
                exited_at=now,
                holding_period_hours=Decimal(str(holding_hours)),
                exit_reason=exit_reason
            )
            
            # Save to database
            await asyncio.to_thread(save_trade, trade)
            
            # Update daily P&L tracking in risk manager
            try:
                setup_type = getattr(position, 'setup_type', None)
                balance = await self.client.get_futures_balance()
                base = getattr(self.config.exchange, "base_currency", "USD")
                equity_now, _, _ = await calculate_effective_equity(
                    balance, base_currency=base, kraken_client=self.client
                )
                self.risk_manager.record_trade_result(net_pnl, equity_now, setup_type)
                
                # Alert if daily loss limit approached or exceeded
                daily_loss_pct = abs(self.risk_manager.daily_pnl) / self.risk_manager.daily_start_equity \
                    if self.risk_manager.daily_start_equity > 0 and self.risk_manager.daily_pnl < 0 \
                    else Decimal("0")
                if daily_loss_pct > Decimal(str(self.config.risk.daily_loss_limit_pct * 0.7)):
                    from src.monitoring.alerting import send_alert
                    limit_pct = self.config.risk.daily_loss_limit_pct * 100
                    await send_alert(
                        "DAILY_LOSS_WARNING",
                        f"Daily loss at {daily_loss_pct:.1%} of equity\n"
                        f"Limit: {limit_pct:.0f}%\n"
                        f"Daily P&L: ${self.risk_manager.daily_pnl:.2f}",
                        urgent=daily_loss_pct > Decimal(str(self.config.risk.daily_loss_limit_pct)),
                    )
            except Exception as e:
                logger.warning("Failed to update daily P&L tracking", error=str(e))
            
            logger.info(
                "Trade saved to history",
                symbol=position.symbol,
                side=position.side.value,
                entry_price=str(position.entry_price),
                exit_price=str(exit_price),
                net_pnl=str(net_pnl),
                exit_reason=exit_reason,
                holding_hours=f"{holding_hours:.2f}"
            )
            
            # Send close alert via Telegram
            try:
                from src.monitoring.alerting import send_alert
                pnl_sign = "+" if net_pnl >= 0 else ""
                pnl_emoji = "✅" if net_pnl >= 0 else "❌"
                await send_alert(
                    "POSITION_CLOSED",
                    f"{pnl_emoji} Position closed: {position.symbol}\n"
                    f"Side: {position.side.value.upper()}\n"
                    f"Entry: ${position.entry_price} → Exit: ${exit_price}\n"
                    f"P&L: {pnl_sign}${net_pnl:.2f}\n"
                    f"Reason: {exit_reason}\n"
                    f"Duration: {holding_hours:.1f}h",
                )
            except Exception:
                pass  # Alert failure must never block trade history
            
        except Exception as e:
            logger.error("Failed to save trade history", symbol=position.symbol, error=str(e))


    # -----------------------------------------------------------------------
    # Protection operations (delegated to src.live.protection_ops)
    # -----------------------------------------------------------------------

    async def _reconcile_protective_orders(self, raw_positions: List[Dict], current_prices: Dict[str, Decimal]):
        """TP Backfill / Reconciliation -- delegates to protection_ops module."""
        from src.live.protection_ops import reconcile_protective_orders
        await reconcile_protective_orders(self, raw_positions, current_prices)

    async def _reconcile_stop_loss_order_ids(self, raw_positions: List[Dict]):
        """SL order ID reconciliation -- delegates to protection_ops module."""
        from src.live.protection_ops import reconcile_stop_loss_order_ids
        await reconcile_stop_loss_order_ids(self, raw_positions)

    async def _place_missing_stops_for_unprotected(self, raw_positions: List[Dict], max_per_tick: int = 3) -> None:
        """Place missing stops -- delegates to protection_ops module."""
        from src.live.protection_ops import place_missing_stops_for_unprotected
        await place_missing_stops_for_unprotected(self, raw_positions, max_per_tick)

    async def _should_skip_tp_backfill(
        self, symbol: str, pos_data: Dict, db_pos: Position, current_price: Decimal,
        is_protected: Optional[bool] = None
    ) -> bool:
        """Safety checks -- delegates to protection_ops module."""
        from src.live.protection_ops import should_skip_tp_backfill
        return await should_skip_tp_backfill(self, symbol, pos_data, db_pos, current_price, is_protected)

    def _needs_tp_backfill(self, db_pos: Position, symbol_orders: List[Dict]) -> bool:
        """TP coverage check -- delegates to protection_ops module."""
        from src.live.protection_ops import needs_tp_backfill
        return needs_tp_backfill(self, db_pos, symbol_orders)

    async def _compute_tp_plan(
        self, symbol: str, pos_data: Dict, db_pos: Position, current_price: Decimal
    ) -> Optional[List[Decimal]]:
        """TP plan computation -- delegates to protection_ops module."""
        from src.live.protection_ops import compute_tp_plan
        return await compute_tp_plan(self, symbol, pos_data, db_pos, current_price)

    async def _cleanup_orphan_reduce_only_orders(self, raw_positions: List[Dict]):
        """Orphan order cleanup -- delegates to protection_ops module."""
        from src.live.protection_ops import cleanup_orphan_reduce_only_orders
        await cleanup_orphan_reduce_only_orders(self, raw_positions)

    async def _place_tp_backfill(
        self, symbol: str, pos_data: Dict, db_pos: Position, tp_plan: List[Decimal],
        symbol_orders: List[Dict], current_price: Decimal
    ):
        """TP order placement -- delegates to protection_ops module."""
        from src.live.protection_ops import place_tp_backfill
        await place_tp_backfill(self, symbol, pos_data, db_pos, tp_plan, symbol_orders, current_price)
