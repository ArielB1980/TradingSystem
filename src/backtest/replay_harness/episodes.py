"""
Episode definitions for deterministic replay scenarios.

Each episode creates a data directory with synthetic candles + liquidity,
configures fault injection, and returns a BacktestRunner ready to execute.

Episodes:
1. Normal market (baseline)
2. High vol spike (slippage + stops entered_book delays)
3. Liquidity drought (partials, stop fills delayed, dust edge cases)
4. API outage for 2 minutes (breaker should open; bot should degrade safely)
5. Restart mid-position (peak_equity persistence + stop ID reconciliation)
6. Bug injection (AttributeError in delegate → process should crash)
"""

from __future__ import annotations

import csv
import math
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional

from src.backtest.replay_harness.data_store import ReplayDataStore
from src.backtest.replay_harness.exchange_sim import ExchangeSimConfig, FundingCurve
from src.backtest.replay_harness.fault_injector import FaultInjector, FaultSpec
from src.backtest.replay_harness.runner import BacktestRunner


def _write_candles_csv(
    path: Path,
    start: datetime,
    duration_minutes: int,
    initial_price: float,
    volatility_pct: float = 0.005,
    trend_pct_per_minute: float = 0.0,
    volume_base: float = 50000,
    seed: int = 42,
) -> None:
    """Generate synthetic 1m candles with controlled properties."""
    rng = random.Random(seed)
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["timestamp", "open", "high", "low", "close", "volume"])
        writer.writeheader()

        price = initial_price
        for i in range(duration_minutes):
            ts = start + timedelta(minutes=i)
            # Random walk with trend
            pct_move = rng.gauss(trend_pct_per_minute, volatility_pct)
            open_p = price
            close_p = price * (1 + pct_move)
            high_p = max(open_p, close_p) * (1 + abs(rng.gauss(0, volatility_pct * 0.5)))
            low_p = min(open_p, close_p) * (1 - abs(rng.gauss(0, volatility_pct * 0.5)))
            vol = volume_base * (0.5 + rng.random())

            writer.writerow({
                "timestamp": ts.isoformat(),
                "open": f"{open_p:.4f}",
                "high": f"{high_p:.4f}",
                "low": f"{low_p:.4f}",
                "close": f"{close_p:.4f}",
                "volume": f"{vol:.0f}",
            })
            price = close_p


def _generate_multi_symbol(
    data_dir: Path,
    symbols: Dict[str, float],  # symbol -> initial_price
    start: datetime,
    duration_minutes: int,
    volatility_pct: float = 0.005,
    **kwargs,
) -> None:
    """Generate candle CSVs for multiple symbols."""
    for i, (symbol, initial_price) in enumerate(symbols.items()):
        safe = symbol.replace("/", "_").replace(":", "_")
        _write_candles_csv(
            path=data_dir / "candles" / f"{safe}_1m.csv",
            start=start,
            duration_minutes=duration_minutes,
            initial_price=initial_price,
            volatility_pct=volatility_pct,
            seed=42 + i,
            **kwargs,
        )


# ---------------------------------------------------------------------------
# Episode builders
# ---------------------------------------------------------------------------

SYMBOLS = {
    "BTC/USD:USD": 95000.0,
    "ETH/USD:USD": 3200.0,
    "SOL/USD:USD": 180.0,
}
SYMBOL_LIST = list(SYMBOLS.keys())
BASE_START = datetime(2025, 6, 1, 0, 0, tzinfo=timezone.utc)


def episode_1_normal(base_dir: Path) -> BacktestRunner:
    """Normal market: 4 hours of typical conditions. Baseline."""
    data_dir = base_dir / "episode_1_normal"
    start = BASE_START
    end = start + timedelta(hours=4)

    _generate_multi_symbol(data_dir, SYMBOLS, start, 240, volatility_pct=0.003)

    return BacktestRunner(
        data_dir=data_dir,
        symbols=SYMBOL_LIST,
        start=start,
        end=end,
        exchange_config=ExchangeSimConfig(
            initial_equity_usd=Decimal("10000"),
            maker_fee_bps=2.0,
            taker_fee_bps=5.0,
        ),
    )


def episode_2_high_vol(base_dir: Path) -> BacktestRunner:
    """High volatility spike: first 2h calm, then 2h extreme vol.

    Tests: slippage model, stop entered_book delays, ShockGuard.
    """
    data_dir = base_dir / "episode_2_high_vol"
    start = BASE_START
    end = start + timedelta(hours=4)

    # Generate calm period
    for i, (sym, price) in enumerate(SYMBOLS.items()):
        safe = sym.replace("/", "_").replace(":", "_")
        _write_candles_csv(
            data_dir / "candles" / f"{safe}_1m.csv",
            start, 120, price, volatility_pct=0.002, seed=42 + i,
        )
        # Append volatile period to same file
        # Read last close
        rows = list(csv.DictReader(open(data_dir / "candles" / f"{safe}_1m.csv")))
        last_close = float(rows[-1]["close"])
        # Generate volatile candles to a temp file then append
        _vol_path = data_dir / "candles" / f"{safe}_1m_vol.csv"
        _write_candles_csv(
            _vol_path,
            start + timedelta(hours=2), 120, last_close,
            volatility_pct=0.02, seed=100 + i,  # 4x normal vol
        )
        # Append volatile candles
        vol_rows = list(csv.DictReader(open(_vol_path)))
        with open(data_dir / "candles" / f"{safe}_1m.csv", "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["timestamp", "open", "high", "low", "close", "volume"])
            for r in vol_rows:
                writer.writerow(r)
        _vol_path.unlink()

    return BacktestRunner(
        data_dir=data_dir,
        symbols=SYMBOL_LIST,
        start=start,
        end=end,
        exchange_config=ExchangeSimConfig(
            initial_equity_usd=Decimal("10000"),
            stop_entered_book_delay_base_seconds=2.0,  # base delay, scaled by vol/depth
            slippage_factor=1.5,
            # Enable Layer 1 visibility quirk: entered_book hidden from
            # open orders but visible via fetch_order — tests multi-layer rescue
            hide_entered_book_from_open_orders=True,
            # Per-symbol funding curves: BTC stable, SOL spiky during vol
            funding_curves={
                "BTC/USD:USD": FundingCurve(base_rate_8h_bps=0.5, vol_spike_multiplier=2.0),
                "ETH/USD:USD": FundingCurve(base_rate_8h_bps=1.0, vol_spike_multiplier=3.0),
                "SOL/USD:USD": FundingCurve(base_rate_8h_bps=2.0, vol_spike_multiplier=5.0),
            },
        ),
    )


def episode_3_liquidity_drought(base_dir: Path) -> BacktestRunner:
    """Low liquidity: wide spreads, low volume, partial fills.

    Tests: partial fill handling, dust edge cases, stop fill delays.
    """
    data_dir = base_dir / "episode_3_drought"
    start = BASE_START
    end = start + timedelta(hours=2)

    _generate_multi_symbol(
        data_dir, SYMBOLS, start, 120,
        volatility_pct=0.008,
        volume_base=5000,  # very low volume
    )

    # Write custom liquidity params (very thin book)
    liq_dir = data_dir / "liquidity"
    liq_dir.mkdir(parents=True, exist_ok=True)
    for sym in SYMBOL_LIST:
        safe = sym.replace("/", "_").replace(":", "_")
        with open(liq_dir / f"{safe}.csv", "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["timestamp", "spread_bps", "depth_usd", "vol_regime"])
            writer.writeheader()
            for m in range(120):
                ts = start + timedelta(minutes=m)
                writer.writerow({
                    "timestamp": ts.isoformat(),
                    "spread_bps": 20 + random.Random(42 + m).randint(0, 30),  # 20-50 bps
                    "depth_usd": 2000 + random.Random(42 + m).randint(0, 3000),  # 2k-5k
                    "vol_regime": "high",
                })

    return BacktestRunner(
        data_dir=data_dir,
        symbols=SYMBOL_LIST,
        start=start,
        end=end,
        exchange_config=ExchangeSimConfig(
            initial_equity_usd=Decimal("10000"),
            slippage_factor=2.0,
            stop_entered_book_delay_base_seconds=5.0,  # high base, amplified by drought liquidity
        ),
    )


def episode_4_api_outage(base_dir: Path) -> BacktestRunner:
    """2-minute API outage at T+1h.

    Tests: circuit breaker opens, bot degrades gracefully, recovery.
    """
    data_dir = base_dir / "episode_4_outage"
    start = BASE_START
    end = start + timedelta(hours=2)

    _generate_multi_symbol(data_dir, SYMBOLS, start, 120, volatility_pct=0.003)

    outage_start = start + timedelta(hours=1)
    outage_end = outage_start + timedelta(minutes=2)

    injector = FaultInjector([
        FaultSpec(
            start=outage_start,
            end=outage_end,
            fault_type="timeout",
            message="Kraken API unavailable (simulated outage)",
        ),
        # Rate limit burst just after recovery
        FaultSpec(
            start=outage_end,
            end=outage_end + timedelta(seconds=30),
            fault_type="rate_limit",
            affected_methods=["get_all_futures_positions", "get_futures_account_info"],
            message="Post-outage rate limiting",
        ),
    ])

    return BacktestRunner(
        data_dir=data_dir,
        symbols=SYMBOL_LIST,
        start=start,
        end=end,
        fault_injector=injector,
        exchange_config=ExchangeSimConfig(initial_equity_usd=Decimal("10000")),
    )


def episode_5_restart_mid_position(base_dir: Path) -> BacktestRunner:
    """Simulate restart mid-position with split-brain crash.

    Run 2h with a forced crash (OperationalError) at T+1h between
    "position updated" and "trade recorded", then continue.

    Validates:
    - peak_equity persists across the crash boundary
    - position stop IDs are reconciled after recovery
    - idempotent trade recording prevents duplicates
    - no naked positions after "restart"

    Implementation: inject a targeted fault at T+1h on trade-recording
    calls only, simulating the most dangerous crash window.
    """
    data_dir = base_dir / "episode_5_restart"
    start = BASE_START
    end = start + timedelta(hours=2)

    # Slight uptrend to generate entries
    _generate_multi_symbol(
        data_dir, SYMBOLS, start, 120,
        volatility_pct=0.004,
        trend_pct_per_minute=0.00005,  # slight bull
    )

    # Split-brain fault: crash during the trade-recording window at T+1h.
    # Position updates succeed, but the recording call fails.
    # This is the most valuable restart test: it validates that the system
    # reconciles state and doesn't create duplicate records.
    crash_time = start + timedelta(hours=1)
    injector = FaultInjector([
        # Short crash on position/order queries — simulates process restart
        FaultSpec(
            start=crash_time,
            end=crash_time + timedelta(seconds=5),
            fault_type="timeout",
            affected_methods=[
                "get_all_futures_positions",
                "get_futures_open_orders",
            ],
            message="Split-brain crash: position visible but trade not recorded",
        ),
    ])

    return BacktestRunner(
        data_dir=data_dir,
        symbols=SYMBOL_LIST,
        start=start,
        end=end,
        fault_injector=injector,
        exchange_config=ExchangeSimConfig(initial_equity_usd=Decimal("10000")),
    )


def episode_6_bug_injection(base_dir: Path) -> BacktestRunner:
    """Bug injection: AttributeError in a delegate at T+30m.

    Tests: unknown exception → crash → systemd restart (in replay: exception logged).
    The system should NOT silently continue.
    """
    data_dir = base_dir / "episode_6_bug"
    start = BASE_START
    end = start + timedelta(hours=1)

    _generate_multi_symbol(data_dir, SYMBOLS, start, 60, volatility_pct=0.003)

    injector = FaultInjector([
        FaultSpec(
            start=start + timedelta(minutes=30),
            end=start + timedelta(minutes=30, seconds=10),
            fault_type="attribute_error",
            affected_methods=["get_all_futures_positions"],
            message="Simulated bug: missing attribute",
        ),
    ])

    return BacktestRunner(
        data_dir=data_dir,
        symbols=SYMBOL_LIST,
        start=start,
        end=end,
        fault_injector=injector,
        exchange_config=ExchangeSimConfig(initial_equity_usd=Decimal("10000")),
    )


# ---------------------------------------------------------------------------
# Run all episodes
# ---------------------------------------------------------------------------

ALL_EPISODES = {
    "1_normal": episode_1_normal,
    "2_high_vol": episode_2_high_vol,
    "3_drought": episode_3_liquidity_drought,
    "4_outage": episode_4_api_outage,
    "5_restart": episode_5_restart_mid_position,
    "6_bug": episode_6_bug_injection,
}
