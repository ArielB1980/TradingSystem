from decimal import Decimal
import json

from src.tools.safety_reset import _sync_legacy_peak_equity


def test_sync_legacy_peak_equity_writes_file(monkeypatch, tmp_path):
    peak_path = tmp_path / "peak_equity_state.json"
    monkeypatch.setenv("PEAK_EQUITY_STATE_PATH", str(peak_path))

    _sync_legacy_peak_equity(Decimal("335.7245577254131"))

    assert peak_path.exists()
    payload = json.loads(peak_path.read_text())
    assert payload["peak_equity"] == "335.7245577254131"
    assert "updated_at" in payload

