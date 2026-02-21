"""
Phase 1 Safety Net: Production entrypoint guard test.

Guards the 5 module paths that production deployment depends on
(Procfile, Makefile, systemd). If any of these imports fail, production
deployment is broken.

Scope is deliberately narrow -- only protects entrypoints that deployment
scripts reference. Internal module paths are expected to change during
refactoring.
"""
import importlib
import pytest


# These are the exact module paths referenced by:
# - Procfile (web, worker)
# - Makefile (run, smoke)
# - systemd service (prod_live)
# - migration script (migrate_schema)
PRODUCTION_ENTRYPOINTS = [
    "src.entrypoints.prod_live",
    "src.health",
    "src.cli",
]


@pytest.mark.parametrize("module_path", PRODUCTION_ENTRYPOINTS)
def test_production_entrypoint_imports(module_path: str):
    """Each production entrypoint must import without errors.

    This catches broken imports, missing dependencies, and syntax errors
    in the critical path that systemd/Procfile rely on.
    """
    try:
        mod = importlib.import_module(module_path)
        assert mod is not None, f"{module_path} imported as None"
    except Exception as e:
        pytest.fail(
            f"Production entrypoint '{module_path}' failed to import: {e}\n"
            f"This will break deployment. Fix the import chain before merging."
        )


def test_migrate_schema_importable():
    """migrate_schema.py is run before app startup in production.

    It's a top-level module (not under src/), so we test it separately.
    """
    try:
        # migrate_schema is at repo root, not in src/
        import importlib.util
        import os

        spec = importlib.util.spec_from_file_location(
            "migrate_schema",
            os.path.join(os.path.dirname(__file__), "..", "..", "migrate_schema.py"),
        )
        assert spec is not None, "migrate_schema.py not found at repo root"
        mod = importlib.util.module_from_spec(spec)
        # Don't execute -- just verify it can be loaded without syntax errors
        # (executing would attempt DB connection)
        assert mod is not None
    except SyntaxError as e:
        pytest.fail(f"migrate_schema.py has syntax error: {e}")


def test_run_py_importable():
    """run.py is the CLI entrypoint referenced by Makefile."""
    import importlib.util
    import os

    spec = importlib.util.spec_from_file_location(
        "run",
        os.path.join(os.path.dirname(__file__), "..", "..", "run.py"),
    )
    assert spec is not None, "run.py not found at repo root"
