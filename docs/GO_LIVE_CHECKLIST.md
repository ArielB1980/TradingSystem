# Go-Live Checklist

No fluff. You are ready to enable live entries only if **all** items below are true.

---

## What’s Already Live-Ready

- **Reconciler**: Wired at startup + periodic + event-driven. Adopt/force_close policy and protection placement. See `src/reconciliation/reconciler.py`, `src/live/live_trading.py`.
- **OHLCV fetcher**: Retries/backoff, per-symbol cooldown, concurrency, min delay. `src/data/ohlcv_fetcher.py`, `src/data/candle_manager.py`.
- **Candle health gate**: `data.min_healthy_coins`, `data.min_health_ratio`. Pauses new entries when data is bad. `src/live/live_trading.py` (`trade_paused`).
- **Auction path** and **\_handle_signal** overrides: No longer blocked by stale margin.
- **ShockGuard**: Present and integrated.

---

## 1. Trading Mode

| Requirement | Config / Env | Where |
|-------------|--------------|--------|
| Real trades allowed | `system.dry_run: false` | `config.yaml` → `system.dry_run` |
| Production exchange | `exchange.use_testnet: false` | `config.yaml` → `exchange.use_testnet` |
| Environment | `environment: prod` | Config root or env |

---

## 2. Entry Mechanism

| Choice | Config | Effect |
|--------|--------|--------|
| **Immediate execution** | `risk.auction_mode_enabled: false` | Signal → open as soon as risk allows. |
| **Auction batching** | `risk.auction_mode_enabled: true` | Signals queued; opens only in auction cycle. “Signals found but not trading” is expected until the next cycle. |

If you expect “trade as soon as signal appears,” set `auction_mode_enabled: false` **or** accept auction batching.

---

## 3. Reconciliation Interval

| Requirement | Config | File |
|-------------|--------|------|
| Live-safe interval | `reconciliation.periodic_interval_seconds >= 60` (prefer **120**) | `src/config/config.yaml` |

**Default in repo:** `120`. Avoid 15s on live—it hammers Kraken together with OHLCV, bulk tickers, and order management. Keep event-driven reconcile (e.g. on fills) if you have it; don’t poll every 15s.

---

## 4. Candle Health Gate

| Requirement | Config (code defaults) | What to check in logs |
|-------------|------------------------|------------------------|
| Enough coins | `data.min_healthy_coins` = 30 | `coins_with_sufficient_candles >= 30` |
| Enough ratio | `data.min_health_ratio` = 0.25 | `(sufficient / total) >= 0.25` |

If these fail, `trade_paused=True` and you get “signals found” but no orders. Overrides live in `data` section of config (see `src/config/config.py` → `DataConfig`).

---

## 5. Live Safety Gate (Paper Requirements)

| Requirement | Config | Where enforced |
|-------------|--------|----------------|
| Block until paper passes | `live.require_paper_success: true` | `src/cli.py` (start live flow) |
| Thresholds | `min_paper_days`, `min_paper_trades`, `max_paper_drawdown_pct` | `src/config/config.py` → `LiveConfig` |

**Branches:**

- **Safe:** Run paper until thresholds are met.
- **Immediate live:** Set `live.require_paper_success: false` in `config.yaml` **intentionally** (you accept bypassing the gate).  
  Config path: `live.require_paper_success` in `src/config/config.yaml`.

---

## 6. Unmanaged Position Policy

| Recommendation | Config | Log to confirm |
|----------------|--------|----------------|
| Adopt ghosts | `reconciliation.unmanaged_position_policy: adopt` | `RECONCILE_SUMMARY` periodically |

`reconciliation.unmanaged_position_policy` in `config.yaml` / ReconciliationConfig. Use `force_close` only if you explicitly want to flat unmanaged positions.

---

## 7. Env and Credentials

On the **server**, ensure at least:

| Env / credential | Purpose |
|------------------|---------|
| `KRAKEN_API_KEY` / `KRAKEN_API_SECRET` | Spot (OHLCV, etc.) |
| `KRAKEN_FUTURES_API_KEY` / `KRAKEN_FUTURES_API_SECRET` | Futures trading and reconciliation |
| `DATABASE_URL` | Persistence |
| `CONFIG_PATH` | Config file used by the process |

See `.env.example` for full list. The service must load these before starting.

---

## 8. Risk Sizing (First Live)

Current `config.yaml` defaults are aggressive for a first live week. Suggested **minimum** for go-live:

| Current (example) | Suggested for first live |
|-------------------|---------------------------|
| `risk.risk_per_trade_pct: 0.03` | `0.005`–`0.01` |
| `risk.target_leverage: 7.0` | `3`–`5` |
| `risk.auction_max_margin_util: 0.90` | `0.5`–`0.7` |
| `risk.max_concurrent_positions: 25` | `5`–`10` |

Scale up after the full loop is proven. Config keys live under `risk` in `config.yaml`.

---

## 9. Alerts

| Minimum | Config / setup |
|--------|----------------|
| Log alerts | `monitoring.alert_methods` includes `"log"` (default). |
| CRITICAL events | Prefer Slack/Discord webhook in `monitoring.alert_methods` and corresponding webhook env/URL. |

---

## Final “Go Live” Conditions

You are ready to enable live entries **only if**:

1. **Trading mode:** `system.dry_run=false`, `exchange.use_testnet=false`, `environment=prod`.
2. **Entry mechanism:** Either `auction_mode_enabled=false` (immediate) or you accept auction batching.
3. **Reconciliation:** `reconciliation.periodic_interval_seconds >= 60` (prefer 120).
4. **Candle health:** Logs show `coins_with_sufficient_candles >= 30` and ratio `>= 0.25` (or you intentionally lowered thresholds).
5. **Live safety gate:** Paper thresholds are met **or** `live.require_paper_success=false` is set deliberately.
6. **Unmanaged policy:** `reconciliation.unmanaged_position_policy=adopt` (or force_close by design), and **RECONCILE_SUMMARY** appears periodically in logs.
7. **Alerts:** At least log alerts; ideally Slack/Discord for CRITICAL.

For reconciliation behavior and candle health details, see **OPS_HEALTH.md**.
