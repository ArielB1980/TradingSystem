"""
Invariant: Live/backfill TP placement funnels through a single path (update_protective_orders)
so that contract sizing, step quantize, and venue min filter apply. No caller under src/ may
invoke place_protective_orders (legacy single-TP placer); that would reintroduce dust/notional paths.
Allowlist: only executor.py may contain the name (its definition); tests may reference it.
"""
import pathlib


def test_no_src_calls_place_protective_orders():
    """No file under src/ may call place_protective_orders; only executor.py may define it (grep-based invariant)."""
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    src_dir = repo_root / "src"
    if not src_dir.exists():
        return
    violators = []
    for py_path in src_dir.rglob("*.py"):
        try:
            text = py_path.read_text()
        except Exception:
            continue
        rel = str(py_path.relative_to(repo_root))
        for i, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "place_protective_orders(" not in line:
                continue
            # executor.py is allowed only for the definition line
            if rel == "src/execution/executor.py" and "def place_protective_orders" in line:
                continue
            violators.append((rel, i, line.strip()[:80]))
            break
    assert not violators, (
        "Only executor.py may define place_protective_orders; no src module may call it. "
        "TP placement must go through protection_ops.place_tp_backfill -> executor.update_protective_orders. "
        "Violators: " + str(violators)
    )
