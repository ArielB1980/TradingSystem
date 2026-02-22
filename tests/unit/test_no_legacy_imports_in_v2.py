import os
import subprocess
import sys
import textwrap
from pathlib import Path


def test_live_trading_module_does_not_import_legacy_position_manager_when_v2_enabled():
    repo_root = Path(__file__).resolve().parent.parent.parent

    code = textwrap.dedent(
        """
        import os, sys
        os.environ["ENVIRONMENT"] = "prod"
        os.environ["DRY_RUN"] = "1"
        os.environ["USE_STATE_MACHINE_V2"] = "true"

        import src.live.live_trading  # noqa: F401

        assert "src.execution.position_manager" not in sys.modules, "legacy PositionManager imported"
        print("OK")
        """
    ).strip()

    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_root)
    env["ENVIRONMENT"] = "prod"
    env["DRY_RUN"] = "1"
    env["USE_STATE_MACHINE_V2"] = "true"

    res = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
    )

    assert res.returncode == 0, f"stdout={res.stdout}\nstderr={res.stderr}"
    assert "OK" in (res.stdout or "")

