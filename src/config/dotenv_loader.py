"""
Explicit dotenv loader.

Rules:
- In production (`ENVIRONMENT=prod`): do not load `.env` / `.env.local`.
- Otherwise: load `.env` then `.env.local` (local overrides).

This must remain dependency-light and MUST NOT import `src.config.config`.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


def _env(name: str, default: str | None = None) -> str | None:
    v = os.getenv(name)
    return v if v is not None else default


def _is_prod_env() -> bool:
    return str(_env("ENVIRONMENT", "prod") or "prod").strip().lower() == "prod"


def load_dotenv_files(*, repo_root: Path | None = None) -> None:
    """
    Load dotenv files for local/dev usage.

    In prod, this is a no-op by design.
    """
    if _is_prod_env():
        return

    root = repo_root or Path(__file__).resolve().parent.parent.parent
    env_path = root / ".env"
    env_local_path = root / ".env.local"

    # Load base .env first (if present)
    if env_path.exists():
        load_dotenv(dotenv_path=env_path, override=False)

    # Load .env.local second (override for local convenience)
    if env_local_path.exists():
        load_dotenv(dotenv_path=env_local_path, override=True)

