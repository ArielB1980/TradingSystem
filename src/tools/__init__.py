"""
Promoted operational tools — safe-by-default utilities for system management.

Every tool here follows these conventions:
  1. --dry-run is the DEFAULT (no accidental live execution)
  2. --execute flag required to take real action
  3. Refuses to run with live API keys unless I_UNDERSTAND_LIVE=1 env var is set
  4. Clear output of what WOULD happen vs what DID happen
"""
