import asyncio
import os
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import List, Dict, Optional, Any, TYPE_CHECKING

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

if TYPE_CHECKING:
    from src.execution.position_manager import (
        ActionType as LegacyActionType,
        ManagementAction as LegacyManagementAction,
        PositionManager as LegacyPositionManager,
    )

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
        
        self.smc_engine = SMCEngine(config.strategy)
        self.risk_manager = RiskManager(config.risk, liquidity_filters=config.liquidity_filters)
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
        self.position_manager: Optional["LegacyPositionManager"] = None
        self.kill_switch = KillSwitch(self.client)
        self.market_discovery = MarketDiscoveryService(self.client, config)
        self._last_discovery_error_log_time: Optional[datetime] = None

        # Auction mode allocator (if enabled)
        self.auction_allocator = None
        self.auction_signals_this_tick = []  # Collect signals for auction mode
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
        else:
            # Legacy mode - use old managed_positions dict
            self.position_registry = None
            self.position_manager_v2 = None
            self.execution_gateway = None
            self.position_persistence = None
            self._protection_monitor = None
            self._protection_task = None
            self._order_poll_task = None
            # Legacy manager is only constructed when V2 is disabled
            from src.execution.position_manager import PositionManager as LegacyPositionManagerImpl
            self.position_manager = LegacyPositionManagerImpl()
        
        # Legacy in-memory state (only in legacy mode)
        self.managed_positions: Optional[Dict[str, Position]] = {} if not self.use_state_machine_v2 else None
        # Hard invariant: V2 must not have any legacy authority objects.
        if self.use_state_machine_v2:
            assert self.position_manager is None, "V2 mode must not initialize legacy PositionManager"
            assert self.managed_positions is None, "V2 mode must not initialize managed_positions"
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
        self.markets = config.exchange.spot_markets
        if config.assets.mode == "whitelist":
             self.markets = config.assets.whitelist
        elif config.coin_universe and config.coin_universe.enabled:
             # Expand from Tiers
             expanded = []
             for tier, coins in config.coin_universe.liquidity_tiers.items():
                 expanded.extend(coins)
             # Deduplicate and exclude disallowed bases (fiat + stablecoin).
             self.markets = [s for s in list(set(expanded)) if not has_disallowed_base(s)]
             logger.info("Coin Universe Enabled", markets=self.markets)
             
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
        Look up symbol in config coin_universe.liquidity_tiers (static lists).
        Used only for tier mismatch warning; dynamic classification is authoritative.
        Returns "A", "B", "C", or None if not in any static tier.
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
            
            # Trim to Kraken-supported only; log any symbols we drop
            prev_symbols = set(self._market_symbols())
            supported = set(mapping.keys())
            dropped = prev_symbols - supported
            for sym in sorted(dropped):
                logger.warning("SYMBOL REMOVED (unsupported on Kraken)", symbol=sym)
            
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

            # 2.7 One-time startup reconciliation (ghost/zombie positions, adopt or force_close)
            if (not self.use_state_machine_v2) and not (
                self.config.system.dry_run and not self.client.has_valid_futures_credentials()
            ):
                _recon_cfg = getattr(self.config, "reconciliation", None)
                if _recon_cfg and getattr(_recon_cfg, "reconcile_enabled", True):
                    try:
                        logger.info("Running startup reconciliation (legacy)...")
                        recon = self._build_reconciler()
                        await recon.reconcile_all()
                        self.last_recon_time = datetime.now(timezone.utc)
                    except Exception as ex:
                        logger.warning("Startup reconciliation failed", error=str(ex))

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

                if self.kill_switch.is_active():
                    logger.critical("Kill switch active - pausing loop")
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

                # Dynamic sleep to align with 1m intervals
                elapsed = (now - loop_start).total_seconds()
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
        """
        while self.active:
            await asyncio.sleep(interval_seconds)
            if not self.active:
                break
            if not getattr(self, "_protection_monitor", None):
                continue
            try:
                results = await self._protection_monitor.check_all_positions()
                naked = [s for s, ok in results.items() if not ok]
                if naked:
                    logger.critical("NAKED_POSITIONS_DETECTED", naked_symbols=naked, details=results)
                    is_prod_live = (os.getenv("ENVIRONMENT", "").strip().lower() == "prod") and (not self.config.system.dry_run)
                    if is_prod_live:
                        await self.kill_switch.activate(KillSwitchReason.RECONCILIATION_FAILURE, emergency=True)
                        return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Protection check loop failed", error=str(e), error_type=type(e).__name__)

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
        
        Checks both DB positions and managed_positions for unprotected positions.
        Emits alerts and optionally pauses trading.
        """
        from src.storage.repository import get_active_positions, async_record_event
        
        unprotected = []

        tracked_symbols: set[str] = set()

        # V2: Check registry state (authoritative)
        if self.use_state_machine_v2 and self.position_registry:
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
        else:
            # Legacy: Check managed_positions (in-memory state)
            assert self.managed_positions is not None, "managed_positions must exist in legacy mode"
            for symbol, pos in self.managed_positions.items():
                tracked_symbols.add(symbol)
                if not pos.is_protected or not pos.initial_stop_price or not pos.stop_loss_order_id:
                    unprotected.append({
                        'symbol': symbol,
                        'source': 'managed_positions',
                        'reason': pos.protection_reason or 'UNKNOWN',
                        'has_sl_price': pos.initial_stop_price is not None,
                        'has_sl_order': pos.stop_loss_order_id is not None,
                        'is_protected': pos.is_protected
                    })

        # Legacy-only: also check DB positions (for positions not yet in managed_positions).
        # In V2 mode the registry + exchange are authoritative; DB is for dashboard/history only.
        db_positions = []
        if not self.use_state_machine_v2:
            db_positions = await asyncio.to_thread(get_active_positions)
            for pos in db_positions:
                if pos.symbol not in tracked_symbols:
                    if not pos.is_protected or not pos.initial_stop_price or not pos.stop_loss_order_id:
                        unprotected.append({
                            'symbol': pos.symbol,
                            'source': 'database',
                            'reason': pos.protection_reason or 'UNKNOWN',
                            'has_sl_price': pos.initial_stop_price is not None,
                            'has_sl_order': pos.stop_loss_order_id is not None,
                            'is_protected': pos.is_protected
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
        # Semaphore to control concurrency (e.g. 20 coins at a time for candle fetching)
        sem = asyncio.Semaphore(20)
        
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
                    
                    # Position Management
                    position_data = map_positions.get(futures_symbol)
                    managed_pos = None  # Only set in legacy mode; V2 uses state machine
                    if position_data:
                        # Management Logic
                        symbol = position_data['symbol']

                        # V2: State machine is the only authority.
                        if self.use_state_machine_v2 and self.position_manager_v2 and self.execution_gateway:
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
                        else:
                            # Legacy: in-memory managed_positions + legacy manager actions
                            assert self.managed_positions is not None, "managed_positions must exist in legacy mode"
                            assert self.position_manager is not None, "position_manager must exist in legacy mode"
                            # Ensure tracked
                            if symbol not in self.managed_positions:
                                # Load from DB first to preserve initial_stop_price
                                from src.storage.repository import get_active_position
                                db_pos = await asyncio.to_thread(get_active_position, symbol)
                                orders_for_symbol = orders_by_symbol.get(normalize_symbol_for_position_match(symbol), [])
                                
                                self.managed_positions[symbol] = self._init_managed_position(
                                    position_data,
                                    mark_price,
                                    db_pos=db_pos,
                                    orders_for_symbol=orders_for_symbol
                                )
                            
                            managed_pos = self.managed_positions[symbol]
                            old_size = managed_pos.size
                            managed_pos.current_mark_price = mark_price
                            managed_pos.unrealized_pnl = Decimal(str(position_data.get('unrealized_pnl', 0))) # Key corrected from raw API
                            managed_pos.size = Decimal(str(position_data['size']))
                            
                            # Update position in DB when size/margin changes (after reconciliation)
                            if old_size != managed_pos.size or managed_pos.margin_used != Decimal(str(position_data.get('margin_used', 0))):
                                managed_pos.margin_used = Decimal(str(position_data.get('margin_used', 0)))
                                from src.storage.repository import save_position
                                await asyncio.to_thread(save_position, managed_pos)
                                logger.debug("Position updated after reconciliation", symbol=symbol, size=str(managed_pos.size))
                            
                            actions = self.position_manager.evaluate(managed_pos, mark_price)
                            if actions:
                                await self._execute_management_actions(symbol, actions, managed_pos)
                            
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
                    
                    # V4: Dynamic Exits (Abandon Ship & Time-Based)
                    # We check this AFTER signal generation because we need the fresh Bias
                    if position_data and managed_pos:
                         await self._check_dynamic_exits(symbol, managed_pos, signal, candle_count)

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
                                logger.info(f"SMC Analysis {spot_symbol}: NO_SIGNAL -> {signal.reasoning}")
                            
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
            
        except Exception as e:
            logger.error("Failed to sync account state", error=str(e))
    
    async def _validate_position_protection_legacy(self):
        """CRITICAL: Ensure all open positions have stop loss orders (legacy method - replaced by _validate_position_protection)."""
        try:
            if self.managed_positions is None:
                logger.debug("Legacy protection validation skipped (V2 enabled)")
                return
            all_positions = await self.client.get_all_futures_positions()
            
            for pos in all_positions:
                symbol = pos['symbol']
                
                # Check if position has protective orders in managed_positions
                if symbol in self.managed_positions:
                    managed_pos = self.managed_positions[symbol]
                    
                    # CRITICAL CHECK: Stop loss must be set
                    if not managed_pos.initial_stop_price:
                        logger.critical(
                            "UNPROTECTED POSITION: no stop loss",
                            symbol=symbol,
                            size=str(pos["size"]),
                            entry=str(pos["entry_price"]),
                            unrealized_pnl=str(pos.get("unrealized_pnl", 0)),
                        )
                        try:
                            from src.monitoring.alerts import get_alert_system
                            get_alert_system().send_alert(
                                "critical",
                                "Unprotected position – no stop loss",
                                f"{symbol}: size={pos['size']} entry={pos['entry_price']}. Place stop manually or restart with protection.",
                                metadata={"symbol": symbol, "size": str(pos["size"])},
                            )
                        except Exception as ex:
                            logger.warning("Failed to send unprotected-position alert", error=str(ex))
                else:
                    logger.critical(
                        "UNMANAGED POSITION: exchange position not tracked",
                        symbol=symbol,
                        size=str(pos["size"]),
                        entry=str(pos["entry_price"]),
                    )
                    try:
                        from src.monitoring.alerts import get_alert_system
                        get_alert_system().send_alert(
                            "critical",
                            "Unmanaged position on exchange",
                            f"{symbol} exists on exchange but not in managed_positions. Review and sync.",
                            metadata={"symbol": symbol},
                        )
                    except Exception as ex:
                        logger.warning("Failed to send unmanaged-position alert", error=str(ex))
        except Exception as e:
            logger.error("Failed to validate position protection", error=str(e))
    
    async def _handle_signal(
        self, 
        signal: Signal, 
        spot_price: Decimal, 
        mark_price: Decimal,
        available_margin_override: Optional[Decimal] = None,
        notional_override: Optional[Decimal] = None,
        skip_margin_check: bool = False,
    ) -> dict:
        """
        Process signal through risk and executor.
        
        Args:
            signal: Trading signal
            spot_price: Current spot price
            mark_price: Current futures mark price
            available_margin_override: Override available margin (for auction execution)
            notional_override: Override position notional (for auction execution)
            skip_margin_check: Skip margin validation (auction already validated)
        
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
        
        # ========== V2 PATH: Position State Machine ==========
        if self.use_state_machine_v2 and self.position_manager_v2:
            return await self._handle_signal_v2(signal, spot_price, mark_price)
        
        # ========== LEGACY PATH (below) ==========
        # 1. Fetch Account Equity and Available Margin (unless overridden)
        if available_margin_override is None:
            balance = await self.client.get_futures_balance()
            base = getattr(self.config.exchange, "base_currency", "USD")
            equity, available_margin, _ = await calculate_effective_equity(
                balance, base_currency=base, kraken_client=self.client
            )
        else:
            # Use override for margin, but still need equity for risk calculations
            balance = await self.client.get_futures_balance()
            base = getattr(self.config.exchange, "base_currency", "USD")
            equity, _, _ = await calculate_effective_equity(
                balance, base_currency=base, kraken_client=self.client
            )
            available_margin = available_margin_override
        
        if equity <= 0:
            logger.error("Insufficient equity for trading", equity=str(equity))
            return {
                "order_placed": False,
                "reason": "Insufficient equity",
                "rejection_reasons": ["equity <= 0"]
            }

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
            notional_override=notional_override,
            skip_margin_check=skip_margin_check,
            symbol_tier=symbol_tier,
        )
        
        if not decision.approved:
            logger.warning("Trade rejected by Risk Manager", reasons=decision.rejection_reasons)
            return {
                "order_placed": False,
                "reason": f"Risk Manager rejected: {', '.join(decision.rejection_reasons)}",
                "rejection_reasons": decision.rejection_reasons
            }
        
        # Use notional_override if provided (auction execution)
        final_notional = notional_override if notional_override is not None else decision.position_notional
        
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
                    if self.managed_positions is not None and decision.close_symbol in self.managed_positions:
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
                    return {
                        "order_placed": False,
                        "reason": f"Failed to close existing position for replacement: {str(e)}",
                        "rejection_reasons": ["replacement_close_failed"]
                    }

            # Execute Entry
            order_intent = self.execution_engine.generate_entry_plan( # Reverted to original method name and args
                signal, 
                final_notional,
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
            size_notional=final_notional,
            leverage=decision.leverage,
            entry_price_spot=signal.entry_price,
            stop_loss_spot=signal.stop_loss,
            take_profit_spot=signal.take_profit,
            entry_price_futures=order_intent['metadata']['fut_entry'],
            stop_loss_futures=order_intent['metadata']['fut_sl'],
            take_profit_futures=order_intent['take_profits'][0]['price'] if order_intent['take_profits'] else None
        )

        # 4. Execute (pass actual positions for pyramiding guard)
        try:
            entry_order = await self.executor.execute_signal(
                intent_model,
                mark_price,
                self.risk_manager.current_positions
            )
        except ValueError as e:
            err_str = str(e)
            if "SIZE_BELOW_MIN" in err_str:
                reason = "SIZE_BELOW_MIN"
            elif "SIZE_STEP_ROUND_TO_ZERO" in err_str:
                reason = "SIZE_STEP_ROUND_TO_ZERO"
            elif "Size validation failed" in err_str and ":" in err_str:
                reason = err_str.split(":")[-1].strip()
            else:
                reason = "SIZE_VALIDATION_FAILED"
            logger.warning("Entry rejected (size/validation)", symbol=signal.symbol, reason=reason, error=err_str[:200])
            return {
                "order_placed": False,
                "reason": err_str[:200],
                "rejection_reasons": [reason],
            }

        if entry_order:
             logger.info("Entry order placed", order_id=entry_order.order_id)
             
             # 5. Place Protective Orders (Full TP Ladder) - After Entry Fill
             # Split SL and TP placement so failures don't mask each other
             # Use latest futures tickers for optimal mapping
             futures_symbol = self.futures_adapter.map_spot_to_futures(
                 signal.symbol,
                 futures_tickers=self.latest_futures_tickers
             )
             tps = [o["price"] for o in order_intent.get("take_profits", []) if o.get("price")]
             
             # Extract TP prices for position state
             tp1 = tps[0] if len(tps) > 0 else None
             tp2 = tps[1] if len(tps) > 1 else None
             
             # Use update_protective_orders to place full TP ladder (TP1/TP2/TP3)
             # This replaces the single-TP call and places all TPs at once
             new_sl_id, new_tp_ids = await self.executor.update_protective_orders(
                 symbol=entry_order.symbol,
                 side=entry_order.side,
                 current_sl_id=None,  # New position, no existing SL
                 new_sl_price=intent_model.stop_loss_futures,
                 current_tp_ids=[],  # New position, no existing TPs
                 new_tp_prices=tps,  # Full ladder: TP1, TP2, TP3
                 position_size_notional=intent_model.size_notional,  # Pass actual position size
             )
             
             if new_sl_id:
                 logger.info("Protective SL placed", order_id=new_sl_id)
             else:
                 logger.critical("FAILED TO PLACE STOP LOSS", symbol=signal.symbol)
             
             if new_tp_ids:
                 logger.info("TP ladder placed", tp_count=len(new_tp_ids), tp_ids=new_tp_ids)
             else:
                 logger.warning("Failed to place TP ladder", symbol=signal.symbol)
             
             # Initialize Active Trade Management State
             # We optimisticly track the position with its immutable intents
             # Compute protection status
             is_protected = (intent_model.stop_loss_futures is not None and new_sl_id is not None)
             protection_reason = None if is_protected else ("SL_ORDER_MISSING" if intent_model.stop_loss_futures and not new_sl_id else "NO_SL_ORDER_OR_PRICE")
             
             position_state = Position(
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
                 
                 # Immutable Parameters
                 initial_stop_price=intent_model.stop_loss_futures,
                 trade_type=signal.regime,
                 tp1_price=tp1,
                 tp2_price=tp2,
                 partial_close_pct=Decimal("0.5"), # Default config
                 
                 # ID Linking
                 stop_loss_order_id=new_sl_id, 
                 tp_order_ids=new_tp_ids,
                 
                 # Protection Status
                 is_protected=is_protected,
                 protection_reason=protection_reason
             )
             
             assert self.managed_positions is not None, "managed_positions must exist in legacy mode"
             self.managed_positions[futures_symbol] = position_state
             logger.info("Position State initialized", symbol=futures_symbol)
             
             # NEW: persist position state immediately (so TP/SL survives restarts)
             from src.storage.repository import save_position
             await asyncio.to_thread(save_position, position_state)
             logger.info("Position persisted to database", symbol=futures_symbol)
             
             return {
                 "order_placed": True,
                 "reason": f"Entry order placed successfully (order_id: {entry_order.order_id})",
                 "rejection_reasons": []
             }
        else:
            # Entry order failed
            return {
                "order_placed": False,
                "reason": "Entry order execution failed",
                "rejection_reasons": ["executor_returned_none"]
            }

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
            trade_type=signal.regime if hasattr(signal, 'regime') else "tight_smc"
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
        return _ok()

    async def _update_candles(self, symbol: str):
        """Update local candle caches from acquisition with throttling."""
        await self.candle_manager.update_candles(symbol)

    def _init_managed_position(
        self,
        exchange_data: Dict,
        mark_price: Decimal,
        db_pos: Optional[Position] = None,
        orders_for_symbol: Optional[List[Dict]] = None
    ) -> Position:
        """
        Hydrate Position object from exchange data (for recovery).
        
        CRITICAL: Deterministic SL recovery with exact precedence:
        1. DB active position has initial_stop_price → use it
        2. Else, parse open reduce-only stop order → extract stop price
        3. Else → mark as UNPROTECTED (no fabrication)
        """
        symbol = exchange_data.get('symbol')
        if not symbol:
            raise ValueError("Missing 'symbol' in exchange data")
        
        logger.warning(f"Hydrating position for {symbol} (Recovery)")
        
        # Defensive: Ensure required keys exist
        if 'entry_price' not in exchange_data:
            logger.error(f"Missing 'entry_price' in exchange data for {symbol}", data_keys=list(exchange_data.keys()))
            raise ValueError(f"Cannot hydrate position: missing entry_price")
        
        # Determine side from signed size (safest, handles all Kraken formats)
        size_raw = Decimal(str(exchange_data.get('size', 0)))
        side = Side.LONG if size_raw > 0 else Side.SHORT
        
        # Recover initial_stop_price with exact precedence
        initial_sl = None
        sl_order_id = None
        protection_reason = None
        
        # 1. Try DB first (best case - preserved from previous session)
        if db_pos and db_pos.initial_stop_price:
            initial_sl = db_pos.initial_stop_price
            sl_order_id = db_pos.stop_loss_order_id
            logger.info("Preserved initial_stop_price from DB", symbol=symbol, sl=str(initial_sl), sl_order_id=sl_order_id)
        
        # 2. Try open orders (recover from exchange)
        if not initial_sl and orders_for_symbol:
            for order in orders_for_symbol:
                # Check if this is a reduce-only stop order
                is_reduce_only = order.get('reduceOnly', False)
                order_type = str(order.get('type', '')).lower()
                has_stop_price = order.get('stopPrice') is not None
                is_stop_type = any(stop_term in order_type for stop_term in ['stop', 'stop-loss', 'stop_loss', 'stp'])
                
                if is_reduce_only and (has_stop_price or is_stop_type):
                    # Extract stop price
                    stop_price = order.get('stopPrice') or order.get('price')
                    if stop_price:
                        initial_sl = Decimal(str(stop_price))
                        sl_order_id = order.get('id')
                        logger.info("Extracted initial_stop_price from SL order", symbol=symbol, sl=str(initial_sl), order_id=sl_order_id)
                        break
        
        # 3. Mark UNPROTECTED if still missing (no fabrication)
        if not initial_sl:
            protection_reason = "NO_SL_ORDER_OR_PRICE"
            logger.error("UNPROTECTED position - no SL recovered", symbol=symbol, reason=protection_reason)
        
        # Verify protection status
        # is_protected = True only if both initial_stop_price AND stop_loss_order_id exist
        is_protected = (initial_sl is not None and sl_order_id is not None)
        if initial_sl and not sl_order_id:
            protection_reason = "SL_ORDER_MISSING"
            logger.warning("Position has SL price but no SL order", symbol=symbol, sl_price=str(initial_sl))
        
        return Position(
            symbol=symbol,
            side=side,
            size=size_raw,
            size_notional=Decimal("0"), # Unknown without calc
            entry_price=Decimal(str(exchange_data['entry_price'])),
            current_mark_price=mark_price,
            liquidation_price=Decimal(str(exchange_data.get('liquidationPrice', 0))),
            unrealized_pnl=Decimal(str(exchange_data.get('unrealizedPnl', 0))),
            leverage=Decimal("1"), # Approx
            margin_used=Decimal("0"),
            opened_at=datetime.now(timezone.utc),
            
            # Recovered/protection fields
            initial_stop_price=initial_sl,
            stop_loss_order_id=sl_order_id,
            is_protected=is_protected,
            protection_reason=protection_reason,
            
            # Init defaults
            tp1_price=None,
            tp2_price=None,
            final_target_price=None,
            partial_close_pct=Decimal("0.5"),
            original_size=Decimal(str(exchange_data['size'])),
        )

    async def _execute_management_actions(self, symbol: str, actions: List["LegacyManagementAction"], position: Position):
        """Execute logic actions decided by legacy PositionManager."""
        from src.execution.position_manager import ActionType
        for action in actions:
            logger.info(f"Management Action: {action.type.value}", symbol=symbol, reason=action.reason)
            
            try:
                if action.type == ActionType.CLOSE_POSITION:
                    # Get exit price and reason before closing
                    exit_price = position.current_mark_price
                    exit_reason = action.reason or "unknown"
                    
                    # A) On SL fill → cancel all TP orders and clear local state
                    if exit_reason == "stop_loss" or "stop_loss" in exit_reason.lower():
                        # Cancel all TP orders
                        for tp_id in (position.tp_order_ids or []):
                            try:
                                await self.futures_adapter.cancel_order(tp_id, symbol)
                                logger.info("Cancelled TP order on SL fill", order_id=tp_id, symbol=symbol)
                            except Exception as e:
                                logger.warning("Failed to cancel TP order", order_id=tp_id, symbol=symbol, error=str(e))
                        
                        # Also cancel any single take_profit_order_id if used anywhere
                        if position.take_profit_order_id:
                            try:
                                await self.futures_adapter.cancel_order(position.take_profit_order_id, symbol)
                                logger.info("Cancelled legacy TP order on SL fill", order_id=position.take_profit_order_id, symbol=symbol)
                            except Exception as e:
                                logger.warning("Failed to cancel legacy TP order", order_id=position.take_profit_order_id, symbol=symbol, error=str(e))
                    
                    # B) On final TP fill (position size == 0) → cancel SL order
                    if exit_reason in ("take_profit", "tp1", "tp2", "tp3") and position.size == 0:
                        if position.stop_loss_order_id:
                            try:
                                await self.futures_adapter.cancel_order(position.stop_loss_order_id, symbol)
                                logger.info("Cancelled SL order on final TP fill", order_id=position.stop_loss_order_id, symbol=symbol)
                            except Exception as e:
                                logger.warning("Failed to cancel SL order on final TP", order_id=position.stop_loss_order_id, symbol=symbol, error=str(e))
                    
                    # Market Close
                    await self.client.close_position(symbol)
                    
                    # Mark closed in DB
                    from src.storage.repository import delete_position
                    await asyncio.to_thread(delete_position, symbol)
                    
                    # Save to trade history
                    await self._save_trade_history(position, exit_price, exit_reason)
                    
                    # State update handled on next tick (position gone)
                    
                elif action.type == ActionType.PARTIAL_CLOSE:
                    # Place market reduce-only order (reduce_only=True: exit, no dust)
                    exit_side = 'sell' if position.side == Side.LONG else 'buy'
                    await self.client.place_futures_order(
                        symbol=symbol,
                        side=exit_side,
                        order_type='market',
                        size=float(action.quantity),
                        reduce_only=True,
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
                        resp = await self.client.edit_futures_order(
                            order_id=position.stop_loss_order_id,
                            symbol=symbol,
                            stop_price=float(action.price)
                        )
                        # If edit fell back to cancel+replace, update tracked order id.
                        if isinstance(resp, dict):
                            new_id = resp.get("order_id")
                            if new_id and new_id != position.stop_loss_order_id:
                                logger.info(
                                    "Stop-loss order id updated after replace",
                                    symbol=symbol,
                                    old_order_id=position.stop_loss_order_id,
                                    new_order_id=new_id,
                                )
                                position.stop_loss_order_id = new_id
                    else:
                        logger.warning("Cannot update stop - no SL Order ID tracked", symbol=symbol)
                        
            except Exception as e:
                logger.error(f"Failed to execute {action.type}", symbol=symbol, error=str(e))
    
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
                    # Check if protective orders are live
                    futures_symbol = pos.symbol
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
                    # Remove from managed positions
                    if self.managed_positions is not None and symbol in self.managed_positions:
                        del self.managed_positions[symbol]
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
                            available_margin_override=refreshed_available_margin,
                            notional_override=candidate.position_notional,
                            skip_margin_check=True,
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
            
        except Exception as e:
            logger.error("Failed to save trade history", symbol=position.symbol, error=str(e))

    async def _check_dynamic_exits(self, symbol: str, position: Position, signal: Signal, candle_count: int):
        """
        Check and execute dynamic exits (Abandon Ship, Time-based).
        V4 Strategy Enhancement.
        """
        if not position or position.size == 0:
            return

        # V2 uses state-machine-native management actions; legacy dynamic exits are disabled here.
        if self.use_state_machine_v2:
            return

        from src.execution.position_manager import ActionType, ManagementAction

        actions = []
        
        # 1. Abandon Ship (Bias Flip)
        if getattr(self.config.strategy, 'abandon_ship_enabled', False):
            # Bias direction map
            bias = signal.higher_tf_bias # "bullish", "bearish", "neutral"
            
            # If we are LONG and bias flips to BEARISH -> Close
            if position.side == Side.LONG and bias == "bearish":
                logger.warning(f"🌊 ABANDON SHIP: Long position vs Bearish bias", symbol=symbol)
                actions.append(ManagementAction(
                    type=ActionType.CLOSE_POSITION,
                    quantity=position.size,
                    reason="abandon_ship_bias_flip"
                ))
            
            # If we are SHORT and bias flips to BULLISH -> Close
            elif position.side == Side.SHORT and bias == "bullish":
                logger.warning(f"🌊 ABANDON SHIP: Short position vs Bullish bias", symbol=symbol)
                actions.append(ManagementAction(
                    type=ActionType.CLOSE_POSITION,
                    quantity=position.size,
                    reason="abandon_ship_bias_flip"
                ))

        # 2. Time-Based Exit
        # If position held > X bars and we haven't hit TP1 yet (or just held too long)
        # Using 15m bars approx
        time_limit_bars = getattr(self.config.strategy, 'time_based_exit_bars', 0)
        if time_limit_bars > 0:
            # Calculate bars held
            # Approximate using timestamps
            elapsed = datetime.now(timezone.utc) - position.opened_at
            # 15 mins per bar
            bars_held = elapsed.total_seconds() / 900 
            
            if bars_held > time_limit_bars:
                # Only close if not in profit? or strictly time based?
                # User prompt: "If no take-profit (TP1...) is hit within X bars, close"
                # Check active TP status (tp1_hit flag on position)
                if not position.tp1_hit:
                     logger.info(f"⌛ TIME EXIT: Held {bars_held:.1f} bars > limit {time_limit_bars}", symbol=symbol)
                     actions.append(ManagementAction(
                        type=ActionType.CLOSE_POSITION,
                        quantity=position.size,
                        reason="time_based_stale_exit"
                    ))

        if actions:
            await self._execute_management_actions(symbol, actions, position)

    async def _reconcile_protective_orders(self, raw_positions: List[Dict], current_prices: Dict[str, Decimal]):
        """
        TP Backfill / Reconciliation loop that repairs positions missing TP coverage.
        
        Runs after position sync to ensure all open positions have proper TP ladder.
        """
        if not self.config.execution.tp_backfill_enabled:
            return
        
        from src.storage.repository import get_active_position, save_position, async_record_event
        
        skipped_not_protected: List[str] = []
        for pos_data in raw_positions:
            symbol = pos_data.get('symbol')
            if not symbol or pos_data.get('size', 0) == 0:
                continue
            
            try:
                # Validate pos_data is a dict
                if not isinstance(pos_data, dict):
                    logger.error("Invalid pos_data type in _reconcile_protective_orders", symbol=symbol, pos_data_type=type(pos_data).__name__)
                    continue
                
                # Get persisted position state
                db_pos = await asyncio.to_thread(get_active_position, symbol)
                if not db_pos:
                    # Position not in DB yet - skip (will be created on next entry)
                    continue
                
                # Get current market price
                if not isinstance(current_prices, dict):
                    logger.error("Invalid current_prices type", symbol=symbol, current_prices_type=type(current_prices).__name__)
                    continue
                    
                current_price = current_prices.get(symbol)
                if not current_price:
                    logger.debug("Skipping TP backfill - no current price", symbol=symbol)
                    continue
                
                # Ensure current_price is a Decimal, not a dict
                if isinstance(current_price, dict):
                    logger.error("Invalid current_price type (dict)", symbol=symbol, price_type=type(current_price).__name__)
                    continue
                if not isinstance(current_price, Decimal):
                    current_price = Decimal(str(current_price))
                
                # Step 4: Safety checks - skip if unsafe
                # In V2 mode, check registry for protection status (DB may be stale)
                is_protected = db_pos.is_protected
                if self.use_state_machine_v2 and self.position_registry:
                    v2_pos = self.position_registry.get_position(symbol)
                    if v2_pos and v2_pos.stop_order_id:
                        is_protected = True  # V2 registry says it's protected
                
                if not is_protected:
                    skipped_not_protected.append(symbol)
                if await self._should_skip_tp_backfill(symbol, pos_data, db_pos, current_price, is_protected):
                    continue
                
                # Get open orders for this symbol
                # NOTE: positions use PF_* while orders may use CCXT unified symbols.
                from src.data.symbol_utils import position_symbol_matches_order
                open_orders = await self.client.get_futures_open_orders()
                symbol_orders = [o for o in open_orders if position_symbol_matches_order(symbol, o.get('symbol') or '')]
                
                # Step 1: Determine if TP coverage is missing
                needs_backfill = self._needs_tp_backfill(db_pos, symbol_orders)
                
                if not needs_backfill:
                    logger.debug("TP backfill not needed", symbol=symbol, has_tp_plan=bool(db_pos.tp1_price or db_pos.tp2_price), has_tp_ids=bool(db_pos.tp_order_ids), open_tp_count=len([o for o in symbol_orders if o.get('reduceOnly', False)]))
                    continue
                
                logger.info("TP backfill needed", symbol=symbol, has_tp_plan=bool(db_pos.tp1_price or db_pos.tp2_price), has_tp_ids=bool(db_pos.tp_order_ids), open_tp_count=len([o for o in symbol_orders if o.get('reduceOnly', False)]))
                
                # Step 2: Get or compute TP plan
                tp_plan = await self._compute_tp_plan(symbol, pos_data, db_pos, current_price)
                
                if not tp_plan:
                    await async_record_event(
                        "TP_BACKFILL_SKIPPED",
                        symbol,
                        {
                            "reason": "failed_to_compute_plan",
                            "entry": str(pos_data.get('entry_price', 0)),
                            "sl": str(db_pos.initial_stop_price) if db_pos.initial_stop_price else None
                        }
                    )
                    continue
                
                # Step 3: Place / repair TP orders
                await self._place_tp_backfill(symbol, pos_data, db_pos, tp_plan, symbol_orders, current_price)
                
            except Exception as e:
                logger.error("TP backfill failed", symbol=symbol, error=str(e))
                await async_record_event(
                    "TP_BACKFILL_SKIPPED",
                    symbol,
                    {"reason": f"error: {str(e)}"}
                )

        if skipped_not_protected:
            symbols_dedupe = sorted(set(skipped_not_protected))
            logger.warning(
                "Positions needing protection (TP backfill skipped)",
                symbols=symbols_dedupe,
                count=len(symbols_dedupe),
                action="Run 'make place-missing-stops' (dry-run) then 'make place-missing-stops-live' to protect.",
            )

    async def _reconcile_stop_loss_order_ids(self, raw_positions: List[Dict]):
        """
        Reconcile stop loss order IDs from exchange with database positions.
        
        This fixes the issue where stop loss orders exist on exchange but aren't
        tracked in the database, causing false "UNPROTECTED" alerts.
        """
        from src.storage.repository import get_active_position, save_position
        
        try:
            # Get all open orders from exchange
            open_orders = await self.client.get_futures_open_orders()
            
            # Group orders by *normalized* symbol so PF_* positions match unified order symbols
            from src.data.symbol_utils import normalize_symbol_for_position_match
            orders_by_symbol: Dict[str, List[Dict]] = {}
            for order in open_orders:
                sym = order.get('symbol')
                key = normalize_symbol_for_position_match(sym) if sym else ""
                if key:
                    if key not in orders_by_symbol:
                        orders_by_symbol[key] = []
                    orders_by_symbol[key].append(order)
            
            # For each position, check if we have a stop loss order on exchange
            for pos_data in raw_positions:
                symbol = pos_data.get('symbol')
                if not symbol or pos_data.get('size', 0) == 0:
                    continue
                
                try:
                    # Get database position
                    db_pos = await asyncio.to_thread(get_active_position, symbol)
                    if not db_pos:
                        continue
                    
                    # Skip only if the DB already considers the position fully protected.
                    # We still need to reconcile when:
                    # - stop_loss_order_id exists but initial_stop_price is missing
                    # - is_protected flag is stale/false even though fields exist
                    if (
                        db_pos.is_protected
                        and db_pos.stop_loss_order_id
                        and db_pos.initial_stop_price
                        and not str(db_pos.stop_loss_order_id).startswith("unknown_")
                    ):
                        continue
                    
                    # Look for stop loss orders for this symbol
                    symbol_orders = orders_by_symbol.get(normalize_symbol_for_position_match(symbol), [])
                    stop_loss_order = None
                    
                    for order in symbol_orders:
                        # Check if this is a reduce-only stop order
                        info = order.get("info")
                        if not isinstance(info, dict):
                            info = {}
                        is_reduce_only = (
                            order.get("reduceOnly")
                            if order.get("reduceOnly") is not None
                            else (
                                order.get("reduce_only")
                                if order.get("reduce_only") is not None
                                else info.get("reduceOnly", info.get("reduce_only", False))
                            )
                        )
                        order_type = str(order.get("type") or info.get("orderType") or info.get("type") or "").lower()
                        has_stop_price = (
                            order.get("stopPrice") is not None
                            or order.get("triggerPrice") is not None
                            or info.get("stopPrice") is not None
                            or info.get("triggerPrice") is not None
                        )
                        is_stop_type = any(stop_term in order_type for stop_term in ['stop', 'stop-loss', 'stop_loss', 'stp'])
                        
                        # Match stop loss orders: reduce-only and has stop price or stop type
                        if is_reduce_only and (has_stop_price or is_stop_type):
                            # Verify it's for the correct side (opposite of position)
                            order_side = order.get('side', '').lower()
                            pos_side = _exchange_position_side(pos_data)
                            expected_order_side = 'sell' if pos_side == 'long' else 'buy'
                            
                            if order_side == expected_order_side:
                                stop_loss_order = order
                                break
                    
                    # If we found a stop loss order but database doesn't have it, update
                    if stop_loss_order:
                        sl_order_id = stop_loss_order.get('id')
                        if sl_order_id:
                            logger.info(
                                "Reconciled stop loss order ID from exchange",
                                symbol=symbol,
                                stop_loss_order_id=sl_order_id,
                                previous_sl_id=db_pos.stop_loss_order_id
                            )
                            
                            # Update position with stop loss order ID
                            db_pos.stop_loss_order_id = sl_order_id

                            # If we are missing the stop price in DB, backfill it from exchange.
                            # Without this, positions can remain "unprotected" even when the stop exists.
                            if db_pos.initial_stop_price is None:
                                stop_price_raw = (
                                    stop_loss_order.get("stopPrice")
                                    or stop_loss_order.get("triggerPrice")
                                    or (stop_loss_order.get("info") or {}).get("stopPrice")
                                    or (stop_loss_order.get("info") or {}).get("triggerPrice")
                                )
                                if stop_price_raw is not None:
                                    try:
                                        stop_price_dec = Decimal(str(stop_price_raw))
                                        # Sanity: stop must be on the correct side of entry.
                                        if db_pos.side == Side.LONG and stop_price_dec < db_pos.entry_price:
                                            db_pos.initial_stop_price = stop_price_dec
                                        elif db_pos.side == Side.SHORT and stop_price_dec > db_pos.entry_price:
                                            db_pos.initial_stop_price = stop_price_dec
                                        else:
                                            logger.warning(
                                                "Skip reconciling initial_stop_price: direction mismatch",
                                                symbol=symbol,
                                                db_side=db_pos.side.value if hasattr(db_pos.side, "value") else str(db_pos.side),
                                                entry_price=str(db_pos.entry_price),
                                                exchange_stop_price=str(stop_price_dec),
                                            )
                                    except Exception as e:
                                        logger.warning(
                                            "Failed to parse stop price from exchange order",
                                            symbol=symbol,
                                            error=str(e),
                                        )
                            
                            # Update is_protected flag if we have both price and order ID
                            if db_pos.initial_stop_price and sl_order_id:
                                db_pos.is_protected = True
                                db_pos.protection_reason = None
                                logger.info(
                                    "Position marked as protected after reconciliation",
                                    symbol=symbol,
                                    is_protected=True
                                )
                            
                            # Save updated position to database
                            await asyncio.to_thread(save_position, db_pos)
                            
                            # Also update managed_positions if it exists
                            if self.managed_positions is not None and symbol in self.managed_positions:
                                self.managed_positions[symbol].stop_loss_order_id = sl_order_id
                                if db_pos.initial_stop_price:
                                    self.managed_positions[symbol].is_protected = True
                                    self.managed_positions[symbol].protection_reason = None
                
                except Exception as e:
                    logger.warning(
                        "Failed to reconcile stop loss order ID",
                        symbol=symbol,
                        error=str(e)
                    )
                    continue
        
        except Exception as e:
            logger.error("Stop loss order ID reconciliation failed", error=str(e))

    async def _place_missing_stops_for_unprotected(self, raw_positions: List[Dict], max_per_tick: int = 3) -> None:
        """
        Place missing stop-loss orders for positions that have no SL on exchange.
        Reduces UNPROTECTED alerts by auto-placing one reduce-only stop per naked position (rate-limited).
        """
        from src.data.symbol_utils import position_symbol_matches_order

        def _order_is_stop(o: Dict, side: str) -> bool:
            t = (o.get("info") or {}).get("orderType") or o.get("type") or o.get("order_type") or ""
            t = str(t).lower()
            if "take_profit" in t or "take-profit" in t:
                return False
            if "stop" not in t and "stop_loss" not in t and t != "stop":
                return False
            if not o.get("reduceOnly", o.get("reduce_only", False)):
                return False
            order_side = (o.get("side") or "").lower()
            expect = "sell" if side == "long" else "buy"
            return order_side == expect

        if self.config.system.dry_run:
            return
        try:
            open_orders = await self.client.get_futures_open_orders()
        except Exception as e:
            logger.warning("Failed to fetch open orders for missing-stops check", error=str(e))
            return
        naked = []
        for pos_data in raw_positions:
            pos_sym = pos_data.get("symbol") or ""
            if not pos_sym or float(pos_data.get("size", 0)) == 0:
                continue
            side = _exchange_position_side(pos_data)
            has_stop = False
            for o in open_orders:
                if not position_symbol_matches_order(pos_sym, o.get("symbol") or ""):
                    continue
                if _order_is_stop(o, side):
                    has_stop = True
                    break
            if not has_stop:
                naked.append(pos_data)
        if not naked:
            return
        stop_pct = Decimal("2.0")
        placed = 0
        for pos_data in naked:
            if placed >= max_per_tick:
                break
            symbol = pos_data.get("symbol") or ""
            size = Decimal(str(pos_data.get("size", 0)))
            if size <= 0:
                continue
            # Kraken minimum amount is often 0.001; skip if below to avoid venue rejection
            if size < Decimal("0.001"):
                logger.debug("Skip placing missing stop: size below venue min", symbol=symbol, size=str(size))
                continue
            entry = Decimal(str(pos_data.get("entryPrice", pos_data.get("entry_price", 0))))
            if entry <= 0:
                continue
            side = _exchange_position_side(pos_data)
            if side == "long":
                stop_price = entry * (Decimal("1") - stop_pct / Decimal("100"))
            else:
                stop_price = entry * (Decimal("1") + stop_pct / Decimal("100"))
            close_side = "sell" if side == "long" else "buy"
            unified = symbol
            if symbol.startswith("PF_") and "/" not in symbol:
                from src.data.symbol_utils import pf_to_unified
                unified = pf_to_unified(symbol) or symbol
            try:
                # reduce_only=True: protective stop, no dust
                await self.client.place_futures_order(
                    symbol=unified,
                    side=close_side,
                    order_type="stop",
                    size=size,
                    stop_price=stop_price,
                    reduce_only=True,
                )
                logger.info(
                    "Placed missing stop for unprotected position",
                    symbol=symbol,
                    stop_price=str(stop_price),
                    size=str(size),
                )
                placed += 1
            except Exception as e:
                logger.warning(
                    "Failed to place missing stop for unprotected position",
                    symbol=symbol,
                    error=str(e),
                )

    async def _should_skip_tp_backfill(
        self, symbol: str, pos_data: Dict, db_pos: Position, current_price: Decimal,
        is_protected: Optional[bool] = None
    ) -> bool:
        """Step 4: Don't backfill when it's unsafe."""
        # Check cooldown
        last_backfill = self.tp_backfill_cooldowns.get(symbol)
        if last_backfill:
            elapsed = (datetime.now(timezone.utc) - last_backfill).total_seconds()
            cooldown_seconds = self.config.execution.tp_backfill_cooldown_minutes * 60
            if elapsed < cooldown_seconds:
                logger.debug("TP backfill skipped: cooldown", symbol=symbol, elapsed=elapsed, cooldown=cooldown_seconds)
                return True
        
        # Position size <= 0 (coerce: exchange may return str)
        try:
            size_val = float(pos_data.get('size') or 0)
        except (TypeError, ValueError):
            size_val = 0
        if size_val <= 0:
            logger.debug("TP backfill skipped: zero size", symbol=symbol)
            return True
        
        # Require protection (not just initial_stop_price)
        # Use passed is_protected if provided (V2 mode), else fall back to db_pos
        protected = is_protected if is_protected is not None else db_pos.is_protected
        if not protected:
            logger.warning("TP backfill skipped: position not protected", symbol=symbol, reason=db_pos.protection_reason, has_sl_price=bool(db_pos.initial_stop_price), has_sl_order=bool(db_pos.stop_loss_order_id))
            return True
        
        # Position is within MIN_HOLD_SECONDS after entry
        if db_pos.opened_at:
            elapsed = (datetime.now(timezone.utc) - db_pos.opened_at).total_seconds()
            if elapsed < self.config.execution.min_hold_seconds:
                logger.debug("TP backfill skipped: too new", symbol=symbol, elapsed=elapsed, min_hold=self.config.execution.min_hold_seconds)
                return True
        
        return False

    def _needs_tp_backfill(self, db_pos: Position, symbol_orders: List[Dict]) -> bool:
        """Step 1: Determine if TP coverage is missing."""
        # Check if db_pos has TP plan or order IDs
        has_tp_plan = (db_pos.tp1_price is not None) or (db_pos.tp2_price is not None)
        has_tp_ids = bool(db_pos.tp_order_ids and len(db_pos.tp_order_ids) > 0)
        
        # Check for open TP orders on exchange
        # Note: Detection uses reduce-only + opposite side + type check
        # This may match other reduce-only limit orders if we ever place them for other purposes
        # For now, this is safe as we only place reduce-only orders for SL/TP
        open_tp_orders = [
            o for o in symbol_orders
            if o.get('reduceOnly', False) and 
            o.get('type', '').lower() in ('take_profit', 'take-profit', 'limit') and
            # For LONG positions, TP orders are SELL (opposite side)
            # For SHORT positions, TP orders are BUY (opposite side)
            ((db_pos.side == Side.LONG and o.get('side', '').lower() == 'sell') or
             (db_pos.side == Side.SHORT and o.get('side', '').lower() == 'buy'))
        ]
        
        # Additional check: prefer orders with explicit take_profit type if available
        # Some exchanges provide clearer order type differentiation
        explicit_tp_orders = [
            o for o in open_tp_orders
            if o.get('type', '').lower() in ('take_profit', 'take-profit')
        ]
        
        # If we have explicit TP orders, use those; otherwise fall back to reduce-only limit orders
        if explicit_tp_orders:
            open_tp_orders = explicit_tp_orders
        
        # Needs backfill if:
        # 1. No TP plan and no TP order IDs in DB
        if not has_tp_plan and not has_tp_ids:
            return True
        
        # 2. No open TP orders on exchange
        if len(open_tp_orders) == 0:
            return True
        
        # 3. Fewer TP orders than expected
        min_expected = self.config.execution.min_tp_orders_expected
        if len(open_tp_orders) < min_expected:
            return True
        
        return False

    async def _compute_tp_plan(
        self, symbol: str, pos_data: Dict, db_pos: Position, current_price: Decimal
    ) -> Optional[List[Decimal]]:
        """Step 2: Get or compute a TP plan."""
        # Prefer stored plan
        tp_plan = []
        if db_pos.tp1_price:
            tp_plan.append(db_pos.tp1_price)
        if db_pos.tp2_price:
            tp_plan.append(db_pos.tp2_price)
        if db_pos.final_target_price:
            tp_plan.append(db_pos.final_target_price)
        
        if len(tp_plan) >= 2:  # We have a stored plan
            return tp_plan
        
        # Compute deterministically using R-multiples
        # Ensure pos_data is a dict, not a Decimal
        if not isinstance(pos_data, dict):
            logger.error("Invalid pos_data type in _compute_tp_plan", symbol=symbol, pos_data_type=type(pos_data).__name__)
            return None
        
        entry = Decimal(str(pos_data.get('entry_price', pos_data.get('entryPrice', 0))))
        sl = db_pos.initial_stop_price
        
        if not entry or not sl or entry == 0:
            return None
        
        risk = abs(entry - sl)
        if risk == 0:
            return None
        
        # Determine side sign
        side_sign = Decimal("1") if db_pos.side == Side.LONG else Decimal("-1")
        
        # Compute TP ladder: 1R, 2R, 3R
        tp1 = entry + side_sign * Decimal("1.0") * risk
        tp2 = entry + side_sign * Decimal("2.0") * risk
        tp3 = entry + side_sign * Decimal("3.0") * risk
        
        tp_plan = [tp1, tp2, tp3]
        
        # Emit planned event
        from src.storage.repository import async_record_event
        await async_record_event(
            "TP_BACKFILL_PLANNED",
            symbol,
            {
                "side": db_pos.side.value,
                "entry": str(entry),
                "sl": str(sl),
                "risk": str(risk),
                "tp_plan": [str(tp) for tp in tp_plan],
                "reason": "computed_from_r_multiples"
            }
        )
        
        # Sanity guards
        min_distance = current_price * Decimal(str(self.config.execution.min_tp_distance_pct))
        
        if db_pos.side == Side.LONG:
            # For LONG: require tp1 > current_price * (1 + min_tp_distance_pct)
            if tp1 <= current_price + min_distance:
                logger.warning("TP1 too close to current price (LONG)", symbol=symbol, tp1=str(tp1), current=str(current_price))
                return None
        else:  # SHORT
            # For SHORT: require tp1 < current_price * (1 - min_tp_distance_pct)
            if tp1 >= current_price - min_distance:
                logger.warning("TP1 too close to current price (SHORT)", symbol=symbol, tp1=str(tp1), current=str(current_price))
                return None
        
        # Optional: clamp extreme TPs
        if self.config.execution.max_tp_distance_pct:
            max_distance = current_price * Decimal(str(self.config.execution.max_tp_distance_pct))
            if db_pos.side == Side.LONG:
                tp_plan = [min(tp, current_price + max_distance) for tp in tp_plan]
            else:
                tp_plan = [max(tp, current_price - max_distance) for tp in tp_plan]
        
        return tp_plan

    async def _cleanup_orphan_reduce_only_orders(self, raw_positions: List[Dict]):
        """
        Cleanup orphan reduce-only orders (SL/TP) for positions that no longer exist.
        
        Critical: When SL/TP fills, the position closes but reduce-only orders may remain.
        These must be cancelled to prevent:
        - Orders failing later with "no position"
        - Potential position flips if reduce-only isn't honored correctly
        """
        # Build set of symbols that currently have open positions
        # Normalize both position symbols and order symbols for comparison
        open_syms = set()
        for p in raw_positions:
            pos_sym = p.get("symbol")
            if pos_sym and p.get("size", 0) != 0:
                open_syms.add(pos_sym)
                # Also add normalized versions for comparison
                # Positions use "PF_BLURUSD", orders might use "BLUR/USD:USD"
                if pos_sym.startswith("PF_"):
                    # Convert PF_BLURUSD -> BLUR/USD:USD for comparison
                    base = pos_sym[3:-3]  # Remove "PF_" and "USD"
                    if base == "XBT":
                        base = "BTC"
                    normalized = f"{base}/USD:USD"
                    open_syms.add(normalized)
        
        try:
            orders = await self.client.get_futures_open_orders()
        except Exception as e:
            logger.error("Failed to fetch open orders for orphan cleanup", error=str(e))
            return
        
        cancelled = 0
        max_cancellations = 20  # Rate limit per tick
        
        for o in orders:
            if cancelled >= max_cancellations:
                break
                
            try:
                # Only process reduce-only orders
                if not o.get("reduceOnly", False):
                    continue
                
                sym = o.get("symbol")
                oid = o.get("id")
                
                if not sym or not oid:
                    continue
                
                # Normalize order symbol for comparison
                # Orders might be "BLUR/USD:USD", convert to "PF_BLURUSD" for comparison
                normalized_order_sym = sym
                if "/" in sym and ":" in sym:
                    # Format: "BLUR/USD:USD" -> "PF_BLURUSD"
                    base = sym.split("/")[0]
                    if base == "BTC":
                        base = "XBT"
                    normalized_order_sym = f"PF_{base}USD"
                
                # If symbol (or normalized version) has an open position, keep the order
                if sym in open_syms or normalized_order_sym in open_syms:
                    continue
                
                # Position is closed but order remains - cancel it
                # Skip if order_id is invalid (e.g., "unknown_" prefix)
                if oid and not oid.startswith("unknown_"):
                    try:
                        await self.futures_adapter.cancel_order(oid, sym)
                        cancelled += 1
                        logger.info(
                            "Cancelled orphan reduce-only order",
                            symbol=sym,
                            order_id=oid,
                            order_type=o.get("type", "unknown")
                        )
                    except Exception as e:
                        # Handle invalidArgument errors gracefully
                        error_str = str(e)
                        if "invalidArgument" in error_str or "order_id" in error_str.lower():
                            logger.debug(
                                "Skipped orphan order cancellation - invalid order ID",
                                symbol=sym,
                                order_id=oid,
                                error=error_str
                            )
                        else:
                            logger.warning(
                                "Failed to cancel orphan reduce-only order",
                                symbol=sym,
                                order_id=oid,
                                error=str(e)
                            )
                else:
                    logger.debug(
                        "Skipped orphan order cancellation - placeholder order ID",
                        symbol=sym,
                        order_id=oid
                    )
                
            except Exception as e:
                logger.warning(
                    "Error processing orphan order",
                    symbol=o.get("symbol"),
                    order_id=o.get("id"),
                    error=str(e)
                )
        
        if cancelled > 0:
            logger.info("Orphan order cleanup complete", cancelled=cancelled, total_orders=len(orders))

    async def _place_tp_backfill(
        self, symbol: str, pos_data: Dict, db_pos: Position, tp_plan: List[Decimal],
        symbol_orders: List[Dict], current_price: Decimal
    ):
        """Step 3: Place / repair TP orders on exchange."""
        from src.storage.repository import save_position, async_record_event
        
        # Get existing TP order IDs
        existing_tp_ids = db_pos.tp_order_ids or []
        
        # Check if existing TPs match plan (within tolerance)
        existing_tp_orders = [
            o for o in symbol_orders
            if o.get('id') in existing_tp_ids or
            (o.get('reduceOnly', False) and o.get('type', '').lower() in ('take_profit', 'take-profit', 'limit'))
        ]
        
        needs_replace = False
        if existing_tp_orders:
            # Check if prices match (within tolerance)
            tolerance = Decimal(str(self.config.execution.tp_price_tolerance))
            for existing_order in existing_tp_orders:
                existing_price = Decimal(str(existing_order.get('price', 0)))
                if existing_price == 0:
                    continue
                
                # Find closest planned TP
                closest_planned = min(tp_plan, key=lambda tp: abs(tp - existing_price))
                price_diff_pct = abs(existing_price - closest_planned) / closest_planned
                
                if price_diff_pct > tolerance:
                    needs_replace = True
                    break
        else:
            needs_replace = True
        
        if not needs_replace:
            await async_record_event(
                "TP_BACKFILL_SKIPPED",
                symbol,
                {"reason": "tp_orders_match_plan", "tp_count": len(existing_tp_orders)}
            )
            return
        
        # Cancel existing TPs if replacing
        for tp_id in existing_tp_ids:
            try:
                await self.futures_adapter.cancel_order(tp_id, symbol)
                logger.debug("Cancelled existing TP for backfill", order_id=tp_id, symbol=symbol)
            except Exception as e:
                logger.warning("Failed to cancel existing TP", order_id=tp_id, symbol=symbol, error=str(e))
        
        # Place new TP ladder
        try:
            # CRITICAL: Use centralized method to convert position size to notional
            # This handles exchange-specific size formats correctly
            position_size_notional = await self.futures_adapter.position_size_notional(
                symbol=symbol,
                pos_data=pos_data,
                current_price=current_price
            )
            
            new_sl_id, new_tp_ids = await self.executor.update_protective_orders(
                symbol=symbol,
                side=db_pos.side,
                current_sl_id=db_pos.stop_loss_order_id,
                new_sl_price=db_pos.initial_stop_price,  # Keep SL unchanged
                current_tp_ids=existing_tp_ids,
                new_tp_prices=tp_plan,
                position_size_notional=position_size_notional,  # Pass actual position size
            )
            
            # Update position state
            db_pos.tp_order_ids = new_tp_ids
            db_pos.tp1_price = tp_plan[0] if len(tp_plan) > 0 else None
            db_pos.tp2_price = tp_plan[1] if len(tp_plan) > 1 else None
            db_pos.final_target_price = tp_plan[2] if len(tp_plan) > 2 else None
            
            # Persist
            await asyncio.to_thread(save_position, db_pos)
            
            # Update cooldown
            self.tp_backfill_cooldowns[symbol] = datetime.now(timezone.utc)
            
            # Emit event
            await async_record_event(
                "TP_BACKFILL_PLACED" if not existing_tp_orders else "TP_BACKFILL_REPLACED",
                symbol,
                {
                    "side": db_pos.side.value,
                    "size": str(pos_data.get('size', 0)),
                    "entry": str(pos_data.get('entry_price', 0)),
                    "sl": str(db_pos.initial_stop_price),
                    "tp_plan": [str(tp) for tp in tp_plan],
                    "tp_order_ids": new_tp_ids,
                    "existing_tp_prices": [str(Decimal(str(o.get('price', 0)))) for o in existing_tp_orders] if existing_tp_orders else [],
                    "reason": "backfill_repair" if existing_tp_orders else "backfill_new"
                }
            )
            
            logger.info(
                "TP backfill completed",
                symbol=symbol,
                action="replaced" if existing_tp_orders else "placed",
                tp_count=len(new_tp_ids)
            )
            
        except Exception as e:
            logger.error("Failed to place TP backfill", symbol=symbol, error=str(e))
            await async_record_event(
                "TP_BACKFILL_SKIPPED",
                symbol,
                {"reason": f"placement_failed: {str(e)}"}
            )