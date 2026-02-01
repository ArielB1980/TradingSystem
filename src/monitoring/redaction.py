"""
Log redaction helpers.

Designed for structlog processors.
"""

from __future__ import annotations

from typing import Any


SENSITIVE_KEY_FRAGMENTS = (
    "key",
    "secret",
    "token",
    "password",
    "authorization",
    "signature",
)


def _is_sensitive_key(key: str) -> bool:
    k = str(key).lower()
    return any(frag in k for frag in SENSITIVE_KEY_FRAGMENTS)


def redact(obj: Any) -> Any:
    """
    Recursively redact dict keys that look sensitive.
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if _is_sensitive_key(k):
                out[k] = "***REDACTED***"
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    return obj


def structlog_redaction_processor(_logger: Any, _method_name: str, event_dict: dict) -> dict:
    """
    Structlog processor: redact sensitive fields from event_dict.
    """
    return redact(event_dict)

