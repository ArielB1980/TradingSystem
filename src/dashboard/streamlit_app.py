"""
Streamlit-based Multi-Asset Trading Dashboard.

Professional operational dashboard for monitoring and managing
the multi-asset SMC trading system.
"""
import streamlit as st
import pandas as pd
from datetime import datetime, timezone

from src.dashboard.utils import (
    get_portfolio_metrics,
    get_all_positions,
    get_coin_snapshots,
    get_system_status,
    get_event_feed,
    format_reason_code,
)

# Page config
st.set_page_config(
    page_title="SMC Trading Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom CSS
st.markdown("""
<style>
    .stMetric {
        background-color: #1E1E1E;
        padding: 10px;
        border-radius: 5px;
    }
    .big-font {
        font-size: 20px !important;
        font-weight: bold;
    }
    .success-box {
        padding: 10px;
        border-radius: 5px;
        background-color: #1e4620;
        color: #4ade80;
    }
    .warning-box {
        padding: 10px;
        border-radius: 5px;
        background-color: #4a3a1a;
        color: #fbbf24;
    }
    .danger-box {
        padding: 10px;
        border-radius: 5px;
        background-color: #4a1a1a;
        color: #f87171;
    }
</style>
""", unsafe_allow_html=True)

# Sidebar
st.sidebar.title("📊 SMC Trading Dashboard")
st.sidebar.markdown("---")

page = st.sidebar.radio(
    "Navigation",
    ["Portfolio Overview", "Coin Matrix", "Coin Detail", "Execution Monitor", "Performance"]
)

st.sidebar.markdown("---")

# System status sidebar
status = get_system_status()
st.sidebar.markdown("**System Status**")
st.sidebar.success(f"✅ Mode: {status['mode']}")

if status['spot_feed_health']:
    st.sidebar.success("✅ Spot Feed: Healthy")
else:
    st.sidebar.error("❌ Spot Feed: Unhealthy")

if status['futures_feed_health']:
    st.sidebar.success("✅ Futures Feed: Healthy")
else:
    st.sidebar.error("❌ Futures Feed: Unhealthy")

if status['kill_switch']:
    st.sidebar.error("🔴 KILL SWITCH: TRIGGERED")
else:
    st.sidebar.info("🔵 Kill Switch: ARMED")

st.sidebar.caption(f"Last Updated: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}")

# ============================================================
# SCREEN A: PORTFOLIO OVERVIEW
# ============================================================
if page == "Portfolio Overview":
    st.title("📊 Portfolio Overview")
    
    # System State Banner
    st.subheader("System State")
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        mode_delta = "Safe" if status['mode'] != "PROD" else "Live"
        st.metric("Mode", status['mode'], delta=mode_delta)
    
    with col2:
        kill_status = "🔴 TRIGGERED" if status['kill_switch'] else "🟢 ARMED"
        st.metric("Kill Switch", kill_status)
    
    with col3:
        st.metric("Trading Status", status['trading_status'])
    
    with col4:
        st.metric("Last Recon", f"{status['last_recon_seconds']}s ago")
    
    st.markdown("---")
    
    # Portfolio Risk Strip
    st.subheader("Portfolio Risk Metrics")
    metrics = get_portfolio_metrics()
    
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        st.metric("Equity", f"${metrics['equity']:,.2f}")
    
    with col2:
        margin_pct = (metrics['margin_used'] / metrics['equity'] * 100) if metrics['equity'] > 0 else 0
        st.metric(
            "Margin",
            f"${metrics['margin_used']:,.0f} / ${metrics['equity']:,.0f}",
            delta=f"{margin_pct:.1f}% used"
        )
    
    with col3:
        eff_lev = metrics['effective_leverage']
        st.metric("Effective Leverage", f"{eff_lev:.2f}×")
    
    with col4:
        daily_pnl = metrics['daily_pnl']
        daily_pnl_pct = (daily_pnl / metrics['equity'] * 100) if metrics['equity'] > 0 else 0
        st.metric("Daily PnL", f"${daily_pnl:,.2f}", delta=f"{daily_pnl_pct:+.2f}%")
    
    col5, col6 = st.columns(2)
    
    with col5:
        positions_pct = (metrics['active_positions'] / metrics['max_positions'] * 100)
        st.metric(
            "Active Positions",
            f"{metrics['active_positions']} / {metrics['max_positions']}",
            delta=f"{positions_pct:.0f}% utilized"
        )
    
    with col6:
        available = metrics['max_positions'] - metrics['active_positions']
        st.metric("Available Slots", available)
    
    st.markdown("---")
    
    # Open Positions Table
    st.subheader("Open Positions")
    positions = get_all_positions()
    
    if positions:
        df = pd.DataFrame([
            {
                "Coin": p["symbol"],
                "Side": p["side"],
                "Size": f"${p['notional']:,.0f}",
                "Entry": f"${p['entry_price']:,.2f}",
                "Current": f"${p['current_price']:,.2f}",
                "PnL": f"${p['unrealized_pnl']:,.2f}",
                "Liq Price": f"${p['liq_price']:,.2f}" if p['liq_price'] > 0 else "N/A",
                "Liq Dist %": f"{p['liq_distance_pct']:.1f}%" if p['liq_distance_pct'] > 0 else "N/A",
                "Stop": f"${p['stop_price']:,.2f}" if p['stop_price'] > 0 else "N/A",
                "TPs": p["tp_status"],
                "Trailing": "✅" if p["trailing_active"] else "❌",
                "Flags": ", ".join(p["risk_flags"]) if p["risk_flags"] else "None",
            }
            for p in positions
        ])
        
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.info("No open positions")
    
    st.markdown("---")
    
    # Recent Events Feed
    st.subheader("Recent Events")
    
    # Filter options
    col1, col2 = st.columns([3, 1])
    with col1:
        event_symbol_filter = st.selectbox("Symbol Filter", ["All"] + list(get_coin_snapshots().keys()))
    with col2:
        event_limit = st.number_input("Limit", min_value=10, max_value=200, value=50)
    
    symbol_filter = None if event_symbol_filter == "All" else event_symbol_filter
    events = get_event_feed(limit=event_limit, symbol=symbol_filter)
    
    if events:
        events_df = pd.DataFrame([
            {
                "Time": e["timestamp"],
                "Type": e["type"],
                "Symbol": e["symbol"],
                "Message": e["message"],
            }
            for e in events
        ])
        
        st.dataframe(events_df, use_container_width=True, hide_index=True)
    else:
        st.info("No recent events")

# ============================================================
# SCREEN B: COIN MATRIX (MOST IMPORTANT)
# ============================================================
elif page == "Coin Matrix":
    st.title("🎯 Coin Matrix - Multi-Asset Opinion Board")
    st.caption("Real-time view of system's opinion on every tracked coin")
    
    # Global Filters
    st.subheader("Filters")
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        show_filter = st.selectbox(
            "Show",
            ["All", "ENTER Candidates", "BLOCKED", "In Position", "LONG Signal", "SHORT Signal"]
        )
    
    with col2:
        min_quality = st.slider("Min Setup Quality", 0, 100, 0, 5)
    
    with col3:
        max_basis = st.slider("Max Basis %", 0.0, 2.0, 2.0, 0.1)
    
    with col4:
        max_spread = st.slider("Max Spread %", 0.0, 1.0, 1.0, 0.05)
    
    st.markdown("---")
    
    # Get coin snapshots
    snapshots = get_coin_snapshots()
    
    # Build table rows
    rows = []
    for symbol, snap in snapshots.items():
        # Apply filters
        if show_filter == "ENTER Candidates" and snap.next_action != "ENTER":
            continue
        if show_filter == "BLOCKED" and not snap.block_reason_codes:
            continue
        if show_filter == "In Position" and not snap.pos_side:
            continue
        if show_filter == "LONG Signal" and snap.signal != "LONG":
            continue
        if show_filter == "SHORT Signal" and snap.signal != "SHORT":
            continue
        
        if snap.setup_quality < min_quality:
            continue
        if abs(float(snap.basis_pct)) > max_basis:
            continue
        if float(snap.spread_pct) > max_spread:
            continue
        
        # Build row
        rows.append({
            "Coin": symbol,
            "Signal": snap.signal,
            "Quality": f"{snap.setup_quality:.0f}",
            "Bias": snap.bias_htf,
            "Regime": snap.regime,
            "ADX": f"{float(snap.adx):.1f}",
            "OB": f"${float(snap.ob_level):,.0f}" if snap.ob_level else "-",
            "FVG": f"${float(snap.fvg_band[0]):,.0f}-${float(snap.fvg_band[1]):,.0f}" if snap.fvg_band else "-",
            "Basis %": f"{float(snap.basis_pct):.3f}",
            "Spread %": f"{float(snap.spread_pct):.3f}",
            "Funding %": f"{float(snap.funding_rate):.3f}",
            "Action": snap.next_action,
            "Block Reason": snap.block_reason_codes[0] if snap.block_reason_codes else "-",
            "Position": snap.pos_side if snap.pos_side else "-",
            "Liq Dist %": f"{float(snap.liq_distance_pct):.1f}" if snap.liq_distance_pct else "-",
        })
    
    # Display matrix
    if rows:
        df = pd.DataFrame(rows)
        
        # Color code by signal
        def color_signal(val):
            if val == "LONG":
                return 'background-color: #1e4620; color: #4ade80'
            elif val == "SHORT":
                return 'background-color: #4a1a1a; color: #f87171'
            return ''
        
        # Display with styling
        st.dataframe(
            df.style.applymap(color_signal, subset=['Signal']),
            use_container_width=True,
            hide_index=True,
            height=600
        )
        
        st.caption(f"Showing {len(rows)} coins matching filters")
        
        # Reason code legend
        if any(row["Block Reason"] != "-" for row in rows):
            with st.expander("📖 Rejection Reason Codes"):
                unique_reasons = set(row["Block Reason"] for row in rows if row["Block Reason"] != "-")
                for reason in sorted(unique_reasons):
                    st.markdown(f"**{reason}**: {format_reason_code(reason)}")
    
    else:
        st.warning("No coins match the current filters")

# Other placeholder pages
elif page == "Coin Detail":
    st.title("Coin Detail - Deep Dive")
    symbol = st.selectbox("Select Coin", list(get_coin_snapshots().keys()))
    st.warning(f"🚧 {symbol} detail view coming in next sprint")

elif page == "Execution Monitor":
    st.title("Execution Monitor")
    st.warning("🚧 Execution monitor coming in next sprint")

elif page == "Performance":
    st.title("Performance & Attribution")
    st.warning("🚧 Performance dashboard coming in next sprint")
