# Positions Closed & Open Orders – FAQ

## “The system closed all positions – intentional or bug?”

**“All positions closed”** means the **exchange** reports 0 open positions (you see this as “Active Portfolio: 0 positions” in logs).

### When we **intentionally** close

1. **Stop loss hit** – Price crosses stop → we send a reduce‑only market close.
2. **Premise invalidation** – Bias flips (e.g. long vs EMA200) → we close that position.
3. **Targets hit** – TP1/TP2 partial closes, final target full close.
4. **Kill switch** – Emergency mode flattens **all** positions.
5. **Replacement** – We close one symbol to open a better one (opportunity‑cost replacement).
6. **Reversal** – We close before opening the opposite side.

We only **send** close orders in these cases. The rest is the exchange (and possibly manual) closing.

### When it **looks** like we closed but we didn’t

- **Reconciliation “zombies”**  
  We have a position in **our DB**, exchange says we don’t. We **delete it from our DB** only. We do **not** send a close order – the position was already gone on the exchange (e.g. stop filled, manual close, outside closure).

- **Registry “orphans”**  
  V2 registry has a position, exchange doesn’t. We **mark** it orphaned. We don’t place a new close; we may later flatten if we choose to “fix” that symbol on exchange.

So “all closed” can be:

- Us closing via stops/targets/premise/kill/replacement/reversal, or  
- Exchange already flat, and we’re just updating our state (zombie delete / orphan mark).

### How to tell which it was

1. **Logs**  
   Search for: `Exit initiated`, `CLOSE`, `Premise Invalidation`, `Kill switch`, `Reconciliation`, `ZOMBIE`, `ORPHAN`, `FLATTEN`, `replace`, `reversal`.

2. **Exchange history**  
   Check Kraken Futures fills / order history: were those positions closed by **our** orders (same `clientOrderId` / mapping we use) or by something else (e.g. stop‑market fills, manual)?

3. **Kill switch**  
   If it was activated, we explicitly close all positions. Check kill‑switch state and logs.

---

## “The open orders that remain – are they from new signals?”

**Not necessarily.** The “recovered” open orders (e.g. 13) are **all** open orders we sync from the exchange. They can be:

| Type | Meaning |
|------|--------|
| **Entry limits** | From **new** signals, waiting to fill. |
| **Stops** | Protective stops. If there are **no** positions, these are **stale** (left over from positions we already closed – we closed at market but didn’t cancel the stop). |
| **Take‑profits** | Same idea: can be **stale** if the position was closed another way. |

So:

- **With 0 positions**, any remaining **stops** or **TPs** are typically **stale** (bug‑class: we should cancel them when we close the position).
- **Entry** orders are from **new** signals (or older unfilled limits we’re still tracking).

### How to inspect your current open orders

```bash
make audit          # Read‑only: breakdown by type, per‑symbol, multiple stops
make audit-cancel   # Same + cancel redundant stops (keeps most protective per symbol)
```

Use `make audit` first. It prints:

- Counts **by type** (stop, take_profit, limit, etc.).
- **Per‑symbol** counts (and how many stops per symbol).
- **Multiple stops per symbol** (suspicious; usually redundant).

That tells you whether the remaining orders are mostly **stops/TPs** (likely stale) vs **limits** (often new‑signal entries).

---

## Summary

| Question | Short answer |
|----------|--------------|
| **Closed all – intentional or bug?** | Could be either. Check logs (exit reasons, kill switch, reconciliation, flatten) and exchange history. “Zombie” / “orphan” handling does **not** send close orders. |
| **Remaining orders from new signals?** | Only **entry** orders are from new signals. Stops/TPs with **0** positions are usually **stale**; run `make audit` / `make audit-cancel` to verify and clean up. |
