"""
Safety module for production trading.

Contains hard invariant enforcement and system state management.

V2 additions: HardeningDecision enum, HardeningGateError, PersistedHaltState
"""
from src.safety.invariant_monitor import (
    InvariantMonitor,
    SystemInvariants,
    SystemState,
    InvariantViolation,
    get_invariant_monitor,
    init_invariant_monitor,
)
from src.safety.integration import (
    ProductionHardeningLayer,
    init_hardening_layer,
    get_hardening_layer,
    HardeningDecision,
    HardeningGateError,
    PersistedHaltState,
)

__all__ = [
    # Invariant Monitor
    "InvariantMonitor",
    "SystemInvariants",
    "SystemState",
    "InvariantViolation",
    "get_invariant_monitor",
    "init_invariant_monitor",
    # Integration (V2)
    "ProductionHardeningLayer",
    "init_hardening_layer",
    "get_hardening_layer",
    "HardeningDecision",
    "HardeningGateError",
    "PersistedHaltState",
]
