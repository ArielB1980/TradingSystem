# Log Tracking: Auction Runs & Trade Opens

**Last checked:** 2026-01-27 (server logs tail)

## 1. Fix status (BoundLogger)

- **Deploy:** Commit `680b04b` is live. Service restarted at ~22:59 UTC.
- **Pre-fix:** Logs up to 22:54 showed `"Failed to run auction allocation"` with `BoundLogger.info() got multiple values for argument 'event'` on every AUCTION_START → no orders placed.
- **Post-fix:** No BoundLogger errors in logs after 22:59. When the auction runs, it no longer crashes on the "Auction plan generated" log.

## 2. Auction runs & trades opened (from logs)

| Time (UTC)     | Event                         | Result |
|----------------|-------------------------------|--------|
| 21:07          | Auction plan generated        | opens_count=0 → no opens planned |
| 21:28–21:29    | Auction plan → execution     | **opens_planned=5, opens_executed=2, opens_failed=3** — BCH/USD and EUR/USD **opened** ("Entry order placed", "Auction: Opened position") |
| 21:57          | Auction plan (5 opens)       | opens_executed=0, opens_failed=5 (pre-fix; executor_returned_none) |
| 22:37–22:54    | AUCTION_START repeatedly      | **Failed to run auction allocation** (BoundLogger bug) → no execution |
| 22:59+         | New process after deploy      | Trading paused (candle health). No auction/trade-open path run yet in tail. |

So when the auction runs **and** candle health allows trading, **trades are opened** (see 21:28–21:29).

## 3. Current blocker: TRADING PAUSED

Logs show:

```text
"TRADING PAUSED: candle health insufficient"
coins_with_sufficient_candles=12, total=12, min_healthy_coins=30, min_health_ratio=0.25
```

- **Gate:** `src/live/live_trading.py` (e.g. ~830–846, 1519–1524): if `coins_with_sufficient_candles < min_healthy` or ratio < min_ratio, `trade_paused=True` and `_handle_signal` returns without placing orders.
- **Effect:** Auction can run, but every open is rejected with `"TRADING PAUSED: candle health insufficient"` until candle health is above the threshold.

So even with the BoundLogger fix, **no new trades will be opened** until either:

1. Candle health meets the config (e.g. ≥30 coins with ≥50 bars of 15m), or  
2. You temporarily lower `min_healthy_coins` / relax the rule for testing.

## 4. What to watch in logs

- **Auction ran without crash:**  
  `"event": "Auction plan generated"` then `"event": "Auction allocation executed"` (no "Failed to run auction allocation" in between).
- **Trades opened:**  
  `"event": "Entry order placed"` and `"event": "Auction: Opened position"` with symbol/order_id.
- **Still blocked by candle health:**  
  `"event": "TRADING PAUSED: candle health insufficient"` and/or `"reason": "TRADING PAUSED: candle health insufficient"` in handle_signal/auction outcomes.

## 5. Quick commands

```bash
# Live tail
ssh -i ~/.ssh/trading_droplet root@207.154.193.121 \
  'sudo -u trading tail -f /home/trading/TradingSystem/logs/run.log'

# Recent auction/trade events
ssh -i ~/.ssh/trading_droplet root@207.154.193.121 \
  'sudo -u trading tail -n 3000 /home/trading/TradingSystem/logs/run.log' \
  | grep -E "Auction plan generated|Auction allocation executed|Failed to run auction|Entry order placed|Auction: Opened position|AUCTION_START|AUCTION_END|TRADING PAUSED"
```
