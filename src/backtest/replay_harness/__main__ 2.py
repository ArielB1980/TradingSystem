"""Allow running replay harness as: python -m src.backtest.replay_harness"""

from src.backtest.replay_harness.run_episodes import main as _main
import argparse
import asyncio
import sys


def main():
    parser = argparse.ArgumentParser(description="Run replay backtest episodes")
    parser.add_argument("--episode", type=str, help="Run specific episode (e.g. 1_normal)")
    parser.add_argument("--data-dir", type=str, default="data/replay", help="Base directory for episode data")
    parser.add_argument("--output", type=str, default="results/replay", help="Output directory for results")
    parser.add_argument("--seed", type=int, default=42, help="Jitter seed (run with 1..10 to verify safety)")
    args = parser.parse_args()
    exit_code = asyncio.run(_main(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
