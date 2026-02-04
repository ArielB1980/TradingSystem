"""
Trading System Log Viewer

Clean, readable log viewer with signal filtering.
"""
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
import json
import re

# Ensure project root is in path for imports
project_root = Path(__file__).parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import streamlit as st

# CET timezone (UTC+1)
CET = timezone(timedelta(hours=1))

# Page config
st.set_page_config(
    page_title="Trading Log Viewer",
    page_icon="📋",
    layout="wide",
)

# Custom CSS for clean design
st.markdown("""
<style>
/* Hide Streamlit branding */
#MainMenu {visibility: hidden;}
footer {visibility: hidden;}

/* Main container */
.main .block-container {
    padding-top: 1rem;
    max-width: 100%;
}

/* Log entries */
.log-entry {
    padding: 12px 16px;
    margin: 8px 0;
    border-radius: 8px;
    font-family: 'SF Mono', 'Monaco', 'Consolas', monospace;
    font-size: 13px;
    line-height: 1.6;
    background: #1e1e2e;
    border-left: 4px solid #4CAF50;
}
.log-warning { border-left-color: #FF9800; background: #2d2a1a; }
.log-error { border-left-color: #f44336; background: #2d1a1a; }

/* Header row */
.log-header {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    margin-bottom: 6px;
}

/* Time */
.log-time {
    color: #888;
    font-size: 12px;
    font-weight: 500;
}

/* Badges */
.badge {
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
}
.badge-info { background: #2196F3; color: white; }
.badge-warning { background: #FF9800; color: black; }
.badge-error { background: #f44336; color: white; }
.badge-symbol { background: #1565C0; color: white; font-size: 12px; }
.badge-long { background: #4CAF50; color: white; }
.badge-short { background: #f44336; color: white; }
.badge-score { background: #FF9800; color: black; font-weight: 700; }
.badge-score-high { background: #4CAF50; color: white; }
.badge-score-low { background: #f44336; color: white; }

/* Event text */
.log-event {
    color: #e0e0e0;
    font-weight: 500;
}

/* Details row */
.log-details {
    color: #888;
    font-size: 11px;
    margin-top: 4px;
}
.log-details span {
    margin-right: 12px;
}
.detail-key { color: #666; }
.detail-value { color: #aaa; }
.detail-value-highlight { color: #81C784; }

/* Sidebar styling */
.sidebar .stButton button {
    width: 100%;
    margin-bottom: 4px;
}
</style>
""", unsafe_allow_html=True)

# Log file paths
LOG_DIR = project_root / "logs"
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


def parse_log_line(line: str) -> dict:
    """Parse a log line into a dict."""
    line = line.strip()
    if not line:
        return None
    
    # Skip continuation lines
    if line and line[0] in '✓❌○📊✅⚠️🔴🟢│├└─ \t':
        return None
    
    # Try JSON first
    try:
        data = json.loads(line)
        return data
    except json.JSONDecodeError:
        pass
    
    # Parse structured text format
    pattern = r'^(\d{4}-\d{2}-\d{2}T[\d:.]+Z?)\s+\[(\w+)\s*\]\s+(.+?)\s+\[([^\]]+)\]\s*(.*)$'
    match = re.match(pattern, line)
    
    if match:
        timestamp, level, event, logger, extras = match.groups()
        result = {
            "timestamp": timestamp,
            "level": level.strip(),
            "event": event.strip(),
            "logger": logger.strip(),
        }
        
        # Parse key=value pairs
        kv_pattern = r"(\w+)=(?:'([^']*)'|\"([^\"]*)\"|(\[[^\]]*\])|(\{[^\}]*\})|(\S+))"
        for kv_match in re.finditer(kv_pattern, extras):
            key = kv_match.group(1)
            val = kv_match.group(2) or kv_match.group(3) or kv_match.group(4) or kv_match.group(5) or kv_match.group(6) or ""
            result[key] = val
        
        return result
    
    # Skip non-log lines
    if not line.startswith('20'):
        return None
    
    return None


def parse_timestamp_to_cet(ts_str: str) -> str:
    """Convert ISO timestamp to CET time."""
    if not ts_str:
        return ""
    try:
        ts_clean = ts_str.replace("Z", "+00:00")
        if "." in ts_clean:
            ts_clean = ts_clean.split(".")[0] + "+00:00"
        dt = datetime.fromisoformat(ts_clean.replace("+00:00", ""))
        dt_utc = dt.replace(tzinfo=timezone.utc)
        dt_cet = dt_utc.astimezone(CET)
        return dt_cet.strftime("%H:%M:%S")
    except Exception:
        return ts_str[:8] if len(ts_str) >= 8 else ts_str


def format_log_entry(entry: dict) -> str:
    """Format a log entry as styled HTML."""
    timestamp = parse_timestamp_to_cet(entry.get("timestamp", ""))
    level = entry.get("level", "info").lower()
    event = entry.get("event", "")
    
    # Get key fields
    symbol = entry.get("symbol", "")
    side = entry.get("side", "") or entry.get("signal_type", "")
    score = entry.get("score", "")
    
    # Determine entry class
    entry_class = "log-entry"
    if level == "warning":
        entry_class += " log-warning"
    elif level == "error":
        entry_class += " log-error"
    
    # Build header badges
    badges = []
    
    # Level badge
    level_class = f"badge badge-{level}" if level in ["info", "warning", "error"] else "badge badge-info"
    badges.append(f'<span class="{level_class}">{level.upper()}</span>')
    
    # Symbol badge
    if symbol:
        # Clean up symbol (remove PF_ prefix for display)
        display_symbol = symbol.replace("PF_", "").replace("/USD", "").replace("USD", "")
        badges.append(f'<span class="badge badge-symbol">{display_symbol}</span>')
    
    # Side badge
    if side:
        side_lower = side.lower()
        if side_lower in ["long", "buy"]:
            badges.append('<span class="badge badge-long">LONG</span>')
        elif side_lower in ["short", "sell"]:
            badges.append('<span class="badge badge-short">SHORT</span>')
    
    # Score badge
    if score:
        try:
            score_val = float(score)
            if score_val >= 75:
                score_class = "badge badge-score-high"
            elif score_val < 60:
                score_class = "badge badge-score-low"
            else:
                score_class = "badge badge-score"
            badges.append(f'<span class="{score_class}">Score: {score_val:.0f}</span>')
        except (ValueError, TypeError):
            badges.append(f'<span class="badge badge-score">Score: {score}</span>')
    
    badges_html = " ".join(badges)
    
    # Build details section
    skip_keys = {"timestamp", "level", "event", "logger", "exc_info", "symbol", "action", "side", "score", "signal_type"}
    details = {k: v for k, v in entry.items() if k not in skip_keys and v is not None and v != ""}
    
    detail_parts = []
    # Priority fields
    priority = ["entry", "stop", "signal_id", "reason", "size", "price", "pnl", "ohlcv_source"]
    for key in priority:
        if key in details:
            val = details.pop(key)
            val_str = str(val)
            if len(val_str) > 50:
                val_str = val_str[:47] + "..."
            if key in ["entry", "stop", "price", "pnl"]:
                detail_parts.append(f'<span class="detail-key">{key}:</span> <span class="detail-value-highlight">{val_str}</span>')
            else:
                detail_parts.append(f'<span class="detail-key">{key}:</span> <span class="detail-value">{val_str}</span>')
    
    # Remaining fields (limit)
    for key, val in list(details.items())[:3]:
        val_str = str(val)
        if len(val_str) > 40:
            val_str = val_str[:37] + "..."
        detail_parts.append(f'<span class="detail-key">{key}:</span> <span class="detail-value">{val_str}</span>')
    
    details_html = ""
    if detail_parts:
        details_html = f'<div class="log-details">{" ".join(detail_parts)}</div>'
    
    return f'''
    <div class="{entry_class}">
        <div class="log-header">
            <span class="log-time">{timestamp}</span>
            <span class="log-time">CET</span>
            {badges_html}
            <span class="log-event">{event}</span>
        </div>
        {details_html}
    </div>
    '''


def matches_filter(entry: dict, text_filter: str, level_filter: list, quick_filter: str) -> bool:
    """Check if entry matches filters."""
    level = entry.get("level", "info").lower()
    if level not in [l.lower() for l in level_filter]:
        return False
    
    # Quick filters
    if quick_filter == "signals":
        event = entry.get("event", "").lower()
        has_score = bool(entry.get("score"))
        has_signal = bool(entry.get("signal_type"))
        signal_events = ["signal generated", "signal rejected", "auction candidate"]
        if not (has_score or has_signal or any(e in event for e in signal_events)):
            return False
    elif quick_filter == "auction":
        event = entry.get("event", "").lower()
        if "auction" not in event:
            return False
    elif quick_filter == "rejected":
        event = entry.get("event", "").lower()
        if "rejected" not in event and "failed" not in event:
            return False
    elif quick_filter == "errors":
        if level not in ["error", "warning"]:
            return False
    
    # Text filter
    if text_filter:
        searchable = json.dumps(entry).lower()
        if text_filter.lower() not in searchable:
            return False
    
    return True


def load_logs(log_file: Path, num_lines: int = 500) -> list:
    """Load last N lines from log file."""
    if not log_file.exists():
        return []
    try:
        with open(log_file, 'r') as f:
            lines = f.readlines()
        return lines[-num_lines:]
    except Exception:
        return []


# === SIDEBAR ===
st.sidebar.markdown("### 🔍 Search")
text_search = st.sidebar.text_input("", placeholder="Filter logs...", label_visibility="collapsed")

st.sidebar.markdown("### Log Levels")
col1, col2 = st.sidebar.columns(2)
with col1:
    show_info = st.sidebar.checkbox("info", value=True)
    show_warning = st.sidebar.checkbox("warning", value=True)
with col2:
    show_error = st.sidebar.checkbox("error", value=True)
    show_critical = st.sidebar.checkbox("critical", value=True)

level_filter = []
if show_info:
    level_filter.append("info")
if show_warning:
    level_filter.append("warning")
if show_error:
    level_filter.append("error")
if show_critical:
    level_filter.append("critical")

st.sidebar.markdown("### Lines to Load")
num_lines = st.sidebar.slider("", 100, 2000, 1000, label_visibility="collapsed")

auto_refresh = st.sidebar.checkbox("Auto-refresh (10s)", value=False)
if auto_refresh:
    st.markdown('<meta http-equiv="refresh" content="10">', unsafe_allow_html=True)

st.sidebar.markdown("### Quick Filters")

# Quick filter buttons
quick_filter = ""
col1, col2 = st.sidebar.columns(2)
with col1:
    if st.button("📊 Signals", use_container_width=True):
        st.session_state['quick_filter'] = 'signals'
    if st.button("🚫 Rejected", use_container_width=True):
        st.session_state['quick_filter'] = 'rejected'
with col2:
    if st.button("🏛️ Auction", use_container_width=True):
        st.session_state['quick_filter'] = 'auction'
    if st.button("❌ Errors", use_container_width=True):
        st.session_state['quick_filter'] = 'errors'

if st.sidebar.button("🔄 Clear Filters", use_container_width=True):
    st.session_state['quick_filter'] = ''

quick_filter = st.session_state.get('quick_filter', '')

# === MAIN CONTENT ===
now_cet = datetime.now(CET)
st.markdown(f"""
# 📋 Log Viewer
**All times in CET (Central European Time)** · Currently: **{now_cet.strftime('%H:%M:%S')}** on {now_cet.strftime('%A, %d %B %Y')}
""")

# Get log file
LIVE_LOG = get_active_log()

if LIVE_LOG and LIVE_LOG.exists():
    log_lines = load_logs(LIVE_LOG, num_lines)
    
    if log_lines:
        # Parse and filter
        entries = []
        for line in reversed(log_lines):
            entry = parse_log_line(line)
            if entry and matches_filter(entry, text_search, level_filter, quick_filter):
                entries.append(entry)
        
        # Stats
        info_count = sum(1 for e in entries if e.get("level", "info") == "info")
        warn_count = sum(1 for e in entries if e.get("level") == "warning")
        error_count = sum(1 for e in entries if e.get("level") == "error")
        
        filter_label = f" (filter: **{quick_filter}**)" if quick_filter else ""
        st.markdown(f"**{len(entries)}** entries shown (of {len(log_lines)} loaded){filter_label} · ℹ️ {info_count} info · ⚠️ {warn_count} warnings · ❌ {error_count} errors")
        
        # Render logs
        log_html = "".join(format_log_entry(entry) for entry in entries)
        st.markdown(log_html, unsafe_allow_html=True)
    else:
        st.info("No log entries found")
else:
    st.warning(f"Log file not found: {LIVE_LOG}")
