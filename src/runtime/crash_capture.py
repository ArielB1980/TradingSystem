"""
Crash capture instrumentation for production trading bot.

Three layers of crash attribution:
1. faulthandler: C-level segfault/abort stacktraces to stderr (captured by journald)
2. SIGUSR2 → dump all-thread tracebacks to logs/fault.log (on-demand diagnosis)
3. asyncio exception handler → logs/crash.log (unhandled task exceptions)

All output goes to files under logs/ so it survives process death and systemd restart.
"""

from __future__ import annotations

import faulthandler
import logging
import os
import signal
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_LOGS_DIR = Path("logs")
_FAULT_LOG = _LOGS_DIR / "fault.log"
_CRASH_LOG = _LOGS_DIR / "crash.log"


def _ensure_logs_dir() -> None:
    _LOGS_DIR.mkdir(parents=True, exist_ok=True)


def enable_faulthandler() -> None:
    """Enable faulthandler for segfault/abort tracebacks on all threads."""
    _ensure_logs_dir()
    faulthandler.enable(all_threads=True)


def register_sigusr2_dump() -> None:
    """
    Register SIGUSR2 to dump all-thread tracebacks to logs/fault.log.

    Usage from shell:  kill -USR2 <pid>
    """
    _ensure_logs_dir()

    def _dump_handler(signum: int, frame: object) -> None:
        try:
            with open(_FAULT_LOG, "a") as f:
                ts = datetime.now(timezone.utc).isoformat()
                f.write(f"\n{'='*72}\n")
                f.write(f"SIGUSR2 traceback dump at {ts}  (PID {os.getpid()})\n")
                f.write(f"{'='*72}\n")
                faulthandler.dump_traceback(file=f, all_threads=True)
                f.write(f"\n{'='*72}\n\n")
        except Exception:
            pass

    signal.signal(signal.SIGUSR2, _dump_handler)


def write_crash_log(
    exc: BaseException,
    context: str = "unknown",
    cycle_id: Optional[str] = None,
) -> None:
    """
    Append a timestamped crash entry to logs/crash.log.

    Designed to be called from the top-level exception handler around _tick()
    or from the asyncio exception handler. Never raises.
    """
    try:
        _ensure_logs_dir()
        with open(_CRASH_LOG, "a") as f:
            ts = datetime.now(timezone.utc).isoformat()
            f.write(f"\n{'='*72}\n")
            f.write(f"CRASH at {ts}  PID={os.getpid()}  context={context}")
            if cycle_id:
                f.write(f"  cycle_id={cycle_id}")
            f.write(f"\n{type(exc).__name__}: {exc}\n")
            f.write(f"{'-'*72}\n")
            tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
            f.write("".join(tb))
            f.write(f"{'='*72}\n\n")
    except Exception:
        pass


def install_asyncio_exception_handler(loop: object) -> None:
    """
    Install a global asyncio exception handler that logs unhandled task
    exceptions to logs/crash.log instead of silently swallowing them.
    """

    def _handler(loop_obj: object, context: dict) -> None:
        exc = context.get("exception")
        message = context.get("message", "Unhandled exception in asyncio task")

        if exc is not None:
            write_crash_log(exc, context=f"asyncio_task: {message}")

        logger = logging.getLogger("src.runtime.crash_capture")
        logger.error(
            "ASYNCIO_UNHANDLED_EXCEPTION: %s — %s: %s",
            message,
            type(exc).__name__ if exc else "N/A",
            str(exc) if exc else "no exception",
        )

    loop.set_exception_handler(_handler)  # type: ignore[attr-defined]


def setup_all() -> None:
    """Convenience: enable all crash capture layers (call before asyncio.run)."""
    enable_faulthandler()
    register_sigusr2_dump()
