"""
DEPRECATED: main_with_health has been removed.

Production uses: python -m src.entrypoints.prod_live (or run.py live --force)
Health API: python -m src.health

See docs/PRODUCTION_RUNTIME.md and docs/DEPLOYMENT_WORKER_RUNCOMMAND.md.
"""
import sys

if __name__ == "__main__":
    print(
        "main_with_health has been removed. Use:\n"
        "  Production: python -m src.entrypoints.prod_live (or run.py live --force)\n"
        "  Health:     python -m src.health\n"
        "See docs/PRODUCTION_RUNTIME.md",
        file=sys.stderr,
    )
    sys.exit(1)
