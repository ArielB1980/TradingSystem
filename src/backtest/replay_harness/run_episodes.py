"""
CLI entrypoint for running replay episodes.

Usage:
    # Run all episodes:
    python -m src.backtest.replay_harness.run_episodes

    # Run specific episode:
    python -m src.backtest.replay_harness.run_episodes --episode 1_normal

    # Run with custom output dir:
    python -m src.backtest.replay_harness.run_episodes --output results/replay

    # Run with different seeds to verify safety across jitter variations:
    python -m src.backtest.replay_harness.run_episodes --seed 1
    python -m src.backtest.replay_harness.run_episodes --seed 2
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from src.backtest.replay_harness.episodes import ALL_EPISODES
from src.monitoring.logger import get_logger

logger = get_logger(__name__)


async def run_episode(name: str, builder, base_dir: Path, output_dir: Path, seed: int = 42) -> bool:
    """Run a single episode and save results. Returns True if passed."""
    print(f"\n{'=' * 70}")
    print(f"EPISODE: {name}  (seed={seed})")
    print(f"{'=' * 70}")

    try:
        runner = builder(base_dir)
        # Override jitter seed if provided
        if runner._exchange_config:
            runner._exchange_config.jitter_seed = seed
        metrics = await runner.run()

        # Save results
        ep_output = output_dir / name
        ep_output.mkdir(parents=True, exist_ok=True)
        metrics.save(ep_output / "metrics.json")
        metrics.print_report()

        # -----------------------------------------------------------
        # Safety-first pass/fail criteria
        # -----------------------------------------------------------
        passed = True
        reasons = []

        # -- General: must complete ticks --
        if metrics.total_ticks == 0:
            reasons.append("FAIL: Zero ticks completed")
            passed = False

        if name == "6_bug":
            # Bug episode: process must crash (not silently continue)
            if metrics.exceptions_caught == 0:
                reasons.append("FAIL: Bug injection should have caused exceptions")
                passed = False
            if "AttributeError" not in metrics.exceptions_by_type:
                reasons.append("FAIL: AttributeError should have been recorded")
                passed = False
            # After the bug fires, NO further ticks should succeed
            # (30m of ticks = 30, bug at minute 30 → at most 30 successful ticks)
            bug_tick = 30  # episode injects at T+30m
            if metrics.total_ticks > bug_tick + 1:
                reasons.append(
                    f"FAIL: Process continued after bug injection "
                    f"({metrics.total_ticks} ticks, expected ≤ {bug_tick + 1})"
                )
                passed = False

        elif name == "4_outage":
            # Outage episode: should have operational errors but recover
            if metrics.exceptions_caught == 0:
                reasons.append("WARN: Expected exceptions during outage")
            # Must not have invariant violations
            if metrics.invariant_k_violations > 0:
                reasons.append(f"FAIL: {metrics.invariant_k_violations} invariant violations during outage")
                passed = False
            # Kill switch must NOT fire during normal outage handling
            if metrics.kill_switch_activations > 0:
                reasons.append("FAIL: Kill switch fired during outage (should degrade, not halt)")
                passed = False

        else:
            # Episodes 1, 2, 3, 5: strict safety invariants
            # No invariant violations
            if metrics.invariant_k_violations > 0:
                reasons.append(f"FAIL: {metrics.invariant_k_violations} invariant violations")
                passed = False
            # No kill switch activations in normal episodes
            if metrics.kill_switch_activations > 0:
                reasons.append(f"FAIL: Kill switch fired ({metrics.kill_switch_activations}x) in non-fault episode")
                passed = False
            # No rate limiter trips in normal episode
            if name == "1_normal" and metrics.orders_blocked_by_rate_limiter > 0:
                reasons.append(
                    f"FAIL: Rate limiter blocked {metrics.orders_blocked_by_rate_limiter} orders in normal market"
                )
                passed = False
            # No breaker opens in normal episode
            if name == "1_normal" and metrics.breaker_open_count > 0:
                reasons.append(f"FAIL: Circuit breaker opened {metrics.breaker_open_count}x in normal market")
                passed = False

        status = "PASS" if passed else "FAIL"
        print(f"\n--- EPISODE {name}: {status} ---")
        for r in reasons:
            print(f"  {r}")

        return passed

    except Exception as e:
        print(f"\n--- EPISODE {name}: ERROR ---")
        print(f"  Exception: {type(e).__name__}: {e}")
        logger.exception("Episode failed", episode=name)
        return False


async def main(args: argparse.Namespace) -> int:
    base_dir = Path(args.data_dir)
    output_dir = Path(args.output)
    seed = args.seed
    output_dir.mkdir(parents=True, exist_ok=True)

    episodes = ALL_EPISODES
    if args.episode:
        if args.episode not in ALL_EPISODES:
            print(f"Unknown episode: {args.episode}")
            print(f"Available: {list(ALL_EPISODES.keys())}")
            return 1
        episodes = {args.episode: ALL_EPISODES[args.episode]}

    print(f"Jitter seed: {seed}")

    results = {}
    for name, builder in episodes.items():
        passed = await run_episode(name, builder, base_dir, output_dir, seed=seed)
        results[name] = passed

    # Summary
    print(f"\n{'=' * 70}")
    print(f"REPLAY SUITE SUMMARY  (seed={seed})")
    print(f"{'=' * 70}")
    for name, passed in results.items():
        status = "PASS" if passed else "FAIL"
        print(f"  {name}: {status}")

    total = len(results)
    passed_count = sum(1 for v in results.values() if v)
    print(f"\n  {passed_count}/{total} episodes passed")
    print(f"{'=' * 70}")

    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run replay backtest episodes")
    parser.add_argument("--episode", type=str, help="Run specific episode (e.g. 1_normal)")
    parser.add_argument("--data-dir", type=str, default="data/replay", help="Base directory for episode data")
    parser.add_argument("--output", type=str, default="results/replay", help="Output directory for results")
    parser.add_argument("--seed", type=int, default=42, help="Jitter seed (run with 1..10 to verify safety)")
    args = parser.parse_args()

    exit_code = asyncio.run(main(args))
    sys.exit(exit_code)
