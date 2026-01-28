# Instrument Spec and Execution Fixes (revised)

## Green-lit items (unchanged)

- **InstrumentSpecRegistry cache fix** — `symbol_raw → spec` dict, then dump values; deterministic, future-proof.
- **Refresh staleness guard inside `refresh()`** — Registry is authoritative; adapter stays dumb.
- **BTC/XBT dual-candidate resolution** — Try both bases without overwriting; correct Kraken compromise.
- **Kill switch deterministic path** — Move off CWD; env override for ops.
- **Centralized `futures_candidate_symbols()`** — Single source of truth for symbol normalization.
- **SL/TP deferred** — No code change until contract tests validate behavior.

---

## Refinements (incorporated)

### A) Refresh staleness: “loaded vs empty” explicit

**Requirement:** Treat “never loaded” as stale regardless of timestamps. Avoid edge case: disk cache failed → `_by_raw` empty → refresh silently no-ops.

**Implementation:**

- In `InstrumentSpecRegistry._is_stale()`: return `True` when `not self._by_raw` or `self._loaded_at == 0` (never loaded). Otherwise apply TTL: `(time.time() - self._loaded_at) > self._cache_ttl`.
- In `refresh()`: after the “no get_instruments_fn” block, add: if `self._by_raw` and not `self._is_stale()`: return. With the updated `_is_stale()`, “empty” and “never loaded” will always be considered stale and trigger a refresh.

**Conceptually:** `not self._by_raw` → always refresh; `_loaded_at == 0` → stale; otherwise TTL.

---

### B) Market order pricing: hard reject, explicit message

**Requirement:** Make missing price a **hard reject**, not a warning. Enforce invariant: “Market orders must always be sized from a real price.”

**Implementation:**

- In `FuturesAdapter.place_order`, when `order_type == OrderType.MARKET` and no valid `price_use` after trying `mark_price` and cached tickers (and optional fetch): **raise** a clear exception (e.g. `ValueError`), do not fall back to `size_notional / 1`.
- Error message must include: **symbol**, **client_order_id** (or “pre-submit”), **size_notional**, and a short reason: e.g. “Market order requires mark_price or ticker for size calculation; none available for symbol X.”

---

### C) Executor → Adapter: `mark_price` API contract

**Requirement:** Avoid API drift; make `mark_price` safe for legacy callers and document why it exists.

**Implementation:**

- Add `mark_price: Optional[Decimal] = None` to `place_order()`.
- Prefer **keyword-only** for `mark_price` (e.g. in Python, add `*,` before `mark_price` so it must be passed by name). If that would force other params to become keyword-only and break callers, at minimum: default `None`, and add a one-line docstring/comment: “If provided, used for contract sizing when order is market and price is missing (avoids wrong size from fallback).”
- Ensure all existing call sites remain valid (no required new args).

---

### D) Symbol normalization: freeze `futures_candidate_symbols` with tests

**Requirement:** Treat `futures_candidate_symbols()` as the **only** place that encodes Kraken BTC/XBT quirks. Lock the contract with tests.

**Implementation:**

- In `src/data/symbol_utils.py`, add `futures_candidate_symbols(spot_symbol: str) -> List[str]` with the agreed behavior (both BTC and XBT bases when base is BTC or XBT; no BTC/XBT pollution for other bases).
- Add a **unit test** (table-driven or parametrized) that asserts:
  - `"BTC/USD"` → candidate list **includes** XBT variants (e.g. `PF_XBTUSD`, `XBT/USD:USD`).
  - `"XBT/USD"` → candidate list **includes** BTC variants.
  - `"ETH/USD"` → candidate list **does not** include BTC/XBT symbols (no cross-asset pollution).
- Any future Kraken-specific symbol quirk goes in this function only; callers use it, no ad-hoc base overwrites elsewhere.

---

### E) Kill switch path: use data/ or state/ directory

**Requirement:** Prefer a dedicated directory for consistency with instrument specs cache (e.g. `data/` or `state/`), not repo root.

**Implementation:**

- Use the same base as instrument specs: e.g. `DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"` (in kill_switch, resolve from `src/utils/kill_switch.py` → repo root = `parent.parent.parent`; then `DATA_DIR = repo_root / "data"`).
- State file: `DATA_DIR / ".kill_switch_state"` or `DATA_DIR / "kill_switch_state.json"` (no leading dot if you prefer; keep name clear).
- Ensure `DATA_DIR` is created if missing when writing (e.g. `DATA_DIR.mkdir(parents=True, exist_ok=True)` before open).
- **Env override:** if `KILL_SWITCH_STATE_PATH` is set, use that path instead; otherwise use `DATA_DIR / "kill_switch_state_state"` (or chosen filename).
- Update `read_kill_switch_state()` to use the same path logic (shared helper or same `Path(__file__)` + env).

---

## New item: Execution invariant assertion

**Requirement:** Add a permanent guard/documentation that “If order_type == MARKET, then price_used_for_sizing must come from mark/ticker.” Prevents regression to `size_notional / 1` and documents a system rule.

**Implementation:**

- **Where:** In `FuturesAdapter.place_order()`, immediately after `price_use` is finalized (and before `compute_size_contracts` or equivalent).
- **How:** Guard clause: if `order_type == OrderType.MARKET` and `price_use` was not derived from mark/ticker (e.g. you can track a small flag “price_from_mark_or_ticker” when setting `price_use` from `mark_price` or cached ticker or fetch), then **raise** with a clear message. Alternatively, structure the logic so that for MARKET we never set `price_use` to `size_notional`; the only path is “valid mark/ticker” or “raise”. Then add a one-line assertion or comment: “Invariant: market orders are sized only from mark/ticker.”
- **Seatbelt:** A single `assert` or `if order_type == MARKET and not price_from_mark_or_ticker: raise ValueError(...)` is enough; no need for a log-only assertion if we already hard-fail when price is missing (refinement B). The invariant is then: “for MARKET, we either have price_use from mark/ticker or we have already raised.” So the “assertion” is the structure of the code plus one comment documenting the invariant.

---

## Summary of refinements

| Refinement | Location | Action |
|------------|----------|--------|
| A | `instrument_specs.py` | `_is_stale()` returns True when `not self._by_raw` or `self._loaded_at == 0`; then early-return in `refresh()` |
| B | `futures_adapter.py` | Hard reject market order when no mark/ticker; error includes symbol, order id, notional |
| C | `futures_adapter.py` | `mark_price` keyword-only (or default None + comment); doc why adapter needs it |
| D | `symbol_utils.py` + tests | `futures_candidate_symbols()`; unit test: BTC→XBT, XBT→BTC, ETH→no pollution |
| E | `kill_switch.py` | State file under `data/` (or env); same repo-root resolution as instrument_specs |
| Invariant | `futures_adapter.py` | After `price_use` set: guard/comment that MARKET orders are sized only from mark/ticker |

---

## Implementation order (unchanged)

1. Cache save fix (instrument_specs).
2. Refresh staleness + _is_stale() “empty/never loaded” (instrument_specs).
3. Kill switch path under data/ + env override (kill_switch).
4. futures_candidate_symbols + unit test (symbol_utils); then BTC/XBT in registry and adapter.
5. Market order: mark_price param, hard reject, invariant assertion (futures_adapter + executor).
6. SL/TP: contract tests and codify params (separate follow-up).

All green-lit items and the above refinements are now part of the plan.
