# Why the Trading Universe Shrinks (247 → 11)

The system **replaces** the coin list with whatever **market discovery** returns. Discovery is the only thing that sets how many coins are tracked for trading.

## 1. Where the reduction happens

| Step | What happens |
|------|----------------|
| **Startup** | `LiveTrading` loads the initial universe from config: **coin_universe** (200+ symbols) or **whitelist** or **exchange.spot_markets**. That’s the “Coin Universe Enabled” list (e.g. 247 symbols). |
| **Discovery** | `_update_market_universe()` calls `market_discovery.discover_markets()`, which uses **MarketRegistry** to fetch Kraken spot + futures and apply filters. |
| **Replace** | The universe is **replaced** with discovery’s result: `self.markets = mapping`. So the **number of coins** you see in “Market universe updated” and “TRADING PAUSED” is **only** from discovery. |
| **“SYMBOL REMOVED”** | Any symbol that was in the initial list but **not** in discovery’s mapping is logged as “SYMBOL REMOVED (unsupported on Kraken)”. |

So the drop from 247 to 11 is **not** from the initial config; it’s from **discovery returning only 11 pairs**.

## 2. What discovery does (MarketRegistry)

Discovery does three things:

### A. Build spot↔futures mappings

- **Spot:** `KrakenClient.get_spot_markets()` → keys like `"BTC/USD"`.
- **Futures:** `KrakenClient.get_futures_markets()` → keys are `symbol.split(":")[0]` or `symbol` (e.g. `"BTC/USD"` for `"BTC/USD:USD"`, or `"BTCUSD-PERP"` if Kraken uses that).
- **Mapping:** `_build_mappings()` only keeps pairs where `spot_symbol in futures_markets`. So the **key format** must match. If spot uses `"BTC/USD"` and futures uses another format, you get fewer (or no) pairs.

### B. Apply liquidity/spread filters

For **each** of those pairs, `_apply_filters()`:

1. Calls `client.get_spot_ticker(symbol)` (one API call per pair).
2. Checks:
   - `quoteVolume` ≥ **min_spot_volume_usd_24h** (default **$5M**)
   - Spread ≤ **max_spread_pct** (default **0.05%**)
   - Optionally `last` ≥ **min_price_usd**
3. On **any** exception (timeout, rate limit, etc.), the pair is marked ineligible.

So the shrink to 11 can come from:

- **Strict filters:** Only 11 pairs have ≥$5M volume and ≤0.05% spread.
- **API failures:** Many `get_spot_ticker()` calls fail (timeouts, rate limits, 4xx/5xx), so only 11 get a successful check and pass.

### C. Return only eligible pairs

Discovery returns only pairs that passed the filters (and didn’t hit an exception). That set becomes the full trading universe.

## 3. What actually cut the count (247 → 11)

So the reduction is caused by **market discovery**:

1. **Universe is set by discovery**  
   The trading universe is overwritten by discovery’s mapping. The 247 from “Coin Universe Enabled” is only the **input** list; the **output** is whatever discovery returns (e.g. 11).

2. **Few pairs pass Registry filters**  
   Either:
   - **Liquidity/spread:** Only 11 pairs meet **min_spot_volume_usd_24h** ($5M) and **max_spread_pct** (0.05%), or  
   - **API:** Many `get_spot_ticker()` calls fail, so only 11 pairs get validated and marked eligible.

3. **“SYMBOL REMOVED”**  
   Those 236 symbols are in the initial config but **not** in discovery’s mapping, so they’re logged as removed. They were never in discovery’s result.

## 4. Levers to change the count

| Goal | What to do |
|------|------------|
| **See why so few pass** | Log or inspect `rejection_reason` in `MarketPair` (volume, spread, or “Filter error”) and check for ticker/API errors. |
| **Loosen filters** | Lower **min_spot_volume_usd_24h** or raise **max_spread_pct** in `config.liquidity_filters` (or equivalent YAML). |
| **Reduce API failures** | Increase timeouts, add retries for `get_spot_ticker()`, or run discovery less often / in off-peak times so rate limits are less of an issue. |
| **Keep a fixed list regardless of discovery** | You’d need a different mode where the universe is **not** replaced by discovery (e.g. only use discovery for “supported or not” and keep the rest of the list from config). That’s a code/design change. |

## 5. Config that affects discovery

- **Liquidity filters** (e.g. under `liquidity_filters` or where `LiquidityFilters` is bound):
  - `min_spot_volume_usd_24h` — default $5M
  - `max_spread_pct` — default 0.05%
  - `min_price_usd` — default $0.01
- **Kraken key format** — spot vs futures key format in `get_spot_markets` / `get_futures_markets` must match in `_build_mappings`, or you get fewer mappings before filters even run.

Summary: **the coin count goes down because discovery intentionally replaces the universe with only Kraken-supported pairs that pass liquidity/spread checks and successful ticker fetches.** To get more than 11 coins, relax those filters or fix the ticker/API failures that cause most pairs to be rejected.
