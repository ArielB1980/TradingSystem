from types import SimpleNamespace

from src.live.live_trading import _resolve_signal_cooldown_params


def test_signal_cooldown_resolver_uses_base_values_without_canary():
    cfg = SimpleNamespace(
        signal_cooldown_hours=4.0,
        signal_cooldown_canary_enabled=False,
        signal_cooldown_canary_symbols=["BTC/USD"],
        signal_cooldown_hours_canary=1.0,
    )
    params = _resolve_signal_cooldown_params(cfg, "BTC/USD")
    assert params["cooldown_hours"] == 4.0
    assert params["canary_applied"] is False


def test_signal_cooldown_resolver_applies_canary_for_matching_symbol():
    cfg = SimpleNamespace(
        signal_cooldown_hours=4.0,
        signal_cooldown_canary_enabled=True,
        signal_cooldown_canary_symbols=["BTC/USD", "ETH/USD"],
        signal_cooldown_hours_canary=1.0,
    )
    params = _resolve_signal_cooldown_params(cfg, "PF_BTCUSD")
    assert params["cooldown_hours"] == 1.0
    assert params["canary_applied"] is True


def test_signal_cooldown_resolver_keeps_base_for_non_canary_symbol():
    cfg = SimpleNamespace(
        signal_cooldown_hours=4.0,
        signal_cooldown_canary_enabled=True,
        signal_cooldown_canary_symbols=["BTC/USD", "ETH/USD"],
        signal_cooldown_hours_canary=1.0,
    )
    params = _resolve_signal_cooldown_params(cfg, "SOL/USD")
    assert params["cooldown_hours"] == 4.0
    assert params["canary_applied"] is False
