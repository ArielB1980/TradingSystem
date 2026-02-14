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


def _apply_prod_live_safe_mode_overrides(config: object, *, enabled: bool) -> dict[str, tuple[object, object]]:
    """
    Apply conservative *effective* runtime overrides in PROD_LIVE_SAFE_MODE.

    This is intentionally localized to the production entrypoint so prod behavior is explicit.
    Returns a map of overridden fields -> (old, new) for structured logging and tests.
    """
    overrides: dict[str, tuple[object, object]] = {}
    if not enabled:
        return overrides

    # Risk: never allow replacement in safe mode.
    risk = getattr(config, "risk", None)
    if risk is not None:
        old = getattr(risk, "replacement_enabled", None)
        if old is not False:
            try:
                setattr(risk, "replacement_enabled", False)
                overrides["risk.replacement_enabled"] = (old, False)
            except (AttributeError, TypeError, ValueError):
                pass

    # Reconciliation: must stay on in safe mode (startup takeover + periodic sync).
    recon = getattr(config, "reconciliation", None)
    if recon is not None:
        old = getattr(recon, "reconcile_enabled", None)
        if old is not True:
            try:
                setattr(recon, "reconcile_enabled", True)
                overrides["reconciliation.reconcile_enabled"] = (old, True)
            except (AttributeError, TypeError, ValueError):
                pass

        # Tighten cadence (optional conservative default): clamp to <= 60s.
        old_int = getattr(recon, "periodic_interval_seconds", None)
        if isinstance(old_int, int) and old_int > 60:
            try:
                setattr(recon, "periodic_interval_seconds", 60)
                overrides["reconciliation.periodic_interval_seconds"] = (old_int, 60)
            except (AttributeError, TypeError, ValueError):
                pass

    # Execution: pyramiding off in safe mode.
    execution = getattr(config, "execution", None)
    if execution is not None:
        old = getattr(execution, "pyramiding_enabled", None)
        if old is not False:
            try:
                setattr(execution, "pyramiding_enabled", False)
                overrides["execution.pyramiding_enabled"] = (old, False)
            except (AttributeError, TypeError, ValueError):
                pass

    return overrides


def main() -> None:
    # --- Guardrails and lock BEFORE any config/runtime imports ---
    try:
        from src.runtime.guards import assert_prod_live_prereqs, acquire_prod_live_lock
    except (ImportError, OSError, ValueError, TypeError) as e:
        print(f"FATAL: could not import runtime guards: {type(e).__name__}: {e}", file=sys.stderr)
        raise SystemExit(1)

    try:
        assert_prod_live_prereqs()
    except (ImportError, OSError, ValueError, TypeError) as e:
        print(f"FATAL: prod-live prereqs failed: {type(e).__name__}: {e}", file=sys.stderr)
        raise SystemExit(1)

    # Keep the lock object alive for process lifetime.
    try:
        _prod_lock = acquire_prod_live_lock(exchange_name=str(_env("EXCHANGE_NAME", "kraken") or "kraken"), market_type="futures")
    except (ImportError, OSError, ValueError, TypeError) as e:
        print(f"FATAL: prod-live lock not acquired: {type(e).__name__}: {e}", file=sys.stderr)
        raise SystemExit(1)

    # --- Now it is safe to import the rest of the application ---
    try:
        from src.config.config import load_config
        from src.monitoring.logger import setup_logging, get_logger
    except (ImportError, OSError, ValueError, TypeError) as e:
        print(f"FATAL: could not import config/logging: {type(e).__name__}: {e}", file=sys.stderr)
        raise SystemExit(1)

    # Load config and initialize logging as early as possible (post-guards).
    try:
        config = load_config(_config_path())
    except (ImportError, OSError, ValueError, TypeError) as e:
        print(f"FATAL: failed to load config: {type(e).__name__}: {e}", file=sys.stderr)
        raise SystemExit(1)

    try:
        setup_logging(config.monitoring.log_level, config.monitoring.log_format)
    except (ImportError, OSError, ValueError, TypeError) as e:
        print(f"FATAL: failed to setup logging: {type(e).__name__}: {e}", file=sys.stderr)
        raise SystemExit(1)

    logger = get_logger(__name__)

    # PROD_LIVE_SAFE_MODE: conservative effective runtime overrides.
    prod_live_safe_mode = str(_env("PROD_LIVE_SAFE_MODE", "") or "").strip().upper() == "YES"
    safe_mode_overrides = _apply_prod_live_safe_mode_overrides(config, enabled=prod_live_safe_mode)
    if prod_live_safe_mode:
        logger.critical(
            "PROD_LIVE_SAFE_MODE_ENABLED",
            enabled=True,
            overrides={k: {"old": str(v[0]), "new": str(v[1])} for k, v in safe_mode_overrides.items()},
            replacement_enabled=bool(getattr(getattr(config, "risk", None), "replacement_enabled", False)),
            reconcile_enabled=bool(getattr(getattr(config, "reconciliation", None), "reconcile_enabled", True)),
            reconciliation_interval_seconds=getattr(getattr(config, "reconciliation", None), "periodic_interval_seconds", None),
            pyramiding_enabled=bool(getattr(getattr(config, "execution", None), "pyramiding_enabled", False)),
        )

    # Startup identity + production invariant report (prod entrypoint is source of truth).
    try:
        from src.runtime.guards import (
            account_fingerprint,
            confirm_live_env,
            is_dry_run_env,
            is_prod_live_env,
            use_state_machine_v2_env,
        )
        from src.runtime.startup_identity import sanitize_for_logging, stable_sha256_hex
        from src.config.config import CONFIG_SCHEMA_VERSION
        from src.utils.kill_switch import KillSwitch

        exchange_name = getattr(getattr(config, "exchange", None), "name", None) or "kraken"
        try:
            cfg_obj = sanitize_for_logging(config.model_dump())
            config_hash = stable_sha256_hex(cfg_obj)[:12]
        except (ValueError, TypeError, KeyError):
            config_hash = "unknown"

        git_sha = os.getenv("GIT_SHA") or os.getenv("GITHUB_SHA") or "unknown"
        strategy_id = os.getenv("STRATEGY_ID") or git_sha

        db_ident = _prod_lock.db_identity() if _prod_lock is not None else {}
        db_schema = _prod_lock.schema_fingerprint() if _prod_lock is not None else None

        logger.info(
            "STARTUP_IDENTITY",
            runtime="LiveTrading",
            pid=os.getpid(),
            env=os.getenv("ENVIRONMENT", "unknown"),
            is_prod_live=is_prod_live_env(),
            dry_run=is_dry_run_env(),
            use_state_machine_v2=use_state_machine_v2_env(),
            prod_live_safe_mode=prod_live_safe_mode,
            git_sha=git_sha,
            strategy_id=strategy_id,
            config_version=getattr(getattr(config, "system", None), "version", "unknown"),
            config_schema_version=CONFIG_SCHEMA_VERSION,
            config_hash=config_hash,
            exchange=str(exchange_name),
            market_type="futures",
            account_fingerprint=account_fingerprint(),
            lock_key_short=getattr(_prod_lock, "lock_key_short", None),
            db_host=db_ident.get("db_host"),
            db_port=db_ident.get("db_port"),
            db_name=db_ident.get("db_name"),
            db_user=db_ident.get("db_user"),
            db_schema_hash=db_schema or "unknown",
            replacement_enabled=bool(getattr(getattr(config, "risk", None), "replacement_enabled", False)),
        )

        # Prod invariant report (best-effort visibility; fail-closed is enforced elsewhere).
        db_reachable = False
        try:
            if _prod_lock is not None:
                _prod_lock.ping()
                db_reachable = True
        except (OSError, ConnectionError, RuntimeError):
            db_reachable = False

        try:
            ks_status = KillSwitch().get_status()
        except (OSError, ValueError, RuntimeError):
            ks_status = {"active": None, "latched": None, "reason": None}

        logger.critical(
            "PROD_INVARIANT_REPORT",
            lock_acquired=bool(_prod_lock is not None),
            lock_key_short=getattr(_prod_lock, "lock_key_short", None),
            confirm_live=confirm_live_env(),
            v2_enabled=use_state_machine_v2_env(),
            dotenv_policy="disabled_in_prod",
            kill_switch_active=ks_status.get("active"),
            kill_switch_latched=ks_status.get("latched"),
            db_reachable=db_reachable,
            exchange_reachable="unknown_pre_runtime",
            time_sync="unknown_best_effort",
        )
    except Exception as e:
        logger.critical(
            "STARTUP_IDENTITY_FAILED",
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        raise SystemExit(1)

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
        except (OSError, ValueError, ImportError) as e:
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
        except (OSError, RuntimeError):
            pass


if __name__ == "__main__":
    main()

