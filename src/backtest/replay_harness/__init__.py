"""
Event-driven replay harness for validating the live trading stack.

Runs the real LiveTrading engine against a deterministic simulated exchange
that replays candles, models microstructure, and injects faults.

Core components:
- SimClock: Deterministic time control
- ReplayDataStore: Candle + liquidity model provider
- ReplayKrakenClient: Full exchange simulator (fills, stops, partials)
- FaultInjector: Scripted outages / rate limits / errors
- BacktestRunner: Orchestrates LiveTrading in replay mode
- ReplayMetrics: Safety + correctness + trading metrics collection
"""

from src.backtest.replay_harness.sim_clock import SimClock
from src.backtest.replay_harness.data_store import ReplayDataStore, LiquidityParams
from src.backtest.replay_harness.exchange_sim import ExchangeSimConfig, FundingCurve
from src.backtest.replay_harness.fault_injector import FaultInjector, FaultSpec
from src.backtest.replay_harness.metrics import ReplayMetrics

__all__ = [
    "SimClock",
    "ReplayDataStore",
    "LiquidityParams",
    "ExchangeSimConfig",
    "FundingCurve",
    "FaultInjector",
    "FaultSpec",
    "ReplayMetrics",
]
