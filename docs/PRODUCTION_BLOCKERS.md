# Production Blockers and Risks

## Critical Issues (Must Address Before Live Trading)

### 1. ✅ Test Suite Dependencies

**Status:** Fixed

**Issue:** Test suite requires `ccxt` and `structlog` which must be installed via `requirements.txt`.

**Solution:**
- `requirements.txt` includes all runtime dependencies (`ccxt>=4.0.0`, `structlog>=23.0.0`)
- Deployment script (`scripts/deploy.sh`) now installs dependencies after code update:
  ```bash
  venv/bin/pip install --upgrade pip && venv/bin/pip install -r requirements.txt
  ```

**Verification:**
- Ensure production server has `requirements.txt` installed in venv before running tests or production code.

---

### 2. ✅ Data Directory Writability

**Status:** Fixed

**Issue:** Both `data/instrument_specs_cache.json` and `data/kill_switch_state.json` must be writable. Under systemd/hardened servers, repo directory may be read-only.

**Solution:**
- Added environment variable support:
  - `KILL_SWITCH_STATE_PATH` (already implemented)
  - `INSTRUMENT_SPECS_CACHE_PATH` (new)
- Both default to `data/` under repo root, but can be overridden:
  ```bash
  export KILL_Switch_STATE_PATH=/var/lib/trading-system/kill_switch_state.json
  export INSTRUMENT_SPECS_CACHE_PATH=/var/lib/trading-system/instrument_specs_cache.json
  ```

**Production Setup:**
```bash
# In systemd service or .env.local:
KILL_SWITCH_STATE_PATH=/var/lib/trading-system/kill_switch_state.json
INSTRUMENT_SPECS_CACHE_PATH=/var/lib/trading-system/instrument_specs_cache.json

# Ensure directory exists and is writable:
sudo mkdir -p /var/lib/trading-system
sudo chown trading:trading /var/lib/trading-system
sudo chmod 755 /var/lib/trading-system
```

**Verification:**
- Check that state files persist across restarts (kill switch state, instrument cache).

---

### 3. ✅ Legacy State File Cleanup

**Status:** Fixed

**Issue:** `.kill_switch_state` in repo root is no longer used (moved to `data/`), but can confuse ops/docs.

**Solution:**
- Deleted `.kill_switch_state` from repo root
- Added to `.gitignore`: `.kill_switch_state`, `data/kill_switch_state.json`, `data/instrument_specs_cache.json`

**Verification:**
- Confirm file is deleted and won't be recreated in repo root.

---

### 4. ⚠️ SL/TP Semantics (HIGHEST RISK)

**Status:** Deferred - Requires Integration Testing

**Issue:** Stop-loss and take-profit order semantics are exchange-specific in CCXT/Kraken Futures. Without contract tests, we cannot guarantee:
- Stop-market vs stop-limit behavior
- When `price` parameter is required vs optional
- Exact parameter mapping (`stopPrice`, `type`, `params`)

**Current Implementation:**
- `src/data/kraken_client.py`: Maps `OrderType.STOP_LOSS` → `"stp"`, `OrderType.TAKE_PROFIT` → `"take_profit"`
- Sets `params['stopPrice']` when `stop_price` provided
- Uses CCXT `create_order()` with `type`, `price`, `params`

**Risk:**
- Orders may "place" but not trigger correctly
- Orders may be rejected unexpectedly
- Stop-loss protection may fail silently

**Required Actions Before Live:**
1. **Integration Test on Kraken Futures Testnet:**
   - Open tiny position (minimum size)
   - Place stop-loss order
   - Verify order appears on venue with correct type
   - Trigger stop (move price to stop level)
   - Verify order executes correctly
   - Repeat for take-profit

2. **Codify Exact Parameters:**
   - Document exact CCXT/Kraken params in `place_futures_order()` docstring
   - Add integration test that asserts order payload (symbol, type, params including stopPrice)
   - Add contract test that verifies order behavior

3. **Fallback Safety:**
   - Consider adding order verification after placement (check order on exchange matches expected type/params)
   - Add monitoring/alerting if SL/TP orders don't appear as expected

**Files to Update:**
- `src/data/kraken_client.py` - Document exact SL/TP params
- `tests/integration/test_sl_tp_orders.py` - New integration test (requires testnet credentials)

**Priority:** **CRITICAL** - This is the #1 production safety risk. Do not go live without validating SL/TP behavior.

---

## Summary

| Blocker | Status | Risk Level |
|---------|--------|------------|
| Test suite deps | ✅ Fixed | Low |
| Data dir writability | ✅ Fixed | Medium |
| Legacy state file | ✅ Fixed | Low |
| SL/TP semantics | ⚠️ Requires testing | **CRITICAL** |

**Next Steps:**
1. Deploy fixes (items 1-3) to production
2. Set up Kraken Futures testnet account
3. Implement and run SL/TP integration tests
4. Document exact parameters and add contract tests
5. Only then proceed with live trading
