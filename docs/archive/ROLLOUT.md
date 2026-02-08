# Production Rollout Checklist

Follow this exact sequence to deploy the takeover protocol.

## 1. Preparation

- [ ] **Pull & Install**: Ensure you are on `main` and updated.
  ```bash
  git pull origin main
  source .venv/bin/activate
  pip install -r requirements.txt
  ```

- [ ] **Restart Environment**: Ensure no stale processes.
  
- [ ] **Hard-Disable New Entries** (Set in `.env` or export):
  ```bash
  export NEW_ENTRIES_ENABLED=false
  export REVERSALS_ENABLED=false
  export TAKEOVER_MODE=true
  export PARTIALS_ENABLED=false
  export TRAILING_ENABLED=false
  ```

## 2. Dry-Run Takeover

- [ ] **Run Dry-Run**:
  ```bash
  export TAKEOVER_DRY_RUN=true
  python -m src.tools.run_takeover
  ```

- [ ] **Verify Output**:
  - Exchange positions count matches reality.
  - Case A/B/C/D breakdown looks reasonable.
  - No unexpected errors.

## 3. Verify Build Artifact

- [ ] **Run Unit Tests**:
  ```bash
  pytest -q tests/unit/test_production_takeover.py
  ```

## 4. Run Real Takeover

- [ ] **Execute**:
  ```bash
  unset TAKEOVER_DRY_RUN
  # Start main bot (load V2 registry)
  # NOTE: Ensure main bot doesn't auto-trade yet (NEW_ENTRIES_ENABLED=false)
  # Actually, the script is standalone:
  python -m src.tools.run_takeover
  ```

- [ ] **Confirm Output**:
  - "Snapshot created (ID...)"
  - "Protective stop confirmed" OR "Protective stop placed" for all.
  - "Import completed".
  - "Detailed Breakdown" stats.

## 5. Post-Takeover Sanity

- [ ] **Check Logs/State**:
  - Run `python -m src.live.inspect_registry` (if available) or check `data/positions.db`.
  - Confirm: Registry Count == Exchange Count (minus quarantined).
  - Confirm: No NAKED positions.
  - Confirm: No hanging EXIT_PENDING.

## 6. Enable Safe Management

- [ ] **Switch Flags**:
  ```bash
  export TAKEOVER_MODE=false
  export TRADING_NEW_ENTRIES_ENABLED=false  # Keep false!
  export TRADING_REVERSALS_ENABLED=false
  export TRADING_PARTIALS_ENABLED=false
  export TRADING_TRAILING_ENABLED=false
  
  # Enable V2 Management
  export USE_STATE_MACHINE_V2=true
  ```

- [ ] **Run Main Bot**:
  ```bash
  python -m src.main
  ```

## 7. Gradual Enablement

Only after observing full clean lifecycle:

1. Enable **Trailing**: `export TRADING_TRAILING_ENABLED=true`
2. Enable **Partials**: `export TRADING_PARTIALS_ENABLED=true`
3. LAST: Enable **New Entries**: `export TRADING_NEW_ENTRIES_ENABLED=true`

## CRITICAL RULE

If you see **"stop missing / stop replace failed / orphaned position"**:
1. **QUARANTINE** that symbol.
2. **FLATTEN** if unsure.
3. Do NOT enable new entries until resolved.
