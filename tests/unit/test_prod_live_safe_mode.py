import os


def test_prod_live_safe_mode_overrides_force_conservative_effective_values(monkeypatch):
    # Avoid production fail-fast checks inside load_config()
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("DRY_RUN", "1")

    from src.config.config import load_config
    from src.entrypoints.prod_live import _apply_prod_live_safe_mode_overrides

    config = load_config("src/config/config.yaml")

    # Set unsafe values first (simulate a risky config drift).
    config.risk.replacement_enabled = True
    config.reconciliation.reconcile_enabled = False
    config.reconciliation.periodic_interval_seconds = 300
    config.execution.pyramiding_enabled = True

    overrides = _apply_prod_live_safe_mode_overrides(config, enabled=True)

    assert config.risk.replacement_enabled is False
    assert config.reconciliation.reconcile_enabled is True
    assert config.reconciliation.periodic_interval_seconds <= 60
    assert config.execution.pyramiding_enabled is False

    assert overrides.get("risk.replacement_enabled") == (True, False)
    assert overrides.get("reconciliation.reconcile_enabled") == (False, True)
    assert overrides.get("reconciliation.periodic_interval_seconds") == (300, 60)
    assert overrides.get("execution.pyramiding_enabled") == (True, False)

