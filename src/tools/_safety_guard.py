"""
Safety guard for operational tools.

Prevents accidental execution with live credentials unless:
  1. --execute flag is passed (vs default --dry-run)
  2. I_UNDERSTAND_LIVE=1 env var is set when live API keys are detected

Usage in every promoted tool:

    from src.tools._safety_guard import parse_tool_args, guard_live_keys

    args = parse_tool_args("Tool description here")
    guard_live_keys()
    dry_run = not args.execute
"""
import argparse
import os
import sys


def parse_tool_args(description: str) -> argparse.Namespace:
    """Standard argument parser for operational tools.

    Adds:
      --execute : Actually perform the action (default is dry-run)
      --verbose : Enable verbose output
    """
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--execute",
        action="store_true",
        default=False,
        help="Actually perform the action. Without this flag, the tool runs in dry-run mode.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        default=False,
        help="Enable verbose output.",
    )
    return parser.parse_args()


def guard_live_keys() -> None:
    """Abort if live API keys are detected without explicit acknowledgement.

    Checks env vars for Kraken API keys. If they look like real keys
    (non-empty, non-placeholder), requires I_UNDERSTAND_LIVE=1.
    """
    key_vars = [
        "KRAKEN_API_KEY",
        "KRAKEN_API_SECRET",
        "KRAKEN_FUTURES_API_KEY",
        "KRAKEN_FUTURES_API_SECRET",
    ]
    live_keys_found = []
    for var in key_vars:
        val = os.environ.get(var, "")
        if val and not val.startswith("${") and val.lower() not in ("", "none", "placeholder"):
            live_keys_found.append(var)

    if live_keys_found and os.environ.get("I_UNDERSTAND_LIVE") != "1":
        print(
            f"\n{'='*60}\n"
            f"  SAFETY GUARD: Live API keys detected!\n"
            f"  Keys found: {', '.join(live_keys_found)}\n\n"
            f"  To run this tool with live credentials, set:\n"
            f"    export I_UNDERSTAND_LIVE=1\n"
            f"{'='*60}\n",
            file=sys.stderr,
        )
        sys.exit(1)
