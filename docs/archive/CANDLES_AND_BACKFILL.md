# Why Candles "Lack" Data & When to Run Backfill

## 1. What "candle health insufficient" means

The live loop pauses new entries when **either**:

- `coins_with_sufficient_candles < min_healthy_coins` (default **30**), or  
- `(coins_with_sufficient_candles / total_coins) < min_health_ratio` (default **0.25**).

**"Sufficient"** = at least **50 bars of 15m** candles for that symbol (from DB + live updates).

So "candles lack data" can mean two different things:

| Case | Meaning | Fix |
|------|--------|-----|
| **Too few coins** | Universe has 12 symbols, all with ≥50 bars 15m, but 12 &lt; 30 → gate fails. | Lower `min_healthy_coins` (e.g. to 12) or use small-universe logic (see below). |
| **Missing per-coin data** | Some symbols have &lt;50 bars of 15m (or zero) in the DB. | Run backfill for the current universe. |

## 2. Current server behaviour (from logs)

Observed log:

```text
"TRADING PAUSED: candle health insufficient"
coins_with_sufficient_candles=12, total=12, min_healthy_coins=30, min_health_ratio=0.25
```

So:

- **total_coins = 12** → market discovery is only keeping 12 symbols (Kraken-supported intersection).
- **coins_with_sufficient_candles = 12** → all 12 have ≥50 bars of 15m; per-coin data is fine.
- **12 < 30** → the gate fails because of **universe size**, not missing candles.

So in this case **backfill does not resolve the pause**. The fix is to relax the health gate when the universe is small (e.g. require “all coins healthy” instead of “at least 30 coins healthy”), or to lower `min_healthy_coins` in config.

## 3. Where candle data comes from

- **At runtime:** `CandleManager` loads from **DB** via `load_candles_map(symbols, "15m", days=14)` (and 1h/4h/1d with longer windows), then live OHLCV appends new bars.
- **Filling the DB:** `scripts/backfill_historical_data.py` fetches from Kraken and writes to the same DB (1d, 4h, 1h, 15m; default 250 days).
- **Symbol list for backfill:** Uses `_get_monitored_symbols(config)` (dashboard utils), which prefers DISCOVERY_UPDATE in DB, then discovered file, then `coin_universe`, then config markets.

So:

- If the **universe** is small (e.g. 12) because of discovery, that’s unchanged by backfill.
- If **per-coin** 15m (or 1d/4h/1h) history is missing or thin, backfill is what fixes it.

## 4. When to run backfill

Run backfill when:

1. **DB is new or was reset** – so 15m (and 1d/4h/1h) are empty or very short.
2. **New symbols were added** – so new symbols have no or little history.
3. **Strategy needs longer history** – e.g. EMA 200 on 1d needs 200+ daily candles; backfill provides 250 days of 1d.
4. **You see low `coins_with_sufficient_candles`** – e.g. 2 out of 12 with “sufficient” 15m → run backfill for the current monitored symbols.

```bash
# From repo root, with .env.local (and DATABASE_URL if needed)
make backfill
# or
python scripts/backfill_historical_data.py
```

Backfill uses the same DB and symbol source as the app; it does **not** change the number of coins in the universe.

## 5. How to fix “too few coins” (12 < 30)

**Option A – Config (recommended for small universes)**  
In `config.yaml` under `data:` add (or change):

```yaml
data:
  min_healthy_coins: 12   # or whatever your typical universe size is
  min_health_ratio: 0.25
```

So with 12 coins and all 12 healthy, the gate passes.

**Option B – Small-universe logic in code**  
When `total_coins < min_healthy_coins`, require that **all** coins are healthy:  
`coins_with_sufficient_candles == total_coins` and `ratio >= min_health_ratio`.  
That way 12/12 healthy is enough even if `min_healthy_coins` stays 30.

## 6. Summary

| Observation | Cause | Action |
|------------|--------|--------|
| `coins_with_sufficient_candles=12`, `total=12`, still paused | 12 < 30 (min_healthy_coins) | Lower `min_healthy_coins` or add small-universe logic. **No backfill needed** for this. |
| `coins_with_sufficient_candles=2`, `total=12` | Many symbols have &lt;50 bars 15m | **Run backfill** for the current monitored symbols. |
| Need EMA 200 on 1d but many coins fail bias | Not enough 1d history | **Run backfill** (adds 250 days of 1d, etc.). |

**Quick check:** Look at the "Hydration complete" log at startup: `with_sufficient_15m` and `with_zero_15m`. If most symbols have zero 15m, run backfill. If `with_sufficient_15m == total` but you’re still paused, the limit is `min_healthy_coins` / universe size, not missing candles.
