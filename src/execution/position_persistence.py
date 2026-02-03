"""
Position State Persistence.

SQLite-based persistence for crash recovery with:
1. positions table - current position state
2. position_fills table - fill history
3. position_actions table - audit log

Recovery algorithm:
1. Load last known positions + actions
2. Query exchange open orders + open positions
3. Reconcile → mark inconsistent as ORPHANED
"""
import sqlite3
import json
import threading
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, List, Dict
from pathlib import Path

from src.execution.position_state_machine import (
    ManagedPosition,
    PositionRegistry,
    PositionState,
    ExitReason,
    FillRecord,
    set_position_registry
)
from src.domain.models import Side
from src.monitoring.logger import get_logger

logger = get_logger(__name__)


class PositionPersistence:
    """
    SQLite persistence for position state machine.
    """
    
    def __init__(self, db_path: str = "data/positions.db"):
        """Initialize persistence with database path."""
        self.db_path = db_path
        self._local = threading.local()
        
        # Ensure directory exists
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        
        # Initialize schema
        self._init_schema()
    
    @property
    def _conn(self) -> sqlite3.Connection:
        """Get thread-local connection."""
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(self.db_path)
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn
    
    def _init_schema(self) -> None:
        """Initialize database schema."""
        with self._conn:
            self._conn.executescript("""
                -- Positions table
                CREATE TABLE IF NOT EXISTS positions (
                    position_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    state TEXT NOT NULL,
                    
                    -- Immutable entry parameters
                    initial_size TEXT NOT NULL,
                    initial_entry_price TEXT NOT NULL,
                    initial_stop_price TEXT NOT NULL,
                    initial_tp1_price TEXT,
                    initial_tp2_price TEXT,
                    initial_final_target TEXT,
                    
                    -- Current state
                    current_stop_price TEXT,
                    entry_acknowledged INTEGER DEFAULT 0,
                    tp1_filled INTEGER DEFAULT 0,
                    tp2_filled INTEGER DEFAULT 0,
                    break_even_triggered INTEGER DEFAULT 0,
                    trailing_active INTEGER DEFAULT 0,
                    
                    -- Exit tracking
                    exit_reason TEXT,
                    exit_time TEXT,
                    
                    -- Order tracking
                    entry_order_id TEXT,
                    stop_order_id TEXT,
                    pending_exit_order_id TEXT,
                    
                    -- Metadata
                    setup_type TEXT,
                    regime TEXT,
                    trade_type TEXT,
                    intent_confirmed INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    
                    -- Event idempotency
                    processed_event_hashes TEXT DEFAULT '[]'
                );
                
                CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol);
                CREATE INDEX IF NOT EXISTS idx_positions_state ON positions(state);
                
                -- Position fills table
                CREATE TABLE IF NOT EXISTS position_fills (
                    fill_id TEXT PRIMARY KEY,
                    position_id TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    qty TEXT NOT NULL,
                    price TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    is_entry INTEGER NOT NULL,
                    
                    FOREIGN KEY (position_id) REFERENCES positions(position_id)
                );
                
                CREATE INDEX IF NOT EXISTS idx_fills_position ON position_fills(position_id);
                
                -- Position actions audit log
                CREATE TABLE IF NOT EXISTS position_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    position_id TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    payload_json TEXT,
                    status TEXT DEFAULT 'executed',
                    timestamp TEXT NOT NULL,
                    
                    FOREIGN KEY (position_id) REFERENCES positions(position_id)
                );
                
                CREATE INDEX IF NOT EXISTS idx_actions_position ON position_actions(position_id);
                CREATE INDEX IF NOT EXISTS idx_actions_timestamp ON position_actions(timestamp);
                
                -- Pending reversals
                CREATE TABLE IF NOT EXISTS pending_reversals (
                    symbol TEXT PRIMARY KEY,
                    new_side TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                
                -- Action intents (Write-Ahead Log for crash recovery)
                -- Persisted BEFORE sending to exchange to prevent duplicate orders
                CREATE TABLE IF NOT EXISTS action_intents (
                    intent_id TEXT PRIMARY KEY,
                    position_id TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    size TEXT NOT NULL,
                    price TEXT,
                    created_at TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    exchange_order_id TEXT,
                    error TEXT
                );
                
                CREATE INDEX IF NOT EXISTS idx_intents_status ON action_intents(status);
                CREATE INDEX IF NOT EXISTS idx_intents_position ON action_intents(position_id);
            """)
            try:
                self._conn.execute(
                    "ALTER TABLE positions ADD COLUMN intent_confirmed INTEGER DEFAULT 0"
                )
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise
    
    # ========== POSITION CRUD ==========
    
    def save_position(self, position: ManagedPosition) -> None:
        """Save or update position."""
        with self._conn:
            self._conn.execute("""
                INSERT OR REPLACE INTO positions (
                    position_id, symbol, side, state,
                    initial_size, initial_entry_price, initial_stop_price,
                    initial_tp1_price, initial_tp2_price, initial_final_target,
                    current_stop_price, entry_acknowledged,
                    tp1_filled, tp2_filled, break_even_triggered, trailing_active,
                    exit_reason, exit_time,
                    entry_order_id, stop_order_id, pending_exit_order_id,
                    setup_type, regime, trade_type, intent_confirmed,
                    created_at, updated_at,
                    processed_event_hashes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                position.position_id,
                position.symbol,
                position.side.value,
                position.state.value,
                str(position.initial_size),
                str(position.initial_entry_price),
                str(position.initial_stop_price),
                str(position.initial_tp1_price) if position.initial_tp1_price else None,
                str(position.initial_tp2_price) if position.initial_tp2_price else None,
                str(position.initial_final_target) if position.initial_final_target else None,
                str(position.current_stop_price) if position.current_stop_price else None,
                1 if position.entry_acknowledged else 0,
                1 if position.tp1_filled else 0,
                1 if position.tp2_filled else 0,
                1 if position.break_even_triggered else 0,
                1 if position.trailing_active else 0,
                position.exit_reason.value if position.exit_reason else None,
                position.exit_time.isoformat() if position.exit_time else None,
                position.entry_order_id,
                position.stop_order_id,
                position.pending_exit_order_id,
                position.setup_type,
                position.regime,
                position.trade_type,
                1 if position.intent_confirmed else 0,
                position.created_at.isoformat(),
                position.updated_at.isoformat(),
                json.dumps(list(position.processed_event_hashes))
            ))
            
            # Save fills
            for fill in position.entry_fills:
                self._save_fill(position.position_id, fill)
            for fill in position.exit_fills:
                self._save_fill(position.position_id, fill)
    
    def _save_fill(self, position_id: str, fill: FillRecord) -> None:
        """Save a fill record."""
        self._conn.execute("""
            INSERT OR IGNORE INTO position_fills (
                fill_id, position_id, order_id, side, qty, price, timestamp, is_entry
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            fill.fill_id,
            position_id,
            fill.order_id,
            fill.side.value,
            str(fill.qty),
            str(fill.price),
            fill.timestamp.isoformat(),
            1 if fill.is_entry else 0
        ))
    
    def load_position(self, position_id: str) -> Optional[ManagedPosition]:
        """Load a position by ID."""
        cursor = self._conn.execute(
            "SELECT * FROM positions WHERE position_id = ?",
            (position_id,)
        )
        row = cursor.fetchone()
        if row is None:
            return None
        
        return self._row_to_position(dict(row))
    
    def load_active_positions(self) -> List[ManagedPosition]:
        """Load all non-terminal positions."""
        terminal_states = [
            PositionState.CLOSED.value,
            PositionState.CANCELLED.value,
            PositionState.ERROR.value,
            PositionState.ORPHANED.value
        ]
        
        cursor = self._conn.execute(
            f"SELECT * FROM positions WHERE state NOT IN ({','.join('?' for _ in terminal_states)})",
            terminal_states
        )
        
        positions = []
        for row in cursor:
            pos = self._row_to_position(dict(row))
            if pos:
                positions.append(pos)
        
        return positions
    
    def load_all_positions(self) -> List[ManagedPosition]:
        """Load all positions."""
        cursor = self._conn.execute("SELECT * FROM positions")
        
        positions = []
        for row in cursor:
            pos = self._row_to_position(dict(row))
            if pos:
                positions.append(pos)
        
        return positions
    
    def _row_to_position(self, row: Dict) -> Optional[ManagedPosition]:
        """Convert database row to ManagedPosition."""
        try:
            pos = ManagedPosition(
                symbol=row["symbol"],
                side=Side(row["side"]),
                position_id=row["position_id"],
                initial_size=Decimal(row["initial_size"]),
                initial_entry_price=Decimal(row["initial_entry_price"]),
                initial_stop_price=Decimal(row["initial_stop_price"]),
                initial_tp1_price=Decimal(row["initial_tp1_price"]) if row.get("initial_tp1_price") else None,
                initial_tp2_price=Decimal(row["initial_tp2_price"]) if row.get("initial_tp2_price") else None,
                initial_final_target=Decimal(row["initial_final_target"]) if row.get("initial_final_target") else None,
            )
            
            pos.state = PositionState(row["state"])
            pos.current_stop_price = Decimal(row["current_stop_price"]) if row.get("current_stop_price") else None
            pos.entry_acknowledged = bool(row.get("entry_acknowledged", 0))
            pos.tp1_filled = bool(row.get("tp1_filled", 0))
            pos.tp2_filled = bool(row.get("tp2_filled", 0))
            pos.break_even_triggered = bool(row.get("break_even_triggered", 0))
            pos.trailing_active = bool(row.get("trailing_active", 0))
            pos.exit_reason = ExitReason(row["exit_reason"]) if row.get("exit_reason") else None
            pos.exit_time = datetime.fromisoformat(row["exit_time"]) if row.get("exit_time") else None
            pos.entry_order_id = row.get("entry_order_id")
            pos.stop_order_id = row.get("stop_order_id")
            pos.pending_exit_order_id = row.get("pending_exit_order_id")
            pos.setup_type = row.get("setup_type")
            pos.regime = row.get("regime")
            pos.trade_type = row.get("trade_type")
            pos.intent_confirmed = bool(row.get("intent_confirmed", 0))
            pos.created_at = datetime.fromisoformat(row["created_at"])
            pos.updated_at = datetime.fromisoformat(row["updated_at"])
            
            # Load processed event hashes
            hashes = row.get("processed_event_hashes", "[]")
            pos.processed_event_hashes = set(json.loads(hashes))
            
            # Load fills
            self._load_fills(pos)
            
            return pos
            
        except Exception as e:
            logger.error(f"Failed to load position {row.get('position_id')}: {e}")
            return None
    
    def _load_fills(self, position: ManagedPosition) -> None:
        """Load fills for a position."""
        cursor = self._conn.execute(
            "SELECT * FROM position_fills WHERE position_id = ? ORDER BY timestamp",
            (position.position_id,)
        )
        
        for row in cursor:
            fill = FillRecord(
                fill_id=row["fill_id"],
                order_id=row["order_id"],
                side=Side(row["side"]),
                qty=Decimal(row["qty"]),
                price=Decimal(row["price"]),
                timestamp=datetime.fromisoformat(row["timestamp"]),
                is_entry=bool(row["is_entry"])
            )
            
            if fill.is_entry:
                position.entry_fills.append(fill)
            else:
                position.exit_fills.append(fill)
    
    # ========== ACTION LOGGING ==========
    
    def log_action(
        self,
        position_id: str,
        action_type: str,
        payload: Optional[Dict] = None,
        status: str = "executed"
    ) -> None:
        """Log an action to the audit trail."""
        with self._conn:
            self._conn.execute("""
                INSERT INTO position_actions (position_id, action_type, payload_json, status, timestamp)
                VALUES (?, ?, ?, ?, ?)
            """, (
                position_id,
                action_type,
                json.dumps(payload) if payload else None,
                status,
                datetime.now(timezone.utc).isoformat()
            ))
    
    def get_action_history(self, position_id: str, limit: int = 100) -> List[Dict]:
        """Get action history for a position."""
        cursor = self._conn.execute(
            "SELECT * FROM position_actions WHERE position_id = ? ORDER BY timestamp DESC LIMIT ?",
            (position_id, limit)
        )
        
        return [dict(row) for row in cursor]
    
    # ========== PENDING REVERSALS ==========
    
    def save_pending_reversal(self, symbol: str, new_side: Side) -> None:
        """Save pending reversal."""
        with self._conn:
            self._conn.execute("""
                INSERT OR REPLACE INTO pending_reversals (symbol, new_side, created_at)
                VALUES (?, ?, ?)
            """, (symbol, new_side.value, datetime.now(timezone.utc).isoformat()))
    
    def get_pending_reversals(self) -> Dict[str, Side]:
        """Get all pending reversals."""
        cursor = self._conn.execute("SELECT symbol, new_side FROM pending_reversals")
        return {row["symbol"]: Side(row["new_side"]) for row in cursor}
    
    def clear_pending_reversal(self, symbol: str) -> None:
        """Clear pending reversal."""
        with self._conn:
            self._conn.execute("DELETE FROM pending_reversals WHERE symbol = ?", (symbol,))
    
    # ========== REGISTRY PERSISTENCE ==========
    
    def save_registry(self, registry: PositionRegistry) -> None:
        """Save entire registry state."""
        for position in registry.get_all():
            self.save_position(position)
        
        # Save pending reversals
        data = registry.to_dict()
        for symbol, side_str in data.get("pending_reversals", {}).items():
            self.save_pending_reversal(symbol, Side(side_str))
    
    def load_registry(self) -> PositionRegistry:
        """
        Load registry from persistence.
        
        Recovery algorithm:
        1. Load all positions from DB
        2. Load pending reversals
        3. Reconstruct registry state
        """
        registry = PositionRegistry()
        
        # Load all positions
        for pos in self.load_all_positions():
            registry._positions[pos.symbol] = pos
        
        # Load pending reversals
        registry._pending_reversals = self.get_pending_reversals()
        
        # Move terminal positions from _positions to _closed_positions
        terminal_symbols = []
        for symbol, pos in registry._positions.items():
            if pos.is_terminal:
                registry._closed_positions.append(pos)
                terminal_symbols.append(symbol)
        
        # Remove terminal positions from _positions (they're now in _closed_positions)
        for symbol in terminal_symbols:
            del registry._positions[symbol]
        
        logger.info(
            "Registry loaded from persistence",
            total_positions=len(registry._positions) + len(terminal_symbols),
            active_positions=len(registry._positions),
            terminal_moved=len(terminal_symbols),
            pending_reversals=len(registry._pending_reversals)
        )
        
        return registry
    
    # ========== CLEANUP ==========
    
    def cleanup_old_positions(self, days: int = 30) -> int:
        """Remove positions older than N days."""
        cutoff = datetime.now(timezone.utc)
        # Calculate cutoff
        from datetime import timedelta
        cutoff = cutoff - timedelta(days=days)
        
        terminal_states = [
            PositionState.CLOSED.value,
            PositionState.CANCELLED.value,
            PositionState.ERROR.value,
            PositionState.ORPHANED.value
        ]
        
        with self._conn:
            # Delete old fills first (foreign key)
            self._conn.execute("""
                DELETE FROM position_fills WHERE position_id IN (
                    SELECT position_id FROM positions 
                    WHERE state IN (?, ?, ?, ?) AND updated_at < ?
                )
            """, (*terminal_states, cutoff.isoformat()))
            
            # Delete old actions
            self._conn.execute("""
                DELETE FROM position_actions WHERE position_id IN (
                    SELECT position_id FROM positions 
                    WHERE state IN (?, ?, ?, ?) AND updated_at < ?
                )
            """, (*terminal_states, cutoff.isoformat()))
            
            # Delete old positions
            cursor = self._conn.execute("""
                DELETE FROM positions 
                WHERE state IN (?, ?, ?, ?) AND updated_at < ?
            """, (*terminal_states, cutoff.isoformat()))
            
            return cursor.rowcount


# ========== RECOVERY FUNCTIONS ==========

def recover_from_persistence(
    db_path: str = "data/positions.db",
    exchange_positions: Optional[Dict[str, Dict]] = None,
    exchange_orders: Optional[List[Dict]] = None
) -> PositionRegistry:
    """
    Full crash recovery procedure.
    
    1. Load registry from DB
    2. Reconcile with exchange (if data provided)
    3. Mark inconsistencies as ORPHANED
    4. Set as singleton registry
    """
    persistence = PositionPersistence(db_path)
    registry = persistence.load_registry()
    
    if exchange_positions is not None:
        issues = registry.reconcile_with_exchange(
            exchange_positions,
            exchange_orders or []
        )
        
        if issues:
            logger.warning(
                "Recovery found reconciliation issues",
                count=len(issues),
                issues=issues
            )
            
            # Save updated state
            for pos in registry.get_all():
                persistence.save_position(pos)
    
    # Set as singleton
    set_position_registry(registry)
    
    return registry


def persist_registry_state(
    registry: PositionRegistry,
    db_path: str = "data/positions.db"
) -> None:
    """Persist current registry state."""
    persistence = PositionPersistence(db_path)
    persistence.save_registry(registry)
