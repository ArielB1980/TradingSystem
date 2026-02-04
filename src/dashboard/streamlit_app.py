"""
Trading System Dashboard - Log Viewer

Displays system logs in a human-readable format.
"""
import sys
from pathlib import Path
from datetime import datetime
import json
import re

# Ensure project root is in path for imports
project_root = Path(__file__).parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import streamlit as st

# Page config
st.set_page_config(
    page_title="Trading System Logs",
    page_icon="📊",
    layout="wide",
)

# Custom CSS for better readability
st.markdown("""
<style>
.log-entry {
    padding: 8px 12px;
    margin: 4px 0;
    border-radius: 6px;
    font-family: 'SF Mono', 'Monaco', 'Inconsolata', monospace;
    font-size: 13px;
    line-height: 1.5;
}
.log-info { background-color: #1a2332; border-left: 3px solid #4CAF50; }
.log-warning { background-color: #2d2a1a; border-left: 3px solid #FF9800; }
.log-error { background-color: #2d1a1a; border-left: 3px solid #f44336; }
.log-debug { background-color: #1a1a2d; border-left: 3px solid #9E9E9E; }
.log-time { color: #888; font-size: 12px; }
.log-event { color: #fff; font-weight: 600; font-size: 14px; }
.log-detail { color: #aaa; font-size: 12px; margin-top: 4px; }
.log-symbol { color: #64B5F6; font-weight: 600; }
.log-value { color: #81C784; }
.log-module { color: #666; font-size: 11px; }
</style>
""", unsafe_allow_html=True)

st.title("📊 Trading System Monitor")

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
    """Parse a JSON log line into a readable format."""
    try:
        data = json.loads(line.strip())
        return data
    except json.JSONDecodeError:
        return {"raw": line.strip(), "level": "info"}


def format_log_entry_html(entry: dict) -> str:
    """Format a log entry as styled HTML."""
    if "raw" in entry:
        return f'<div class="log-entry log-info">{entry["raw"]}</div>'
    
    # Extract common fields
    timestamp = entry.get("timestamp", "")[:19].replace("T", " ").replace("Z", "")
    level = entry.get("level", "info").lower()
    event = entry.get("event", "")
    logger = entry.get("logger", "").replace("src.", "").replace(".", " › ")
    
    # Determine CSS class
    level_class = f"log-{level}" if level in ["info", "warning", "error", "debug"] else "log-info"
    
    # Level icons
    level_icons = {
        "info": "ℹ️",
        "warning": "⚠️",
        "error": "❌",
        "critical": "🔥",
        "debug": "🔍",
    }
    icon = level_icons.get(level, "📝")
    
    # Build details section
    skip_keys = {"timestamp", "level", "event", "logger", "exc_info"}
    details = {k: v for k, v in entry.items() if k not in skip_keys and v is not None and v != ""}
    
    # Format details nicely
    detail_parts = []
    
    # Prioritize important fields
    priority_fields = ["symbol", "side", "size", "price", "pnl", "action", "reason", "count"]
    for key in priority_fields:
        if key in details:
            val = details.pop(key)
            if key == "symbol":
                detail_parts.append(f'<span class="log-symbol">{val}</span>')
            elif key in ["pnl", "price", "size"]:
                detail_parts.append(f'{key}: <span class="log-value">{val}</span>')
            else:
                detail_parts.append(f'{key}: {val}')
    
    # Add remaining fields
    for key, val in list(details.items())[:6]:
        # Truncate long values
        val_str = str(val)
        if len(val_str) > 60:
            val_str = val_str[:57] + "..."
        detail_parts.append(f'{key}: {val_str}')
    
    details_html = " &nbsp;│&nbsp; ".join(detail_parts) if detail_parts else ""
    
    html = f'''
    <div class="log-entry {level_class}">
        <span class="log-time">{timestamp}</span> {icon}
        <span class="log-event">{event}</span>
        <span class="log-module">[{logger}]</span>
        <div class="log-detail">{details_html}</div>
    </div>
    '''
    return html


def load_logs(log_file: Path, num_lines: int = 200) -> list:
    """Load the last N lines from a log file."""
    if not log_file.exists():
        return []
    
    try:
        with open(log_file, 'r') as f:
            lines = f.readlines()
        return lines[-num_lines:]
    except Exception as e:
        return [f"Error reading log: {e}"]


def matches_filter(entry: dict, text_filter: str, level_filter: list) -> bool:
    """Check if entry matches the current filters."""
    # Check level filter
    level = entry.get("level", "info").lower()
    if level not in [l.lower() for l in level_filter]:
        return False
    
    # Check text filter
    if text_filter:
        text_filter_lower = text_filter.lower()
        # Search in all string values
        searchable = json.dumps(entry).lower()
        if text_filter_lower not in searchable:
            return False
    
    return True


# Sidebar controls
st.sidebar.header("🔧 Controls")

# Text search
text_search = st.sidebar.text_input(
    "🔍 Search logs",
    placeholder="symbol, event, message...",
    help="Filter logs containing this text"
)

# Level filter
level_filter = st.sidebar.multiselect(
    "📊 Log levels",
    ["info", "warning", "error", "debug"],
    default=["info", "warning", "error"]
)

# Number of lines
num_lines = st.sidebar.slider("📄 Lines to load", 50, 1000, 200)

# Auto-refresh
auto_refresh = st.sidebar.checkbox("🔄 Auto-refresh (10s)", value=False)

if auto_refresh:
    st.markdown(
        """<meta http-equiv="refresh" content="10">""",
        unsafe_allow_html=True
    )

st.sidebar.divider()

# Quick filters
st.sidebar.subheader("⚡ Quick Filters")
col1, col2 = st.sidebar.columns(2)
with col1:
    if st.button("Trades Only"):
        text_search = "signal"
with col2:
    if st.button("Errors Only"):
        level_filter = ["error", "warning"]

# Main content
col1, col2 = st.columns([3, 1])

with col1:
    st.subheader("📝 Live Trading Log")
    
with col2:
    if st.button("🔄 Refresh Now"):
        st.rerun()

# Get current log file
LIVE_LOG = get_active_log()

# Show log file info
if LIVE_LOG and LIVE_LOG.exists():
    mtime = datetime.fromtimestamp(LIVE_LOG.stat().st_mtime)
    size_kb = LIVE_LOG.stat().st_size / 1024
    st.caption(f"📁 `{LIVE_LOG.name}` • {size_kb:.1f} KB • Updated {mtime.strftime('%H:%M:%S')}")
else:
    st.warning("No log file found")

# Load and display logs
if LIVE_LOG and LIVE_LOG.exists():
    log_lines = load_logs(LIVE_LOG, num_lines)
    
    if log_lines:
        # Parse and filter logs
        entries = []
        for line in reversed(log_lines):  # Most recent first
            entry = parse_log_line(line)
            if matches_filter(entry, text_search, level_filter):
                entries.append(entry)
        
        # Stats bar
        total_loaded = len(log_lines)
        shown = len(entries)
        
        info_count = sum(1 for e in entries if e.get("level", "info") == "info")
        warn_count = sum(1 for e in entries if e.get("level") == "warning")
        error_count = sum(1 for e in entries if e.get("level") == "error")
        
        st.markdown(
            f"**{shown}** entries shown (of {total_loaded} loaded) • "
            f"ℹ️ {info_count} info • ⚠️ {warn_count} warnings • ❌ {error_count} errors"
        )
        
        if text_search:
            st.info(f"🔍 Filtered for: **{text_search}**")
        
        # Display logs in a scrollable container
        log_html = "".join(format_log_entry_html(entry) for entry in entries[:num_lines])
        st.markdown(log_html, unsafe_allow_html=True)
        
        # Raw JSON view
        with st.expander("🔧 View Raw JSON (last 5 entries)"):
            for entry in entries[:5]:
                st.json(entry)
    else:
        st.info("No log entries found")
else:
    st.warning(f"Log file not found: {LIVE_LOG}")
    st.info("The trading system may not be running yet.")

# Footer
st.divider()
st.caption(f"Last refreshed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
