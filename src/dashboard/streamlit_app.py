"""
Trading System Dashboard - Simple Log Viewer

Displays system logs in a human-readable format.
"""
import sys
import os
from pathlib import Path
from datetime import datetime
import json

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

st.title("📊 Trading System Monitor")

# Log file paths
LOG_DIR = project_root / "logs"
# Check multiple possible log files and use the most recent
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

LIVE_LOG = get_active_log()

def parse_log_line(line: str) -> dict:
    """Parse a JSON log line into a readable format."""
    try:
        data = json.loads(line.strip())
        return data
    except json.JSONDecodeError:
        return {"raw": line.strip()}

def format_log_entry(entry: dict) -> str:
    """Format a log entry for display."""
    if "raw" in entry:
        return entry["raw"]
    
    # Extract common fields
    timestamp = entry.get("timestamp", "")[:19].replace("T", " ")
    level = entry.get("level", "info").upper()
    event = entry.get("event", "")
    logger = entry.get("logger", "").replace("src.", "")
    
    # Color coding for levels
    level_colors = {
        "INFO": "🟢",
        "WARNING": "🟡", 
        "ERROR": "🔴",
        "CRITICAL": "🔴",
        "DEBUG": "⚪",
    }
    level_icon = level_colors.get(level, "⚪")
    
    # Build the message
    parts = [f"{level_icon} **{timestamp}**"]
    
    if event:
        parts.append(f"| {event}")
    
    # Add key details
    skip_keys = {"timestamp", "level", "event", "logger"}
    details = {k: v for k, v in entry.items() if k not in skip_keys and v}
    
    if details:
        detail_str = " | ".join(f"`{k}`: {v}" for k, v in list(details.items())[:5])
        parts.append(f"| {detail_str}")
    
    return " ".join(parts)

def load_logs(log_file: Path, num_lines: int = 100) -> list:
    """Load the last N lines from a log file."""
    if not log_file.exists():
        return []
    
    try:
        with open(log_file, 'r') as f:
            lines = f.readlines()
        return lines[-num_lines:]
    except Exception as e:
        return [f"Error reading log: {e}"]

# Sidebar controls
st.sidebar.header("Controls")
num_lines = st.sidebar.slider("Lines to show", 20, 500, 100)
auto_refresh = st.sidebar.checkbox("Auto-refresh (10s)", value=False)
filter_level = st.sidebar.multiselect(
    "Filter by level",
    ["info", "warning", "error", "critical"],
    default=["info", "warning", "error", "critical"]
)

if auto_refresh:
    st.sidebar.info("Page will refresh every 10 seconds")
    st.markdown(
        """<meta http-equiv="refresh" content="10">""",
        unsafe_allow_html=True
    )

# Main content
col1, col2 = st.columns([3, 1])

with col1:
    st.subheader("📝 Live Trading Log")
    
with col2:
    if st.button("🔄 Refresh"):
        st.rerun()

# Refresh which log file to use
LIVE_LOG = get_active_log()
st.caption(f"📁 Reading from: `{LIVE_LOG.name}`")

# Load and display logs
if LIVE_LOG and LIVE_LOG.exists():
    log_lines = load_logs(LIVE_LOG, num_lines)
    
    if log_lines:
        # Parse and filter logs
        entries = []
        for line in reversed(log_lines):  # Most recent first
            entry = parse_log_line(line)
            level = entry.get("level", "info")
            if level in filter_level:
                entries.append(entry)
        
        # Display stats
        st.markdown(f"**Showing {len(entries)} entries** (most recent first)")
        
        # Display logs
        for entry in entries[:num_lines]:
            formatted = format_log_entry(entry)
            st.markdown(formatted)
            
        # Show raw JSON option
        with st.expander("View Raw JSON (last 10 entries)"):
            for entry in entries[:10]:
                st.json(entry)
    else:
        st.info("No log entries found")
else:
    st.warning(f"Log file not found: {LIVE_LOG}")
    st.info("The trading system may not be running yet.")

# Footer with system status
st.divider()
col1, col2, col3 = st.columns(3)

with col1:
    st.metric("Log File", "live_trading.log")
    
with col2:
    if LIVE_LOG.exists():
        size = LIVE_LOG.stat().st_size / 1024
        st.metric("Log Size", f"{size:.1f} KB")
    else:
        st.metric("Log Size", "N/A")

with col3:
    st.metric("Last Refresh", datetime.now().strftime("%H:%M:%S"))
