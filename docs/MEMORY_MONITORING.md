# Memory Monitoring

## Server RAM Usage

The trading bot runs on a 1 GB droplet. Typical usage:

| Component | Approx. RAM |
|-----------|-------------|
| Bot (scanning 243 markets) | ~330 MB |
| Dashboard + Postgres + kraken-recorder | ~500 MB |
| **Total** | ~830 MB |

At ~82% RAM usage (176 MB free), any spike (OHLCV fetches, API timeouts) can trigger OOM.

## Mitigations Applied

1. **2 GB swapfile** on server: `fallocate -l 2G /swapfile`, `mkswap`, `swapon`, `vm.swappiness=10`
   - Memory pressure goes to swap instead of OOM killer
   - See [ROOT_CAUSE_2026-02-11.md](ROOT_CAUSE_2026-02-11.md)

## Recommendations

- **Short term**: Monitor `free -m` and swap usage; alerts if swap > 1.5 GB
- **Medium term**: Upgrade to **2 GB RAM** droplet if adding services or symbols
- **Alternative**: Run kraken-recorder / dashboard on a separate box to reduce load

## Quick Checks

```bash
# SSH to server
free -m
# Check swap
swapon --show
# Check OOM history
dmesg | grep -i oom
```
