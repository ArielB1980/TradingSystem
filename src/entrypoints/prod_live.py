"""
Dedicated production-live entrypoint.

Design requirements:
- Guards + distributed lock MUST run before importing any heavy runtime code (config, exchange clients, LiveTrading).
- Production MUST NEVER load dotenv files (.env / .env.local).
- Fail-fast with non-zero exit codes on safety violations.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    v = os.getenv(name)
    return v if v is not None else default


def _config_path() -> str:
    """
    Resolve config path without importing config modules.
    """
    return str(_env("CONFIG_PATH", "src/config/config.yaml") or "src/config/config.yaml")


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def main() -> None:
    # --- Guardrails and lock BEFORE any config/runtime imports ---
    try:
        from src.runtime.guards import assert_prod_live_prereqs, acquire_prod_live_lock
    except Exception as e:
        print(f"FATAL: could not import runtime guards: {type(e).__name__}: {e}", file=sys.stderr)
        raise SystemExit(1)

    try:
        assert_prod_live_prereqs()
    except Exception as e:
        print(f"FATAL: prod-live prereqs failed: {type(e).__name__}: {e}", file=sys.stderr)
        raise SystemExit(1)

    # Keep the lock object alive for process lifetime.
    try:
        _prod_lock = acquire_prod_live_lock(exchange_name=str(_env("EXCHANGE_NAME", "kraken") or "kraken"), market_type="futures")
    except Exception as e:
        print(f"FATAL: prod-live lock not acquired: {type(e).__name__}: {e}", file=sys.stderr)
        raise SystemExit(1)

    # --- Now it is safe to import the rest of the application ---
    try:
        from src.config.config import load_config
        from src.monitoring.logger import setup_logging, get_logger
    except Exception as e:
        print(f"FATAL: could not import config/logging: {type(e).__name__}: {e}", file=sys.stderr)
        raise SystemExit(1)

    # Load config and initialize logging as early as possible (post-guards).
    try:
        config = load_config(_config_path())
    except Exception as e:
        print(f"FATAL: failed to load config: {type(e).__name__}: {e}", file=sys.stderr)
        raise SystemExit(1)

    try:
        setup_logging(config.monitoring.log_level, config.monitoring.log_format)
    except Exception as e:
        print(f"FATAL: failed to setup logging: {type(e).__name__}: {e}", file=sys.stderr)
        raise SystemExit(1)

    logger = get_logger(__name__)

    # Optional: worker-integrated health server (for platforms that require an HTTP port).
    # This is started only after prod-live guardrails and lock acquisition succeed.
    if _env_bool("WITH_HEALTH", default=False):
        try:
            import threading
            import uvicorn
            from src.health import worker_health_app

            port = int(os.environ.get("PORT", "8080"))
            host = os.environ.get("WORKER_HEALTH_HOST") or os.environ.get("HEALTH_HOST") or "0.0.0.0"

            def _run_health() -> None:
                uvicorn.run(worker_health_app, host=host, port=port, log_level="info")

            t = threading.Thread(target=_run_health, name="worker-health", daemon=True)
            t.start()
            logger.info("Worker health server started", host=host, port=port)
        except Exception as e:
            logger.critical("Failed to start worker health server", error=str(e), error_type=type(e).__name__, exc_info=True)
            raise SystemExit(1)

    # Defense-in-depth: replacement must not be enabled in prod live unless explicitly overridden.
    if bool(getattr(getattr(config, "risk", None), "replacement_enabled", False)):
        if str(_env("ALLOW_REPLACEMENT_IN_PROD", "") or "").strip().upper() != "YES":
            logger.critical(
                "PROD_LIVE_REPLACEMENT_FORBIDDEN",
                message="replacement_enabled=true is not permitted in prod live unless ALLOW_REPLACEMENT_IN_PROD=YES is set",
            )
            raise SystemExit(1)

    # Run the live engine.
    try:
        import asyncio
        from src.live.live_trading import LiveTrading

        async def _run() -> None:
            engine = LiveTrading(config)
            await engine.run()

        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("Live trading stopped by user")
        raise SystemExit(0)
    except Exception as e:
        logger.critical("Live trading crashed", error=str(e), error_type=type(e).__name__, exc_info=True)
        raise SystemExit(1)
    finally:
        # Best-effort lock release (optional; lock is session-scoped and will release on process exit).
        try:
            if _prod_lock is not None:
                _prod_lock.release()
        except Exception:
            pass


if __name__ == "__main__":
    main()

