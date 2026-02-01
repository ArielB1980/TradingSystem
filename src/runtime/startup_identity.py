"""
Startup identity helpers (config hashing, env fingerprinting).

Keep this dependency-light: it should not import exchange clients or run-time loops.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


_SENSITIVE_KEY_FRAGMENTS = ("key", "secret", "token", "password", "authorization", "signature")


def _is_sensitive_key(k: str) -> bool:
    kk = str(k).lower()
    return any(frag in kk for frag in _SENSITIVE_KEY_FRAGMENTS)


def sanitize_for_logging(obj: Any) -> Any:
    """
    Recursively sanitize nested dict/list structures by redacting sensitive-looking keys.
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if _is_sensitive_key(k):
                out[k] = "***REDACTED***"
            else:
                out[k] = sanitize_for_logging(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [sanitize_for_logging(v) for v in obj]
    return obj


def stable_sha256_hex(obj: Any) -> str:
    """
    Compute a stable sha256 hash for an object by JSON-serializing with sorted keys.
    """
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

