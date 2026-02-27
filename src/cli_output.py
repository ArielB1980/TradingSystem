"""
Shared CLI output helpers for fatal error reporting.
"""
from __future__ import annotations

import sys
import traceback


def print_critical_error(title: str, error: Exception, *, include_type: bool = True) -> None:
    print("=" * 80, file=sys.stderr)
    print(f"CRITICAL ERROR - {title}", file=sys.stderr)
    print("=" * 80, file=sys.stderr)
    print(f"Error: {error}", file=sys.stderr)
    if include_type:
        print(f"Type: {type(error).__name__}", file=sys.stderr)
    traceback.print_exc(file=sys.stderr)
    print("=" * 80, file=sys.stderr)
