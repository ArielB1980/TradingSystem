"""
BacktestRunner — Orchestrates LiveTrading in replay mode.

Runs the real LiveTrading._tick() method step-by-step with:
- Simulated clock (SimClock)
- Simulated exchange (ReplayKrakenClient)
- Metrics collection (ReplayMetrics)
- Optional fault injection (FaultInjector)

Key trick: we don't run LiveTrading.run() (which has the main loop + sleeps).
Instead we:
1. Construct LiveTrading with the replay client
2. Call the startup sequence manually
3. Step through ticks one at a time, advancing the clock between each

This gives us deterministic control while exercising the real code paths.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable
from unittest.mock import patch, AsyncMock

from src.backtest.replay_harness.sim_clock import SimClock
from src.backtest.replay_harness.data_store import ReplayDataStore
from src.backtest.replay_harness.exchange_sim import ReplayKrakenClient, ExchangeSimConfig
from src.backtest.replay_harness.fault_injector import FaultInjector
from src.backtest.replay_harness.metrics import ReplayMetrics
from src.exceptions import InvariantError, OperationalError, DataError
from src.monitoring.logger import get_logger

logger = get_logger(__name__)


class BacktestRunner:
    """Step-by-step replay runner for the live trading stack.

    Usage:
        runner = BacktestRunner(
            data_dir=Path("data/replay/episode_1"),
            symbols=["BTC/USD:USD", "ETH/USD:USD"],
            start=datetime(2025, 1, 1, tzinfo=timezone.utc),
            end=datetime(2025, 1, 2, tzinfo=timezone.utc),
        )
        results = await runner.run()
        results.print_report()
    """

    def __init__(
        self,
        data_dir: Path,
        symbols: List[str],
        start: datetime,
        end: datetime,
        *,
        tick_interval_seconds: int = 60,
        exchange_config: Optional[ExchangeSimConfig] = None,
        fault_injector: Optional[FaultInjector] = None,
        config_overrides: Optional[Dict[str, Any]] = None,
        max_ticks: Optional[int] = None,
        timeframes: Optional[List[str]] = None,
    ):
        self._data_dir = Path(data_dir)
        self._symbols = symbols
        self._start = start
        self._end = end
        self._tick_interval = tick_interval_seconds
        self._exchange_config = exchange_config or ExchangeSimConfig()
        self._fault_injector = fault_injector
        self._config_overrides = config_overrides or {}
        self._max_ticks = max_ticks
        self._timeframes = timeframes or ["1m"]

        # Built during setup
        self._clock: Optional[SimClock] = None
        self._data_store: Optional[ReplayDataStore] = None
        self._exchange: Optional[ReplayKrakenClient] = None
        self._metrics: Optional[ReplayMetrics] = None
        self._live_trading: Optional[Any] = None  # LiveTrading instance

    async def run(self) -> ReplayMetrics:
        """Execute the full replay backtest. Returns metrics."""
        self._setup()
        await self._initialize()

        tick_count = 0
        current = self._start

        logger.info(
            "REPLAY_START",
            start=self._start.isoformat(),
            end=self._end.isoformat(),
            symbols=self._symbols,
            tick_interval=self._tick_interval,
        )

        while current <= self._end:
            if self._max_ticks and tick_count >= self._max_ticks:
                break

            self._clock.set(current)

            # Step exchange simulation (process pending orders, triggers, funding)
            fills = self._exchange.step(current)

            # Record fills in metrics
            for fill in fills:
                self._metrics.total_fills += 1
                if fill.is_maker:
                    self._metrics.maker_fills += 1
                else:
                    self._metrics.taker_fills += 1

            # Run one tick of the live trading engine
            try:
                await self._run_tick()
                self._metrics.total_ticks += 1
            except InvariantError as e:
                self._metrics.record_event("INVARIANT_VIOLATION", {"error": str(e)})
                self._metrics.invariant_k_violations += 1
                self._metrics.record_exception("InvariantError")
                logger.error("REPLAY_INVARIANT_VIOLATION", tick=tick_count, error=str(e))
            except OperationalError as e:
                self._metrics.record_exception("OperationalError")
                self._metrics.failed_ticks += 1
            except DataError as e:
                self._metrics.record_exception("DataError")
                self._metrics.failed_ticks += 1
            except Exception as e:
                self._metrics.record_exception(type(e).__name__)
                self._metrics.failed_ticks += 1
                logger.error("REPLAY_TICK_EXCEPTION", tick=tick_count, error=str(e), type=type(e).__name__)

            # Record equity snapshot
            ex = self._exchange.exchange_metrics
            self._metrics.record_equity(
                timestamp=current,
                equity=Decimal(str(ex["equity"])),
                margin_used=Decimal(str(ex["margin_used"])),
                unrealized_pnl=Decimal(str(ex.get("unrealized_pnl", 0))),
                open_positions=ex["open_positions"],
            )

            tick_count += 1
            current += timedelta(seconds=self._tick_interval)

        # Finalize
        self._metrics.total_fees = Decimal(str(self._exchange.exchange_metrics["total_fees"]))
        self._metrics.total_funding = Decimal(str(self._exchange.exchange_metrics["total_funding"]))
        self._metrics.gross_pnl = Decimal(str(self._exchange.exchange_metrics["realized_pnl"]))
        self._metrics.orders_blocked_by_rate_limiter = (
            self._live_trading.execution_gateway._order_rate_limiter.orders_blocked_total
            if self._live_trading and hasattr(self._live_trading, "execution_gateway")
            else 0
        )

        logger.info(
            "REPLAY_COMPLETE",
            ticks=tick_count,
            pnl=float(self._metrics.gross_pnl),
            fees=float(self._metrics.total_fees),
            trades=self._metrics.total_trades,
        )

        return self._metrics

    def _setup(self) -> None:
        """Initialize all components."""
        self._clock = SimClock(start=self._start)

        self._data_store = ReplayDataStore(
            data_dir=self._data_dir,
            symbols=self._symbols,
            timeframes=self._timeframes,
        )
        self._data_store.load()

        self._exchange = ReplayKrakenClient(
            clock=self._clock,
            data_store=self._data_store,
            config=self._exchange_config,
            fault_injector=self._fault_injector,
        )

        self._metrics = ReplayMetrics()
        self._metrics.peak_equity = self._exchange_config.initial_equity_usd

    async def _initialize(self) -> None:
        """Initialize LiveTrading with the replay client.

        We construct a minimal config and inject the replay client.
        """
        from src.config.config import load_config

        # Set env vars for config loading
        os.environ.setdefault("ENV", "local")
        os.environ.setdefault("DRY_RUN", "0")

        config = load_config()

        # Apply overrides
        for key, value in self._config_overrides.items():
            parts = key.split(".")
            obj = config
            for p in parts[:-1]:
                obj = getattr(obj, p)
            setattr(obj, parts[-1], value)

        # Ensure test-safe settings
        config.system.dry_run = False  # We want the exchange sim to receive orders
        config.exchange.spot_markets = self._symbols
        config.exchange.futures_markets = self._symbols

        # Build LiveTrading but inject our replay client
        from src.live.live_trading import LiveTrading

        # Monkey-patch KrakenClient construction
        with patch("src.live.live_trading.KrakenClient", return_value=self._exchange):
            lt = LiveTrading(config)

        # Override the client with our exchange sim
        lt.client = self._exchange

        # Set up minimal execution gateway with the replay client
        if hasattr(lt, "execution_gateway"):
            lt.execution_gateway.client = self._exchange

        self._live_trading = lt

    async def _run_tick(self) -> None:
        """Run one tick of the live trading engine."""
        if self._live_trading is None:
            raise RuntimeError("LiveTrading not initialized")

        # Patch datetime.now and asyncio.sleep for this tick
        original_now = datetime.now

        def patched_now(tz=None):
            if tz:
                return self._clock.now().astimezone(tz)
            return self._clock.now().replace(tzinfo=None)

        with patch("src.live.live_trading.datetime") as mock_dt:
            mock_dt.now = patched_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            mock_dt.fromisoformat = datetime.fromisoformat

            # Run the tick
            await self._live_trading._tick()
