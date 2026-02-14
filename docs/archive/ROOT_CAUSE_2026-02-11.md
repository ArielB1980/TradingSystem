# Root Cause Analysis – Server Logs & Fixes (2026-02-11)

**Principle: Fix root causes, not symptoms.**

This document summarizes what stood out in the last 24–48 hours of server logs, the underlying causes, and the fixes applied.

---

## 1. Infrastructure: OOM Crashes (Root Cause: No Swap on 1GB Droplet)

### Symptoms
- Trading bot killed repeatedly: Feb 9 08:34, Feb 10 09:03, Feb 10 14:49, Feb 11 08:23, Feb 11 08:24 (OOM).
- Load average 31–42 on a 1‑CPU droplet; SSH timeouts; crash–restart loop.
- `systemd`: `Main process exited, code=killed, status=9/KILL` / `Failed with result 'oom-kill'`.

### Root Cause
- **961 MB RAM, 0 B swap.** Bot + dashboard + Docker/Postgres + kraken-recorder use ~830 MB; bot alone ~330 MB when scanning 243 markets. Any spike (OHLCV fetches, API timeouts) pushes the system over and the OOM killer kills the bot.

### Fix Applied
- **2 GB swapfile** added on the server: `fallocate -l 2G /swapfile`, `mkswap`, `swapon`, added to `/etc/fstab`, `vm.swappiness=10`.
- Effect: Memory pressure goes to swap instead of triggering OOM; bot can survive spikes and timeouts.

### Recommendation
- Medium/long term: Either **upgrade to 2 GB RAM** or **reduce workload** (e.g. fewer symbols, or run kraken-recorder / dashboard on a separate box) so that 1 GB + swap is not the only safety net.

---

## 2. Application: Partial Close Rejected (Root Cause: Size Below Venue Minimum)

### Symptoms
- **46 errors** in `run.log`:  
  `Partial close failed: Futures API error: krakenfutures amount of GALA/USD:USD must be greater than minimum amount precision of 1` with `amount=0.5`.
- Same pattern earlier for XRP/USD (amount 0.5; venue minimum 1).

### Root Cause
- In `position_manager_v2.py`, final-target partial close uses `partial_size = position.remaining_qty * Decimal("0.5")`. For `remaining_qty == 1`, that is **0.5**, which is below Kraken’s minimum contract size (1) for GALA (and XRP).
- The gateway sent 0.5 to the exchange; the exchange rejected it. No validation against venue minimum before placement.

### Fix Applied
- **`src/execution/position_manager_v2.py`**: Only emit a partial-close action when `partial_size >= Decimal("1")`. If `partial_size < 1`, we skip the partial close (no order), avoiding “amount must be greater than minimum amount precision of 1”.
- Effect: No more partial closes with size 0.5; no new ORDER_REJECTED_BY_VENUE / Partial close failed for this reason.

### Recommendation
- Longer term: Use **instrument specs** (e.g. `InstrumentSpecRegistry` / min_size per symbol) and in the gateway round partial-close size up to `max(action.size, min_size)` and cap at `remaining_qty`, so all symbols respect venue minimums.

---

## 3. Application: “Position already exists” on Startup Takeover (Root Cause: Case D Purge Key Mismatch)

### Symptoms
- **Critical**: `INVARIANT VIOLATION: Cannot register position: Position already exists for PF_TONUSD (open)` during production takeover after restart.
- `Failed to process PF_TONUSD: Cannot register position: Position already exists for PF_TONUSD (open)`.
- Occurred on Feb 10 07:18 and 09:06, and again on Feb 11 after OOM restart.

### Root Cause
- Takeover classifies “duplicate” (Case D) when the registry already has a position for that symbol (normalized). For Case D without a stop, the code **purges** the registry entry then re-imports from the exchange.
- Purge used `del self.registry._positions[symbol]`, where `symbol` came from the **exchange** (e.g. `TON/USD:USD` or `PF_TONUSD`). The registry keys positions by **registry’s** `position.symbol` (e.g. `PF_TONUSD`). If the exchange uses a different format, the key used for `del` did not match the key used when the position was stored, so **the old position was never removed**. Takeover then called `register_position(pos)` for the new import; the registry still had the old open position (by normalized lookup), so `can_open_position` failed and the invariant was raised.

### Fix Applied
- **`src/execution/production_takeover.py`**: For Case D, get the existing position with **normalized lookup**: `existing = self.registry.get_position(symbol)`. Then purge by the **registry’s key**: `del self.registry._positions[existing.symbol]`. So we always remove the same object that `register_position` would see as “already exists”.
- Effect: After purge, the registry no longer has that symbol; re-import and `register_position` succeed; no more “Position already exists” on takeover.

---

## 4. Other Observations (Not Fixed Here)

- **Kraken API timeouts**: Many `Failed to fetch spot OHLCV` (TimeoutError) and `Failed to fetch futures open orders`. These correlate with high load and memory pressure; swap + stability should reduce them. If they persist, consider stricter backoff, fewer concurrent requests, or a smaller symbol set.
- **Futures openorders errors**: 30 errors in the last run; same root cause (load/OOM/restarts). Monitor after the above fixes.
- **UNPROTECTED / ORPHANED**: TON/USD had `MISSING_STOP` and positions marked ORPHANED after sync; takeover and protection logic should improve this with the Case D fix and swap reducing restarts.
- **Signals vs auction**: Many “Signal generated” lines but often `signals_collected=0` at AUCTION_END because existing positions (e.g. BNB, XRP) already fill the slot; auction correctly does not open a second position per symbol. No code bug identified.

---

## Summary of Changes

| Area | Root cause | Fix |
|------|------------|-----|
| **Infrastructure** | No swap on 1 GB droplet → OOM kills bot | Add 2 GB swapfile on server; swappiness=10 |
| **Partial close** | Partial size 0.5 &lt; venue min 1 → reject | Only emit partial close when `partial_size >= 1` (`position_manager_v2.py`) |
| **Takeover** | Case D purge used exchange symbol key → stale position remained → “Position already exists” | Purge by `existing.symbol` after normalized lookup (`production_takeover.py`) |

All fixes are in the repo (code) or on the server (swap). No band-aids; each addresses the underlying invariant or resource constraint.
