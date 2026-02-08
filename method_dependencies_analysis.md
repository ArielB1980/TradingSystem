# Method Dependencies Analysis
## File: `/Users/arielbarack/Documents/TradingSystem/src/live/live_trading.py`
## Range: Lines 2901-3682

---

## 1. `_reconcile_protective_orders`
**Line Range:** 2901-3009

### Self Attributes/Methods Accessed:
- `self.config.execution.tp_backfill_enabled`
- `self.use_state_machine_v2`
- `self.position_registry`
- `self.position_registry.get_position()` (if v2 enabled)
- `self._should_skip_tp_backfill()`
- `self.client.get_futures_open_orders()`
- `self._needs_tp_backfill()`
- `self._compute_tp_plan()`
- `self._place_tp_backfill()`

### Imports Used:
- `from src.storage.repository import get_active_position, save_position, async_record_event`
- `from src.data.symbol_utils import position_symbol_matches_order`
- `asyncio` (to_thread)
- `logger` (module-level)
- `Decimal` (from decimal)
- `datetime.now(timezone.utc)` (from datetime)

### Helper Functions:
- None

---

## 2. `_reconcile_stop_loss_order_ids`
**Line Range:** 3010-3165

### Self Attributes/Methods Accessed:
- `self.client.get_futures_open_orders()`

### Imports Used:
- `from src.storage.repository import get_active_position, save_position`
- `from src.data.symbol_utils import normalize_symbol_for_position_match`
- `asyncio` (to_thread)
- `logger` (module-level)
- `Decimal` (from decimal)
- `Side` (from src.domain.models)

### Helper Functions:
- `_exchange_position_side()` (module-level function, line 61)

---

## 3. `_place_missing_stops_for_unprotected`
**Line Range:** 3166-3259

### Self Attributes/Methods Accessed:
- `self.config.system.dry_run`
- `self.client.get_futures_open_orders()`
- `self.client.place_futures_order()`

### Imports Used:
- `from src.data.symbol_utils import position_symbol_matches_order`
- `from src.data.symbol_utils import pf_to_unified` (conditional import)
- `logger` (module-level)
- `Decimal` (from decimal)

### Helper Functions:
- `_order_is_stop(o: Dict, side: str) -> bool` (nested function, lines 3173-3184)
- `_exchange_position_side()` (module-level function, line 61)

---

## 4. `_should_skip_tp_backfill`
**Line Range:** 3260-3298

### Self Attributes/Methods Accessed:
- `self.tp_backfill_cooldowns` (dict attribute)
- `self.config.execution.tp_backfill_cooldown_minutes`
- `self.config.execution.min_hold_seconds`

### Imports Used:
- `logger` (module-level)
- `datetime.now(timezone.utc)` (from datetime)
- `Decimal` (from decimal)
- `Optional` (from typing)

### Helper Functions:
- None

---

## 5. `_needs_tp_backfill`
**Line Range:** 3299-3344

### Self Attributes/Methods Accessed:
- `self.config.execution.min_tp_orders_expected`

### Imports Used:
- `logger` (module-level)
- `Side` (from src.domain.models)
- `List`, `Dict` (from typing)

### Helper Functions:
- None

---

## 6. `_compute_tp_plan`
**Line Range:** 3346-3456

### Self Attributes/Methods Accessed:
- `self.config.execution.min_tp_distance_pct`
- `self.config.execution.max_tp_distance_pct` (optional)

### Imports Used:
- `from src.storage.repository import async_record_event`
- `logger` (module-level)
- `Decimal` (from decimal)
- `Optional`, `List`, `Dict` (from typing)
- `Side` (from src.domain.models)

### Helper Functions:
- None

---

## 7. `_cleanup_orphan_reduce_only_orders`
**Line Range:** 3458-3568

### Self Attributes/Methods Accessed:
- `self.client.get_futures_open_orders()`
- `self.futures_adapter.cancel_order()`

### Imports Used:
- `logger` (module-level)
- `Decimal` (from decimal)

### Helper Functions:
- None

---

## 8. `_place_tp_backfill`
**Line Range:** 3569-3682

### Self Attributes/Methods Accessed:
- `self.config.execution.tp_price_tolerance`
- `self.futures_adapter.cancel_order()`
- `self.futures_adapter.position_size_notional()`
- `self.executor.update_protective_orders()`
- `self.tp_backfill_cooldowns` (dict attribute)

### Imports Used:
- `from src.storage.repository import save_position, async_record_event`
- `asyncio` (to_thread)
- `logger` (module-level)
- `Decimal` (from decimal)
- `List`, `Dict` (from typing)

### Helper Functions:
- None

---

## Summary Statistics

### Most Frequently Used Self Attributes:
- `self.config.*` (used in 6 methods)
- `self.client.*` (used in 3 methods)
- `self.futures_adapter.*` (used in 2 methods)
- `self.executor.*` (used in 1 method)
- `self.position_registry.*` (used in 1 method)
- `self.tp_backfill_cooldowns` (used in 2 methods)
- `self.use_state_machine_v2` (used in 1 method)

### Most Frequently Used External Imports:
- `logger` (used in all 8 methods)
- `Decimal` (used in 7 methods)
- `from src.storage.repository import *` (used in 4 methods)
- `from src.data.symbol_utils import *` (used in 3 methods)
- `asyncio` (used in 3 methods)
- `Side` (used in 3 methods)

### Helper Functions Defined:
- `_order_is_stop()` - nested in `_place_missing_stops_for_unprotected` (lines 3173-3184)
- `_exchange_position_side()` - module-level function (line 61), used by 2 methods
