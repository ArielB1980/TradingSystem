"""
Trading System Dashboard - Log Viewer

Displays system logs in a human-readable format.
"""
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
import json
import re

# CET timezone (UTC+1, or UTC+2 during DST)
CET = timezone(timedelta(hours=1))

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
    padding: 10px 14px;
    margin: 6px 0;
    border-radius: 8px;
    font-family: 'SF Mono', 'Monaco', 'Inconsolata', monospace;
    font-size: 13px;
    line-height: 1.6;
}
.log-info { background-color: #1a2332; border-left: 4px solid #4CAF50; }
.log-warning { background-color: #2d2a1a; border-left: 4px solid #FF9800; }
.log-error { background-color: #2d1a1a; border-left: 4px solid #f44336; }
.log-debug { background-color: #1a1a2d; border-left: 4px solid #9E9E9E; }
.log-header { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.log-time { color: #666; font-size: 11px; min-width: 50px; }
.log-symbol { 
    background: linear-gradient(135deg, #1565C0, #1976D2);
    color: #fff; 
    font-weight: 700; 
    padding: 2px 8px; 
    border-radius: 4px;
    font-size: 12px;
}
.log-action { 
    background: linear-gradient(135deg, #7B1FA2, #9C27B0);
    color: #fff; 
    font-weight: 600; 
    padding: 2px 8px; 
    border-radius: 4px;
    font-size: 12px;
}
.log-event { color: #e0e0e0; font-weight: 500; font-size: 13px; }
.log-detail { color: #999; font-size: 11px; margin-top: 6px; }
.log-value { color: #81C784; font-weight: 500; }
.log-module { color: #555; font-size: 10px; font-style: italic; }
.log-buy { background: linear-gradient(135deg, #2E7D32, #43A047) !important; }
.log-sell { background: linear-gradient(135deg, #C62828, #E53935) !important; }
.log-score { 
    background: linear-gradient(135deg, #F57C00, #FF9800);
    color: #000; 
    font-weight: 700; 
    padding: 2px 8px; 
    border-radius: 4px;
    font-size: 12px;
}
.log-score-high { background: linear-gradient(135deg, #388E3C, #4CAF50) !important; color: #fff; }
.log-score-low { background: linear-gradient(135deg, #D32F2F, #F44336) !important; color: #fff; }
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
    """Parse a log line (JSON or structured text) into a dict."""
    line = line.strip()
    if not line:
        return None
    
    # Try JSON first
    try:
        data = json.loads(line)
        return data
    except json.JSONDecodeError:
        pass
    
    # Parse structured text format:
    # 2026-02-04T15:36:15.421571Z [info ] Event message [module] key=value key2=value2
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
        
        # Parse key=value pairs from extras
        # Handle both simple values and complex values with spaces/brackets
        kv_pattern = r"(\w+)=(?:'([^']*)'|\"([^\"]*)\"|(\[[^\]]*\])|(\{[^\}]*\})|(\S+))"
        for kv_match in re.finditer(kv_pattern, extras):
            key = kv_match.group(1)
            # Find which group matched
            val = kv_match.group(2) or kv_match.group(3) or kv_match.group(4) or kv_match.group(5) or kv_match.group(6) or ""
            result[key] = val
        
        return result
    
    # Fallback - return as raw
    return {"raw": line, "level": "info"}


def parse_timestamp_to_cet(ts_str: str) -> str:
    """Convert ISO timestamp to clean CET format."""
    if not ts_str:
        return ""
    try:
        # Parse ISO format (e.g., 2026-02-04T15:03:42.317965Z)
        ts_clean = ts_str.replace("Z", "+00:00")
        if "." in ts_clean:
            ts_clean = ts_clean.split(".")[0] + "+00:00"
        dt = datetime.fromisoformat(ts_clean.replace("+00:00", ""))
        dt_utc = dt.replace(tzinfo=timezone.utc)
        dt_cet = dt_utc.astimezone(CET)
        return dt_cet.strftime("%H:%M:%S")
    except Exception:
        return ts_str[:8] if len(ts_str) >= 8 else ts_str


def format_log_entry_html(entry: dict) -> str:
    """Format a log entry as styled HTML."""
    if "raw" in entry:
        return f'<div class="log-entry log-info">{entry["raw"]}</div>'
    
    # Extract common fields
    timestamp = parse_timestamp_to_cet(entry.get("timestamp", ""))
    level = entry.get("level", "info").lower()
    event = entry.get("event", "")
    logger = entry.get("logger", "").replace("src.", "")
    
    # Determine CSS class
    level_class = f"log-{level}" if level in ["info", "warning", "error", "debug"] else "log-info"
    
    # Level icons
    level_icons = {
        "info": "",
        "warning": "⚠️",
        "error": "❌",
        "critical": "🔥",
        "debug": "🔍",
    }
    icon = level_icons.get(level, "")
    
    # Extract key fields for highlighting
    symbol = entry.get("symbol", "")
    action = entry.get("action", "")
    side = entry.get("side", "")
    score = entry.get("score", "")
    signal_type = entry.get("signal_type", "")
    
    # Build header with symbol and action prominently displayed
    header_parts = []
    
    # Time first (subtle)
    header_parts.append(f'<span class="log-time">{timestamp}</span>')
    
    # Symbol badge (if present)
    if symbol:
        header_parts.append(f'<span class="log-symbol">{symbol}</span>')
    
    # Action/Side badge (if present) 
    if side:
        side_class = "log-buy" if side.lower() == "buy" else "log-sell"
        header_parts.append(f'<span class="log-action {side_class}">{side.upper()}</span>')
    elif signal_type:
        signal_class = "log-buy" if signal_type.lower() == "long" else "log-sell"
        header_parts.append(f'<span class="log-action {signal_class}">{signal_type.upper()}</span>')
    elif action:
        header_parts.append(f'<span class="log-action">{action}</span>')
    
    # Score badge (if present) - show the actual number prominently
    if score:
        try:
            score_val = float(score)
            if score_val >= 70:
                score_class = "log-score log-score-high"
            elif score_val < 50:
                score_class = "log-score log-score-low"
            else:
                score_class = "log-score"
            header_parts.append(f'<span class="{score_class}">SCORE: {score_val:.1f}</span>')
        except (ValueError, TypeError):
            header_parts.append(f'<span class="log-score">SCORE: {score}</span>')
    
    # Icon for warnings/errors
    if icon:
        header_parts.append(icon)
    
    # Event text
    header_parts.append(f'<span class="log-event">{event}</span>')
    
    header_html = " ".join(header_parts)
    
    # Build details section (remaining fields)
    skip_keys = {"timestamp", "level", "event", "logger", "exc_info", "symbol", "action", "side", "score", "signal_type"}
    details = {k: v for k, v in entry.items() if k not in skip_keys and v is not None and v != ""}
    
    # Format details nicely
    detail_parts = []
    
    # Prioritize important fields
    priority_fields = ["size", "price", "pnl", "reason", "count", "timeframe", "source"]
    for key in priority_fields:
        if key in details:
            val = details.pop(key)
            if key in ["pnl", "price", "size"]:
                detail_parts.append(f'{key}: <span class="log-value">{val}</span>')
            else:
                detail_parts.append(f'{key}: {val}')
    
    # Add remaining fields (limit to prevent overflow)
    for key, val in list(details.items())[:4]:
        val_str = str(val)
        if len(val_str) > 40:
            val_str = val_str[:37] + "..."
        detail_parts.append(f'{key}: {val_str}')
    
    details_html = ""
    if detail_parts:
        details_html = f'<div class="log-detail">{" · ".join(detail_parts)}</div>'
    
    html = f'''
    <div class="log-entry {level_class}">
        <div class="log-header">{header_html}</div>
        {details_html}
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


def matches_filter(entry: dict, text_filter: str, level_filter: list, signals_only: bool = False) -> bool:
    """Check if entry matches the current filters."""
    # Check level filter
    level = entry.get("level", "info").lower()
    if level not in [l.lower() for l in level_filter]:
        return False
    
    # Check signals only filter
    if signals_only:
        event = entry.get("event", "").lower()
        # Match signal-related events
        signal_keywords = [
            "signal", "score", "auction", "candidate", 
            "entry", "approved", "rejected", "contender",
            "winner", "generated", "smc analysis"
        ]
        has_score = bool(entry.get("score"))
        has_signal_type = bool(entry.get("signal_type"))
        event_matches = any(kw in event for kw in signal_keywords)
        
        if not (has_score or has_signal_type or event_matches):
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

# Signals only filter
signals_only = st.sidebar.checkbox("📊 Signals Only", value=False, help="Show only signal-related entries (scores, signals, auctions)")

col1, col2 = st.sidebar.columns(2)
with col1:
    if st.button("Errors Only"):
        level_filter = ["error", "warning"]
with col2:
    if st.button("Clear Filters"):
        text_search = ""
        signals_only = False

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
            if entry and matches_filter(entry, text_search, level_filter, signals_only):
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
