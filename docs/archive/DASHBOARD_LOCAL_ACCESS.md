# Local Dashboard Access

## Quick Start

The dashboard is a **Streamlit web application** that displays real-time trading signals, positions, and system status.

### Start Dashboard

```bash
cd /Users/arielbarack/Documents/TradingSystem
streamlit run src/dashboard/streamlit_app.py --server.port 8501 --server.address 0.0.0.0
```

### Access Dashboard

Once started, open your browser to:
- **Local:** http://localhost:8501
- **Network:** http://[your-ip]:8501

### Dashboard Features

The dashboard displays:
- ✅ **Real-time signals** - All generated signals with details
- ✅ **Position tracking** - Active positions with PnL
- ✅ **Data freshness** - Status of each coin (active/stale/dead)
- ✅ **Signal quality** - Score breakdowns, regime, bias
- ✅ **Portfolio metrics** - Total PnL, position count, risk metrics
- ✅ **Event feed** - Recent system events and trades

### Run in Background

To run the dashboard in the background:

```bash
nohup streamlit run src/dashboard/streamlit_app.py --server.port 8501 --server.address 0.0.0.0 > logs/dashboard.log 2>&1 &
```

### Stop Dashboard

```bash
pkill -f "streamlit run"
```

### Check if Dashboard is Running

```bash
ps aux | grep streamlit | grep -v grep
```

### View Dashboard Logs

```bash
tail -f logs/dashboard.log
```

## Troubleshooting

### Port Already in Use

If port 8501 is in use, use a different port:

```bash
streamlit run src/dashboard/streamlit_app.py --server.port 8502
```

### Database Connection Issues

Ensure:
1. `DATABASE_URL` is set in `.env.local`
2. Database is accessible
3. Database has recent data (run live trading first)

### Dashboard Shows No Data

1. Check that live trading is running: `ps aux | grep "run.py live"`
2. Verify database has data: Check `DECISION_TRACE` events
3. Check dashboard logs for errors

## Production Dashboard

For production (DigitalOcean App Platform), the dashboard runs as a separate component. See `DASHBOARD_URL.md` for production access instructions.
