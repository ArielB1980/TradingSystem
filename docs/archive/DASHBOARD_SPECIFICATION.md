# Trading System Dashboard - Specification Report

## Overview

A real-time log viewer dashboard built with Streamlit that displays trading system logs in a human-readable, filterable format with visual highlighting for quick scanning.

**URL**: `http://<server-ip>:8080`

---

## Technical Stack

| Component | Technology | Version |
|-----------|------------|---------|
| Framework | Streamlit | Latest |
| Language | Python | 3.12+ |
| Styling | Custom CSS (inline) | - |
| Timezone | CET (UTC+1) | - |

### Dependencies

```txt
streamlit>=1.28.0
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Streamlit App                         │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────┐  │
│  │  Sidebar    │  │  Main View  │  │  Footer Stats   │  │
│  │  Controls   │  │  Log Entries│  │                 │  │
│  └─────────────┘  └─────────────┘  └─────────────────┘  │
└─────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────┐
│                    Log Parser                            │
│  ┌─────────────┐  ┌─────────────┐                       │
│  │ JSON Parser │  │ Structured  │                       │
│  │ (fallback)  │  │ Text Parser │                       │
│  └─────────────┘  └─────────────┘                       │
└─────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────┐
│                    Log Files                             │
│  run.log  │  live_trading.log  (auto-selects newest)    │
└─────────────────────────────────────────────────────────┘
```

---

## Log Format Support

### Format 1: Structured Text (Primary)

```
2026-02-04T15:36:15.421571Z [info ] Event message [module.name] key=value key2=value2
```

**Regex Pattern:**
```python
r'^(\d{4}-\d{2}-\d{2}T[\d:.]+Z?)\s+\[(\w+)\s*\]\s+(.+?)\s+\[([^\]]+)\]\s*(.*)$'
```

**Parsed Fields:**
| Field | Description | Example |
|-------|-------------|---------|
| `timestamp` | ISO 8601 UTC | `2026-02-04T15:36:15.421571Z` |
| `level` | Log level | `info`, `warning`, `error` |
| `event` | Main message | `V2 sync_with_exchange complete` |
| `logger` | Module path | `src.live.live_trading` |
| `key=value` | Additional data | `symbol=PF_DOTUSD`, `count=2` |

### Format 2: JSON (Fallback)

```json
{"timestamp": "2026-02-04T15:36:15Z", "level": "info", "event": "Message", "symbol": "BTC/USD"}
```

---

## UI Components

### 1. Sidebar Controls

| Control | Type | Default | Description |
|---------|------|---------|-------------|
| Search | Text input | Empty | Filters logs containing text |
| Log levels | Multi-select | `info, warning, error` | Filter by level |
| Lines to load | Slider | 200 | Range: 50-1000 |
| Auto-refresh | Checkbox | Off | Refreshes every 10 seconds |
| Quick Filters | Buttons | - | "Trades Only", "Errors Only" |

### 2. Main Log View

Each log entry is rendered as styled HTML:

```html
<div class="log-entry log-{level}">
    <div class="log-header">
        <span class="log-time">16:36:15</span>
        <span class="log-symbol">PF_DOTUSD</span>
        <span class="log-action">BUY</span>
        <span class="log-event">Order placed</span>
    </div>
    <div class="log-detail">size: 10 · price: 1234.56 · reason: signal</div>
</div>
```

### 3. Stats Bar

Displays: `{shown} entries shown (of {loaded} loaded) • ℹ️ X info • ⚠️ Y warnings • ❌ Z errors`

### 4. Footer

- Log file name and size
- Last refresh timestamp

---

## Styling Specification

### CSS Classes

```css
/* Base entry */
.log-entry {
    padding: 10px 14px;
    margin: 6px 0;
    border-radius: 8px;
    font-family: 'SF Mono', 'Monaco', 'Inconsolata', monospace;
    font-size: 13px;
    line-height: 1.6;
}

/* Level-specific backgrounds and borders */
.log-info    { background: #1a2332; border-left: 4px solid #4CAF50; }
.log-warning { background: #2d2a1a; border-left: 4px solid #FF9800; }
.log-error   { background: #2d1a1a; border-left: 4px solid #f44336; }
.log-debug   { background: #1a1a2d; border-left: 4px solid #9E9E9E; }

/* Time - subtle */
.log-time { color: #666; font-size: 11px; min-width: 50px; }

/* Symbol badge - blue gradient */
.log-symbol { 
    background: linear-gradient(135deg, #1565C0, #1976D2);
    color: #fff; 
    font-weight: 700; 
    padding: 2px 8px; 
    border-radius: 4px;
    font-size: 12px;
}

/* Action badge - purple gradient */
.log-action { 
    background: linear-gradient(135deg, #7B1FA2, #9C27B0);
    color: #fff; 
    font-weight: 600; 
    padding: 2px 8px; 
    border-radius: 4px;
    font-size: 12px;
}

/* Buy/Sell specific colors */
.log-buy  { background: linear-gradient(135deg, #2E7D32, #43A047) !important; }
.log-sell { background: linear-gradient(135deg, #C62828, #E53935) !important; }

/* Event text */
.log-event { color: #e0e0e0; font-weight: 500; font-size: 13px; }

/* Details row */
.log-detail { color: #999; font-size: 11px; margin-top: 6px; }

/* Values highlighted in green */
.log-value { color: #81C784; font-weight: 500; }
```

### Color Palette

| Element | Color | Hex |
|---------|-------|-----|
| Info border | Green | `#4CAF50` |
| Warning border | Orange | `#FF9800` |
| Error border | Red | `#f44336` |
| Symbol badge | Blue | `#1976D2` |
| Action badge | Purple | `#9C27B0` |
| Buy action | Green | `#43A047` |
| Sell action | Red | `#E53935` |
| Value highlight | Light green | `#81C784` |

---

## Timezone Handling

All timestamps are converted from UTC to CET (Central European Time, UTC+1):

```python
from datetime import timezone, timedelta

CET = timezone(timedelta(hours=1))

def parse_timestamp_to_cet(ts_str: str) -> str:
    """Convert ISO timestamp to clean CET format."""
    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    dt_utc = dt.replace(tzinfo=timezone.utc)
    dt_cet = dt_utc.astimezone(CET)
    return dt_cet.strftime("%H:%M:%S")
```

**Note**: For CEST (summer time, UTC+2), change `timedelta(hours=1)` to `timedelta(hours=2)` or use `pytz`/`zoneinfo` for automatic DST handling.

---

## Log File Selection

The dashboard automatically selects the most recently modified log file:

```python
LOG_FILES = [
    LOG_DIR / "run.log",
    LOG_DIR / "live_trading.log",
]

def get_active_log() -> Path:
    """Get the most recently modified log file."""
    best = None
    best_time = 0
    for lf in LOG_FILES:
        if lf.exists():
            mtime = lf.stat().st_mtime
            if mtime > best_time:
                best_time = mtime
                best = lf
    return best or LOG_FILES[0]
```

---

## Filtering Logic

### Text Search

Searches across all fields (case-insensitive):

```python
def matches_filter(entry: dict, text_filter: str, level_filter: list) -> bool:
    # Check level
    if entry.get("level", "info").lower() not in [l.lower() for l in level_filter]:
        return False
    
    # Check text (searches entire JSON representation)
    if text_filter:
        searchable = json.dumps(entry).lower()
        if text_filter.lower() not in searchable:
            return False
    
    return True
```

### Priority Fields

Fields displayed prominently in badges/header:
- `symbol` → Blue badge
- `side` → Green (buy) / Red (sell) badge
- `action` → Purple badge

Fields displayed in details row (priority order):
1. `size`, `price`, `pnl` (with green value highlighting)
2. `reason`, `count`, `timeframe`, `source`
3. Remaining fields (max 4, truncated at 40 chars)

---

## Deployment

### Requirements

```bash
# Install dependencies
pip install streamlit

# Run locally
streamlit run src/dashboard/streamlit_app.py --server.port 8080

# Run on server (headless)
nohup streamlit run src/dashboard/streamlit_app.py \
    --server.port 8080 \
    --server.address 0.0.0.0 \
    --server.headless true > /tmp/streamlit.log 2>&1 &
```

### Firewall

Ensure port 8080 is open:

```bash
ufw allow 8080/tcp
```

### Auto-Restart (Systemd)

```ini
# /etc/systemd/system/trading-dashboard.service
[Unit]
Description=Trading System Dashboard
After=network.target

[Service]
Type=simple
User=trading
WorkingDirectory=/home/trading/TradingSystem
ExecStart=/home/trading/TradingSystem/venv/bin/streamlit run src/dashboard/streamlit_app.py --server.port 8080 --server.address 0.0.0.0 --server.headless true
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable trading-dashboard
sudo systemctl start trading-dashboard
```

---

## File Structure

```
src/dashboard/
└── streamlit_app.py    # Main application (single file)

logs/
├── run.log             # Primary log file
└── live_trading.log    # Alternative log file
```

---

## Replication Checklist

To replicate this dashboard for another project:

1. **Copy** `src/dashboard/streamlit_app.py`
2. **Update** `LOG_FILES` list with your log file paths
3. **Update** log parsing regex if your format differs
4. **Update** priority fields for badges (symbol, action, side)
5. **Update** timezone if not CET
6. **Install** `streamlit` dependency
7. **Configure** firewall and run command

---

## Known Limitations

1. **No authentication** - Dashboard is publicly accessible
2. **No log rotation handling** - Only reads current file
3. **Memory usage** - Loads N lines into memory
4. **No persistent filters** - Reset on page reload
5. **Manual DST** - CET offset is hardcoded (no automatic summer time)

---

## Future Enhancements (Optional)

- [ ] Add basic authentication (Streamlit secrets)
- [ ] Support multiple log files simultaneously
- [ ] Add log download button
- [ ] Persist filter preferences in URL params
- [ ] Add charts for error frequency over time
- [ ] WebSocket for true real-time updates (vs polling)
