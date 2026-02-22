import os
import subprocess
import sys
import textwrap
from pathlib import Path


def test_config_import_has_no_dotenv_side_effects():
    """
    Importing src.config.config must not load dotenv files.

    We enforce this by injecting a fake `dotenv` module whose `load_dotenv` would abort
    the process if called.
    """
    repo_root = Path(__file__).resolve().parent.parent.parent

    code = textwrap.dedent(
        """
        import dotenv

        def load_dotenv(*args, **kwargs):
            raise SystemExit("DOTENV_CALLED")

        # If our code tries to load dotenv at import time, abort the process.
        dotenv.load_dotenv = load_dotenv

        import src.config.config
        print("OK")
        """
    ).strip()

    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_root)

    res = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
    )

    assert res.returncode == 0, f"stdout={res.stdout}\nstderr={res.stderr}"
    assert "OK" in (res.stdout or "")

