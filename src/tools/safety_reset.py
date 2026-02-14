"""
Atomic safety reset CLI — clears halt + kill switch + peak equity in one operation.

P0.1: No human should ever have to remember "halt file A + kill switch file B + peak file C".

Usage:
    # Soft reset: clear halt + kill switch, preserve positions/stops, resume trading
    python -m src.tools.safety_reset --mode soft --i-understand

    # Soft reset with peak equity update (most common after stale-peak incident)
    python -m src.tools.safety_reset --mode soft --reset-peak-to-current --i-understand

    # Hard reset: clear everything + cancel non-SL orders (does NOT close positions)
    python -m src.tools.safety_reset --mode hard --i-understand

    # Dry run (show what would be changed without touching anything)
    python -m src.tools.safety_reset --mode soft --dry-run

Audit trail:
    Every reset is logged to the safety_state.json reset_events array
    and also to the structured logger. This provides a permanent record
    of who reset what and when.

CRITICAL: --i-understand is required for non-dry-run operations.
"""
import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

# Ensure project root is on sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))


def _load_env():
    """Load .env.local or .env if present."""
    try:
        from dotenv import load_dotenv
        env_local = _PROJECT_ROOT / ".env.local"
        env_file = _PROJECT_ROOT / ".env"
        if env_local.exists():
            load_dotenv(env_local)
        elif env_file.exists():
            load_dotenv(env_file)
    except ImportError:
        pass


def _read_current_states() -> dict:
    """Read all current safety state files (legacy + unified)."""
    from src.safety.safety_state import get_safety_state_manager
    from src.utils.kill_switch import read_kill_switch_state, _kill_switch_state_path
    from src.safety.invariant_monitor import _peak_equity_path, _load_persisted_peak_equity

    ssm = get_safety_state_manager()
    unified = ssm.load()

    # Also read legacy files if they exist
    ks_state = read_kill_switch_state()
    peak = _load_persisted_peak_equity()

    halt_file = Path.home() / ".trading_system" / "halt_state.json"
    halt_data = None
    if halt_file.exists():
        try:
            halt_data = json.loads(halt_file.read_text())
        except Exception:
            halt_data = {"error": "corrupt"}

    return {
        "unified": unified.to_dict(),
        "legacy_halt_file": str(halt_file),
        "legacy_halt": halt_data,
        "legacy_kill_switch_file": str(_kill_switch_state_path()),
        "legacy_kill_switch": ks_state,
        "legacy_peak_equity_file": str(_peak_equity_path()),
        "legacy_peak_equity": str(peak) if peak else None,
    }


def _clear_legacy_files():
    """Remove legacy state files after unified reset."""
    from src.utils.kill_switch import _kill_switch_state_path
    from src.safety.invariant_monitor import _peak_equity_path

    files = [
        Path.home() / ".trading_system" / "halt_state.json",
        _kill_switch_state_path(),
    ]
    # Note: peak_equity file is NOT removed — it's still read by InvariantMonitor
    # until full migration to unified state is complete.

    for f in files:
        if f.exists():
            try:
                f.unlink()
                print(f"  Removed legacy file: {f}")
            except OSError as e:
                print(f"  WARNING: Could not remove {f}: {e}")


async def _fetch_current_equity() -> Decimal:
    """Fetch current equity from exchange (requires API keys in env)."""
    from src.data.kraken_client import KrakenClient

    client = KrakenClient(
        api_key=os.environ.get("KRAKEN_API_KEY", ""),
        api_secret=os.environ.get("KRAKEN_API_SECRET", ""),
        futures_api_key=os.environ.get("KRAKEN_FUTURES_API_KEY", ""),
        futures_api_secret=os.environ.get("KRAKEN_FUTURES_API_SECRET", ""),
    )
    await client.initialize()
    try:
        info = await client.get_futures_account_info()
        return Decimal(str(info.get("equity", 0)))
    finally:
        if hasattr(client, "futures_exchange") and client.futures_exchange:
            await client.futures_exchange.close()
        if hasattr(client, "exchange") and client.exchange:
            await client.exchange.close()


async def _cancel_non_sl_orders():
    """Cancel non-stop-loss orders (hard reset mode)."""
    from src.data.kraken_client import KrakenClient
    from src.utils.kill_switch import KillSwitch

    client = KrakenClient(
        api_key=os.environ.get("KRAKEN_API_KEY", ""),
        api_secret=os.environ.get("KRAKEN_API_SECRET", ""),
        futures_api_key=os.environ.get("KRAKEN_FUTURES_API_KEY", ""),
        futures_api_secret=os.environ.get("KRAKEN_FUTURES_API_SECRET", ""),
    )
    await client.initialize()
    try:
        ks = KillSwitch(client=client)
        cancelled, preserved = await ks._cancel_non_sl_orders()
        return cancelled, preserved
    finally:
        if hasattr(client, "futures_exchange") and client.futures_exchange:
            await client.futures_exchange.close()
        if hasattr(client, "exchange") and client.exchange:
            await client.exchange.close()


def main():
    parser = argparse.ArgumentParser(
        description="Atomic safety reset — clears halt + kill switch + peak equity.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Show current state (read-only)
  python -m src.tools.safety_reset --dry-run

  # Soft reset: clear halt + kill switch, keep positions
  python -m src.tools.safety_reset --mode soft --i-understand

  # Soft reset + reset peak to current equity (most common)
  python -m src.tools.safety_reset --mode soft --reset-peak-to-current --i-understand

  # Hard reset: also cancel non-SL orders (does NOT close positions)
  python -m src.tools.safety_reset --mode hard --i-understand
        """,
    )
    parser.add_argument(
        "--mode",
        choices=["soft", "hard"],
        default="soft",
        help="soft: clear halt/ks/peak only. hard: also cancel non-SL orders.",
    )
    parser.add_argument(
        "--i-understand",
        action="store_true",
        help="Required confirmation flag for non-dry-run operations.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show current state without changing anything.",
    )
    parser.add_argument(
        "--reset-peak-to-current",
        action="store_true",
        help="Reset peak equity to current exchange equity.",
    )
    parser.add_argument(
        "--set-peak",
        type=float,
        default=None,
        help="Set peak equity to a specific value.",
    )
    parser.add_argument(
        "--operator",
        type=str,
        default=os.environ.get("USER", "cli"),
        help="Operator name for audit trail.",
    )

    args = parser.parse_args()

    _load_env()

    print("=" * 60)
    print("SAFETY RESET TOOL")
    print("=" * 60)
    print()

    # 1. Show current state
    print("CURRENT STATE:")
    print("-" * 40)
    try:
        states = _read_current_states()
        unified = states["unified"]
        print(f"  Halt active:        {unified.get('halt_active', False)}")
        print(f"  Halt reason:        {unified.get('halt_reason', 'none')}")
        print(f"  Kill switch active: {unified.get('kill_switch_active', False)}")
        print(f"  Kill switch reason: {unified.get('kill_switch_reason', 'none')}")
        print(f"  Peak equity:        {unified.get('peak_equity', 'none')}")
        print(f"  Last reset:         {unified.get('last_reset_at', 'never')}")
        print(f"  Last reset by:      {unified.get('last_reset_by', 'n/a')}")
        print()
        
        # Also show legacy files
        if states["legacy_halt"]:
            print(f"  [LEGACY] halt_state.json:        {states['legacy_halt']}")
        if states["legacy_kill_switch"].get("active"):
            print(f"  [LEGACY] kill_switch_state.json:  {states['legacy_kill_switch']}")
        if states["legacy_peak_equity"]:
            print(f"  [LEGACY] peak_equity_state.json:  peak={states['legacy_peak_equity']}")
        print()
    except Exception as e:
        print(f"  ERROR reading state: {e}")
        print()

    if args.dry_run:
        print("DRY RUN — no changes made.")
        return

    # 2. Validate confirmation
    if not args.i_understand:
        print("ERROR: --i-understand flag is required for non-dry-run operations.")
        print("       This flag confirms you understand this modifies live safety state.")
        sys.exit(1)

    # 3. Determine new peak equity
    new_peak = None
    if args.set_peak is not None:
        new_peak = Decimal(str(args.set_peak))
        print(f"Peak equity will be set to: ${new_peak:.2f}")
    elif args.reset_peak_to_current:
        print("Fetching current equity from exchange...")
        try:
            new_peak = asyncio.run(_fetch_current_equity())
            print(f"Current equity: ${new_peak:.2f}")
            print(f"Peak equity will be reset to: ${new_peak:.2f}")
        except Exception as e:
            print(f"ERROR: Could not fetch equity: {e}")
            print("You can use --set-peak <value> instead.")
            sys.exit(1)
    print()

    # 4. Perform the reset
    print(f"PERFORMING {args.mode.upper()} RESET...")
    print("-" * 40)

    from src.safety.safety_state import get_safety_state_manager
    ssm = get_safety_state_manager()

    # Atomic reset of unified state
    new_state = ssm.atomic_reset(
        operator=args.operator,
        mode=args.mode,
        new_peak_equity=new_peak,
    )
    print(f"  Unified safety state reset: OK")

    # Clear legacy files
    _clear_legacy_files()

    # Hard mode: cancel non-SL orders
    if args.mode == "hard":
        print("  Cancelling non-stop-loss orders...")
        try:
            cancelled, preserved = asyncio.run(_cancel_non_sl_orders())
            print(f"  Cancelled: {cancelled}, Preserved SLs: {preserved}")
        except Exception as e:
            print(f"  WARNING: Order cancellation failed: {e}")

    print()
    print("RESET COMPLETE")
    print("=" * 60)
    print(f"  Mode:           {args.mode}")
    print(f"  Operator:       {args.operator}")
    print(f"  Halt cleared:   True")
    print(f"  KS cleared:     True")
    print(f"  Peak equity:    {new_state.peak_equity or 'unchanged'}")
    print(f"  Timestamp:      {new_state.last_reset_at}")
    print()
    print("Next steps:")
    print("  1. Restart the trading service: sudo systemctl restart trading-bot.service")
    print("  2. Monitor logs: journalctl -u trading-bot.service -f")
    print("  3. Verify heartbeat: cat runtime/heartbeat.json")


if __name__ == "__main__":
    main()
