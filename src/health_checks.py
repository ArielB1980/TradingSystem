"""
Shared health/connectivity checks used by health endpoints.
"""
from __future__ import annotations

from typing import Dict, Tuple

from src.exceptions import OperationalError, DataError


def check_required_secrets_and_db() -> Tuple[str, Dict[str, dict], str]:
    """
    Return database status, per-secret availability, and environment string.
    """
    from src.utils.secret_manager import check_secret_availability, get_environment

    required_secrets = [
        "DATABASE_URL",
        "KRAKEN_FUTURES_API_KEY",
        "KRAKEN_FUTURES_API_SECRET",
    ]
    secrets: Dict[str, dict] = {}
    for secret in required_secrets:
        is_available, error_msg = check_secret_availability(secret)
        secrets[secret] = {
            "available": is_available,
            "error": error_msg if not is_available else None,
        }

    database = "missing"
    db_available, _ = check_secret_availability("DATABASE_URL")
    if db_available:
        database = "configured"
        try:
            from src.storage.db import get_db
            from sqlalchemy import text

            db = get_db()
            with db.get_session() as session:
                session.execute(text("SELECT 1;"))
            database = "connected"
        except (OperationalError, DataError, OSError) as e:
            database = f"error: {str(e)[:80]}"

    return database, secrets, get_environment()


def quick_connectivity_snapshot() -> dict:
    """
    Compact connectivity snapshot for /api/quick-test.
    """
    from src.utils.secret_manager import check_secret_availability

    database, secrets, environment = check_required_secrets_and_db()
    has_spot_key, _ = check_secret_availability("KRAKEN_API_KEY")
    has_spot_secret, _ = check_secret_availability("KRAKEN_API_SECRET")
    has_spot = bool(has_spot_key and has_spot_secret)
    has_futures = bool(
        secrets.get("KRAKEN_FUTURES_API_KEY", {}).get("available")
        and secrets.get("KRAKEN_FUTURES_API_SECRET", {}).get("available")
    )

    api_keys = "not_configured"
    if has_spot and has_futures:
        api_keys = "spot_and_futures_configured"
    elif has_futures:
        api_keys = "futures_only"
    elif has_spot:
        api_keys = "spot_configured"

    return {
        "database": database,
        "api_keys": api_keys,
        "environment": environment,
        "status": "ok" if database == "connected" else "issues",
    }
