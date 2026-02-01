"""
Safety configuration loader.

Loads and validates safety invariant configuration from YAML.
"""
import os
from decimal import Decimal
from pathlib import Path
from typing import Optional
import yaml

from src.safety.invariant_monitor import SystemInvariants
from src.monitoring.logger import get_logger

logger = get_logger(__name__)


def load_safety_config(config_path: Optional[str] = None, environment: Optional[str] = None) -> dict:
    """
    Load safety configuration from YAML file.
    
    Args:
        config_path: Path to safety.yaml. If None, uses default location.
        environment: Environment name (prod, paper, dev, local). 
                    If None, reads from ENVIRONMENT env var.
    
    Returns:
        Merged configuration dict with environment-specific overrides applied.
    """
    if config_path is None:
        config_path = Path(__file__).parent / "safety.yaml"
    else:
        config_path = Path(config_path)
    
    if not config_path.exists():
        logger.warning(f"Safety config not found at {config_path}, using defaults")
        return {"safety": {}}
    
    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)
    
    # Get environment
    if environment is None:
        environment = os.getenv("ENVIRONMENT", os.getenv("ENV", "local")).lower()
    
    # Apply environment-specific overrides
    base_safety = config.get("safety", {})
    env_overrides = config.get("environments", {}).get(environment, {})
    
    # Deep merge overrides
    merged = _deep_merge(base_safety, env_overrides)
    
    logger.info(
        "Safety config loaded",
        environment=environment,
        invariants_source="environment_override" if env_overrides else "base",
    )
    
    return {"safety": merged, "environment": environment}


def _deep_merge(base: dict, override: dict) -> dict:
    """Deep merge two dicts, with override taking precedence."""
    result = base.copy()
    
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    
    return result


def create_system_invariants(config: Optional[dict] = None) -> SystemInvariants:
    """
    Create SystemInvariants from configuration.
    
    Args:
        config: Safety config dict. If None, loads from file.
    
    Returns:
        SystemInvariants object with configured thresholds.
    """
    if config is None:
        config = load_safety_config()
    
    inv_config = config.get("safety", {}).get("invariants", {})
    
    # Convert values with defaults
    return SystemInvariants(
        max_equity_drawdown_pct=Decimal(str(inv_config.get("max_equity_drawdown_pct", 0.15))),
        min_equity_floor_usd=Decimal(str(inv_config["min_equity_floor_usd"])) if inv_config.get("min_equity_floor_usd") else None,
        max_open_notional_usd=Decimal(str(inv_config.get("max_open_notional_usd", 500000))),
        max_concurrent_positions=inv_config.get("max_concurrent_positions", 10),
        max_margin_utilization_pct=Decimal(str(inv_config.get("max_margin_utilization_pct", 0.85))),
        max_single_position_pct_equity=Decimal(str(inv_config.get("max_single_position_pct_equity", 0.25))),
        max_rejected_orders_per_cycle=inv_config.get("max_rejected_orders_per_cycle", 5),
        max_api_errors_per_minute=inv_config.get("max_api_errors_per_minute", 10),
        max_latency_ms=inv_config.get("max_latency_ms", 5000),
        degraded_equity_drawdown_pct=Decimal(str(inv_config.get("degraded_equity_drawdown_pct", 0.10))),
        degraded_margin_utilization_pct=Decimal(str(inv_config.get("degraded_margin_utilization_pct", 0.70))),
        degraded_concurrent_positions=inv_config.get("degraded_concurrent_positions", 8),
    )


def get_cycle_guard_config(config: Optional[dict] = None) -> dict:
    """
    Get CycleGuard configuration.
    
    Args:
        config: Safety config dict. If None, loads from file.
    
    Returns:
        Dict with cycle guard settings.
    """
    if config is None:
        config = load_safety_config()
    
    cg_config = config.get("safety", {}).get("cycle_guard", {})
    
    return {
        "min_cycle_interval_seconds": cg_config.get("min_cycle_interval_seconds", 60),
        "max_cycle_duration_seconds": cg_config.get("max_cycle_duration_seconds", 300),
        "max_candle_age_seconds": cg_config.get("max_candle_age_seconds", 120),
        "max_clock_skew_seconds": cg_config.get("max_clock_skew_seconds", 30),
    }


def get_reconciliation_config(config: Optional[dict] = None) -> dict:
    """
    Get reconciliation configuration.
    
    Args:
        config: Safety config dict. If None, loads from file.
    
    Returns:
        Dict with reconciliation settings.
    """
    if config is None:
        config = load_safety_config()
    
    rec_config = config.get("safety", {}).get("reconciliation", {})
    
    return {
        "min_delta_threshold_usd": Decimal(str(rec_config.get("min_delta_threshold_usd", 10))),
        "max_delta_per_order_usd": Decimal(str(rec_config.get("max_delta_per_order_usd", 50000))),
        "max_delta_pct_of_position": Decimal(str(rec_config.get("max_delta_pct_of_position", 0.5))),
    }


def get_audit_config(config: Optional[dict] = None) -> dict:
    """
    Get decision audit configuration.
    
    Args:
        config: Safety config dict. If None, loads from file.
    
    Returns:
        Dict with audit settings.
    """
    if config is None:
        config = load_safety_config()
    
    audit_config = config.get("safety", {}).get("audit", {})
    
    return {
        "max_buffer_size": audit_config.get("max_buffer_size", 100),
        "enable_detailed_logging": audit_config.get("enable_detailed_logging", True),
    }


def log_safety_config_summary(config: Optional[dict] = None):
    """Log a summary of the current safety configuration."""
    if config is None:
        config = load_safety_config()
    
    invariants = config.get("safety", {}).get("invariants", {})
    
    logger.info(
        "SAFETY_CONFIG_SUMMARY",
        environment=config.get("environment", "unknown"),
        max_drawdown=invariants.get("max_equity_drawdown_pct"),
        max_notional=invariants.get("max_open_notional_usd"),
        max_positions=invariants.get("max_concurrent_positions"),
        max_margin=invariants.get("max_margin_utilization_pct"),
    )
