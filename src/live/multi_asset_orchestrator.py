"""
Multi-Asset Orchestrator for managing trading across all eligible markets.

Coordinates signal generation, risk validation, and execution for multiple
assets while maintaining per-asset isolation and enforcing global portfolio limits.
"""
import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional, Set
from datetime import datetime, timezone

from src.data.market_registry import MarketRegistry, MarketPair
from src.data.kraken_client import KrakenClient
from src.data.data_acquisition import DataAcquisition
from src.strategy.smc_engine import SMCEngine
from src.risk.risk_manager import RiskManager
from src.execution.executor import Executor
from src.domain.models import Signal, Position, SignalType, Side
from src.monitoring.logger import get_logger

logger = get_logger(__name__)


@dataclass
class AssetState:
    """Per-asset state tracking."""
    symbol: str
    
    # Health status
    spot_feed_healthy: bool = True
    futures_feed_healthy: bool = True
    basis_healthy: bool = True
    last_health_check: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    # Market state
    regime: str = "unknown"
    bias: str = "neutral"
    signal_strength: float = 0.0
    
    # Trading state
    last_signal: Optional[Signal] = None
    position: Optional[Position] = None
    
    # PnL tracking
    unrealized_pnl: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    daily_pnl: Decimal = Decimal("0")
    
    # Rejection tracking
    consecutive_rejections: int = 0
    last_rejection_reason: Optional[str] = None
    
    def mark_unhealthy(self, reason: str):
        """Mark asset as unhealthy."""
        self.spot_feed_healthy = False
        self.last_health_check = datetime.now(timezone.utc)
        logger.warning(f"{self.symbol} marked unhealthy", reason=reason)
    
    def mark_healthy(self):
        """Mark asset as healthy."""
        self.spot_feed_healthy = True
        self.futures_feed_healthy = True
        self.basis_healthy = True
        self.last_health_check = datetime.now(timezone.utc)


class MultiAssetOrchestrator:
    """
    Orchestrates trading across multiple assets.
    
    Responsibilities:
    - Maintain asset registry
    - Update candle buffers for all assets
    - Generate signals per asset (isolated)
    - Apply risk validation per asset
    - Enforce global portfolio limits
    - Manage asset health
    """
    
    def __init__(
        self,
        config,
        registry: MarketRegistry,
        client: KrakenClient,
        data_acquisition: DataAcquisition,
        smc_engine: SMCEngine,
        risk_manager: RiskManager,
        executor: Executor
    ):
        self.config = config
        self.registry = registry
        self.client = client
        self.data_acquisition = data_acquisition
        self.smc_engine = smc_engine
        self.risk_manager = risk_manager
        self.executor = executor
        
        # Asset state tracking
        self.assets: Dict[str, AssetState] = {}
        self.eligible_pairs: List[MarketPair] = []
        
        # Global state
        self.kill_switch_active = False
        self.block_new_entries = False
        
        # Metrics
        self.total_equity = Decimal(str(config.backtest.starting_equity))
        self.cycle_count = 0
    
    async def initialize(self):
        """Initialize orchestrator and discover markets."""
        logger.info("Initializing Multi-Asset Orchestrator...")
        
        # Discover markets
        await self._refresh_markets()
        
        logger.info(
            "Orchestrator initialized",
            eligible_assets=len(self.eligible_pairs),
            mode=self.config.assets.mode
        )
    
    async def run(self):
        """Main orchestration loop."""
        await self.initialize()
        
        while not self.kill_switch_active:
            try:
                # 1. Refresh market registry (periodically)
                if self.registry.needs_refresh(self.config.exchange.discovery_refresh_hours):
                    await self._refresh_markets()
                
                # 2. Update candle buffers for all assets
                await self._update_all_candles()
                
                # 3. Process each asset independently
                for pair in self.eligible_pairs:
                    await self._process_asset(pair.spot_symbol)
                
                # 4. Enforce global portfolio limits
                self._enforce_portfolio_limits()
                
                # 5. Update metrics
                self._update_metrics()
                
                self.cycle_count += 1
                
                # Sleep between cycles (1 minute default)
                await asyncio.sleep(60)
                
            except Exception as e:
                logger.error("Orchestrator cycle failed", error=str(e))
                await asyncio.sleep(10)
    
    async def _refresh_markets(self):
        """Refresh market registry and rebuild asset states."""
        logger.info("Refreshing market registry...")
        
        # Discover markets
        await self.registry.discover_markets()
        
        # Apply mode filtering
        self.eligible_pairs = self.registry.get_eligible_markets(
            mode=self.config.assets.mode,
            whitelist=self.config.assets.whitelist,
            blacklist=self.config.assets.blacklist
        )
        
        # Initialize asset states
        for pair in self.eligible_pairs:
            if pair.spot_symbol not in self.assets:
                self.assets[pair.spot_symbol] = AssetState(symbol=pair.spot_symbol)
        
        # Remove delisted assets
        current_symbols = {pair.spot_symbol for pair in self.eligible_pairs}
        for symbol in list(self.assets.keys()):
            if symbol not in current_symbols:
                del self.assets[symbol]
        
        logger.info(
            "Market refresh complete",
            eligible=len(self.eligible_pairs),
            total_discovered=len(self.registry.discovered_pairs)
        )
    
    async def _update_all_candles(self):
        """Update candle buffers for all eligible assets."""
        symbols = [pair.spot_symbol for pair in self.eligible_pairs]
        
        try:
            await self.data_acquisition.fetch_candles_for_all(symbols)
        except Exception as e:
            logger.error("Failed to update candles", error=str(e))
    
    async def _process_asset(self, symbol: str):
        """
        Process a single asset through the complete pipeline.
        
        Isolation guarantee: Failure in one asset does not affect others.
        """
        state = self.assets.get(symbol)
        if not state:
            return
        
        try:
            # 1. Health check
            if not self._is_asset_healthy(symbol):
                return
            
            # 2. Skip if already have position (no pyramiding)
            if state.position:
                logger.debug(f"{symbol}: Skipping - position already open")
                return
            
            # 3. Skip if global entry block
            if self.block_new_entries:
                logger.debug(f"{symbol}: Skipping - new entries blocked globally")
                return
            
            # 4. Generate signal
            signal = await self._generate_signal(symbol)
            
            if signal.signal_type == SignalType.NO_SIGNAL:
                return
            
            # Store signal
            state.last_signal = signal
            state.signal_strength = 1.0  # TODO: Calculate from signal metadata
            
            # 5. Risk validation
            decision = await self._validate_trade(symbol, signal)
            
            if not decision.approved:
                state.consecutive_rejections += 1
                state.last_rejection_reason = "; ".join(decision.rejection_reasons)
                logger.info(
                    f"{symbol}: Trade rejected",
                    reasons=decision.rejection_reasons
                )
                return
            
            # Reset rejection counter
            state.consecutive_rejections = 0
            
            # 6. Execute trade
            await self._execute_trade(symbol, signal, decision)
            
        except Exception as e:
            logger.error(f"{symbol}: Processing failed", error=str(e))
            state.mark_unhealthy(f"Processing error: {str(e)}")
    
    def _is_asset_healthy(self, symbol: str) -> bool:
        """Check if asset is healthy enough to trade."""
        state = self.assets[symbol]
        
        # Check feed health
        if not state.spot_feed_healthy:
            logger.debug(f"{symbol}: Spot feed unhealthy")
            return False
        
        if not state.futures_feed_healthy:
            logger.debug(f"{symbol}: Futures feed unhealthy")
            return False
        
        # Check basis
        if not state.basis_healthy:
            logger.debug(f"{symbol}: Basis unhealthy")
            return False
        
        # Global kill switch
        if self.kill_switch_active:
            return False
        
        return True
    
    async def _generate_signal(self, symbol: str) -> Signal:
        """Generate trading signal for symbol."""
        # Get candles from data acquisition
        candles = self.data_acquisition.get_candles(symbol)
        
        if not candles:
            return Signal(
                timestamp=datetime.now(timezone.utc),
                symbol=symbol,
                signal_type=SignalType.NO_SIGNAL,
                entry_price=Decimal("0"),
                stop_loss=Decimal("0"),
                take_profit=None,
                reasoning="No candle data available",
                higher_tf_bias="neutral",
                adx=Decimal("0"),
                atr=Decimal("0"),
                ema200_slope="flat"
            )
        
        # Generate signal using SMC engine
        signal = self.smc_engine.generate_signal(
            symbol=symbol,
            bias_candles_4h=candles.get("4h", []),
            bias_candles_1d=candles.get("1d", []),
            exec_candles_15m=candles.get("15m", []),
            exec_candles_1h=candles.get("1h", [])
        )
        
        # Update state
        state = self.assets[symbol]
        state.regime = "trending" if signal.adx > 25 else "ranging"
        state.bias = signal.higher_tf_bias
        
        return signal
    
    async def _validate_trade(self, symbol: str, signal: Signal):
        """Validate trade against risk limits."""
        # Get current prices
        spot_price = signal.entry_price
        futures_mark_price = signal.entry_price  # TODO: Fetch actual mark price
        
        # Run risk validation
        decision = self.risk_manager.validate_trade(
            signal=signal,
            account_equity=self.total_equity,
            spot_price=spot_price,
            perp_mark_price=futures_mark_price
        )
        
        return decision
    
    async def _execute_trade(self, symbol: str, signal: Signal, decision):
        """Execute approved trade."""
        # TODO: Integrate with actual executor
        logger.info(
            f"{symbol}: Trade approved",
            type=signal.signal_type.value,
            notional=str(decision.position_notional),
            leverage=str(decision.leverage)
        )
        
        # For now, just log - actual execution will be added later
        # await self.executor.execute(signal, decision)
    
    def _enforce_portfolio_limits(self):
        """Enforce global portfolio limits."""
        # Count active positions
        active_positions = sum(1 for state in self.assets.values() if state.position)
        
        if active_positions >= self.config.risk.max_concurrent_positions:
            if not self.block_new_entries:
                logger.warning(
                    "Max concurrent positions reached - blocking new entries",
                    active=active_positions,
                    max=self.config.risk.max_concurrent_positions
                )
            self.block_new_entries = True
        else:
            self.block_new_entries = False
        
        # Check daily loss limit
        total_daily_pnl = sum(state.daily_pnl for state in self.assets.values())
        daily_loss_limit = self.total_equity * Decimal(str(self.config.risk.daily_loss_limit_pct))
        
        if total_daily_pnl < -daily_loss_limit:
            logger.error(
                "Daily loss limit exceeded - TRIGGERING KILL SWITCH",
                daily_pnl=str(total_daily_pnl),
                limit=str(daily_loss_limit)
            )
            self.trigger_kill_switch()
    
    def _update_metrics(self):
        """Update aggregate portfolio metrics."""
        # This will be enhanced with actual position tracking
        pass
    
    def trigger_kill_switch(self):
        """Activate kill switch - stops all new trading."""
        self.kill_switch_active = True
        self.block_new_entries = True
        logger.critical("KILL SWITCH ACTIVATED - All trading stopped")
    
    def get_portfolio_summary(self) -> dict:
        """Get aggregate portfolio metrics."""
        active_positions = sum(1 for s in self.assets.values() if s.position)
        total_pnl = sum(s.daily_pnl for s in self.assets.values())
        
        return {
            "total_equity": str(self.total_equity),
            "total_pnl": str(total_pnl),
            "active_positions": active_positions,
            "assets_monitored": len(self.eligible_pairs),
            "assets_healthy": sum(1 for s in self.assets.values() if s.spot_feed_healthy),
            "kill_switch_active": self.kill_switch_active,
            "cycle_count": self.cycle_count
        }
