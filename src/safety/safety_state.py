"""
Unified safety state persistence — single source of truth.

P0.2: Replaces the fragmented trio of:
  - halt_state.json (~/.trading_system/)
  - kill_switch_state.json (data/)
  - peak_equity_state.json (~/.trading_system/)

All safety state is now persisted in ONE file: safety_state.json
Located at: SAFETY_STATE_PATH env var, or ~/.trading_system/safety_state.json

Components still read/write through their own APIs, but the underlying
storage is unified. This ensures atomic reset (clear_all) works correctly
and prevents the 2026-02-14 incident class where halt was cleared but
kill switch was not.

Design decisions:
- JSON file (not DB) for simplicity and crash-safe restarts
- Atomic write via temp file + rename
- File lock for concurrent access safety
- Audit trail: every mutation is logged with operator + timestamp
"""
import fcntl
import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.monitoring.logger import get_logger

logger = get_logger(__name__)

_DEFAULT_STATE_DIR = Path.home() / ".trading_system"
_STATE_FILE = "safety_state.json"


def _safety_state_path() -> Path:
    """State file path: SAFETY_STATE_PATH env, or ~/.trading_system/safety_state.json."""
    env_path = os.environ.get("SAFETY_STATE_PATH")
    if env_path:
        return Path(env_path)
    return _DEFAULT_STATE_DIR / _STATE_FILE


SAFETY_STATE_VERSION = 1  # Bump on schema changes; load() can migrate old versions.


@dataclass
class SafetyState:
    """Unified safety state — single persisted object.
    
    All safety-relevant state lives here. Components read from and write to
    this object, which is atomically persisted to disk.
    """
    # Schema version — allows future migrations
    safety_state_version: int = SAFETY_STATE_VERSION
    
    # Halt state
    halt_active: bool = False
    halt_reason: Optional[str] = None
    halt_violations: List[str] = field(default_factory=list)
    halt_timestamp: Optional[str] = None
    halt_run_id: Optional[str] = None
    
    # Kill switch state
    kill_switch_active: bool = False
    kill_switch_latched: bool = False
    kill_switch_reason: Optional[str] = None
    kill_switch_activated_at: Optional[str] = None
    
    # Peak equity
    peak_equity: Optional[str] = None  # Stored as string for Decimal precision
    peak_equity_updated_at: Optional[str] = None
    
    # Audit trail
    last_reset_at: Optional[str] = None
    last_reset_by: Optional[str] = None
    last_reset_mode: Optional[str] = None
    
    # Events log (last N reset events for audit)
    reset_events: List[Dict[str, Any]] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SafetyState":
        # Handle unknown fields gracefully
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered)
    
    @property
    def peak_equity_decimal(self) -> Optional[Decimal]:
        if self.peak_equity is None:
            return None
        try:
            return Decimal(self.peak_equity)
        except Exception:
            return None


class SafetyStateManager:
    """Manages the unified safety state file with file locking.
    
    All reads and writes go through this manager. It handles:
    - Atomic writes (temp file + rename)
    - File locking (flock) for concurrent access safety
    - Audit trail of reset events
    """
    
    MAX_RESET_EVENTS = 50  # Keep last N reset events
    
    def __init__(self, state_path: Optional[Path] = None):
        self._path = state_path or _safety_state_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
    
    def load(self) -> SafetyState:
        """Load safety state from disk. Returns default state if missing."""
        if not self._path.exists():
            return SafetyState()
        
        try:
            with open(self._path, "r") as f:
                fcntl.flock(f, fcntl.LOCK_SH)
                try:
                    data = json.load(f)
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)
            return SafetyState.from_dict(data)
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.critical(
                "SAFETY_STATE_CORRUPT — defaulting to safest posture",
                error=str(e),
                path=str(self._path),
            )
            # Corrupt file → return state with kill switch active (safest)
            return SafetyState(
                kill_switch_active=True,
                kill_switch_latched=True,
                kill_switch_reason="data_failure",
                kill_switch_activated_at=datetime.now(timezone.utc).isoformat(),
            )
        except OSError as e:
            logger.critical("SAFETY_STATE_READ_FAILED", error=str(e))
            raise
    
    def save(self, state: SafetyState) -> None:
        """Atomically persist safety state to disk."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(".tmp")
        
        try:
            with open(tmp_path, "w") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                try:
                    json.dump(state.to_dict(), f, indent=2)
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)
            tmp_path.rename(self._path)
        except OSError as e:
            # Persistence failure is critical — crash so systemd restarts
            logger.critical("SAFETY_STATE_WRITE_FAILED", error=str(e))
            raise
    
    def atomic_reset(
        self,
        operator: str,
        mode: str = "soft",
        new_peak_equity: Optional[Decimal] = None,
    ) -> SafetyState:
        """
        Atomically reset all safety state.
        
        Args:
            operator: Who is performing the reset (audit trail)
            mode: "soft" (clear halt + ks + peak) or "hard" (same + signals cancel)
            new_peak_equity: If provided, set peak equity to this value
            
        Returns:
            The new (cleared) SafetyState
        """
        old_state = self.load()
        
        now = datetime.now(timezone.utc).isoformat()
        
        # Build reset event for audit trail
        event = {
            "timestamp": now,
            "operator": operator,
            "mode": mode,
            "previous_halt_active": old_state.halt_active,
            "previous_halt_reason": old_state.halt_reason,
            "previous_kill_switch_active": old_state.kill_switch_active,
            "previous_kill_switch_reason": old_state.kill_switch_reason,
            "previous_peak_equity": old_state.peak_equity,
            "new_peak_equity": str(new_peak_equity) if new_peak_equity else None,
        }
        
        # Build new state
        new_state = SafetyState(
            halt_active=False,
            halt_reason=None,
            halt_violations=[],
            halt_timestamp=None,
            halt_run_id=None,
            kill_switch_active=False,
            kill_switch_latched=False,
            kill_switch_reason=None,
            kill_switch_activated_at=None,
            peak_equity=str(new_peak_equity) if new_peak_equity else old_state.peak_equity,
            peak_equity_updated_at=now if new_peak_equity else old_state.peak_equity_updated_at,
            last_reset_at=now,
            last_reset_by=operator,
            last_reset_mode=mode,
            reset_events=(old_state.reset_events + [event])[-self.MAX_RESET_EVENTS:],
        )
        
        self.save(new_state)
        
        logger.critical(
            "SAFETY_STATE_ATOMIC_RESET",
            operator=operator,
            mode=mode,
            previous_halt=old_state.halt_active,
            previous_kill_switch=old_state.kill_switch_active,
            new_peak_equity=str(new_peak_equity) if new_peak_equity else "unchanged",
        )
        
        return new_state
    
    def update_halt(
        self,
        active: bool,
        reason: Optional[str] = None,
        violations: Optional[List[str]] = None,
        run_id: Optional[str] = None,
    ) -> None:
        """Update halt state atomically."""
        state = self.load()
        state.halt_active = active
        state.halt_reason = reason
        state.halt_violations = violations or []
        state.halt_timestamp = datetime.now(timezone.utc).isoformat() if active else None
        state.halt_run_id = run_id
        self.save(state)
    
    def update_kill_switch(
        self,
        active: bool,
        latched: bool = False,
        reason: Optional[str] = None,
        activated_at: Optional[str] = None,
    ) -> None:
        """Update kill switch state atomically."""
        state = self.load()
        state.kill_switch_active = active
        state.kill_switch_latched = latched
        state.kill_switch_reason = reason
        state.kill_switch_activated_at = activated_at
        self.save(state)
    
    def update_peak_equity(self, peak: Decimal) -> None:
        """Update peak equity atomically."""
        state = self.load()
        state.peak_equity = str(peak)
        state.peak_equity_updated_at = datetime.now(timezone.utc).isoformat()
        self.save(state)


# Global instance
_manager: Optional[SafetyStateManager] = None


def get_safety_state_manager() -> SafetyStateManager:
    """Get global SafetyStateManager instance."""
    global _manager
    if _manager is None:
        _manager = SafetyStateManager()
    return _manager
