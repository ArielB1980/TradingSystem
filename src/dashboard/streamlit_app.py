"""
Streamlit-based Multi-Asset Trading Dashboard.

Professional operational dashboard for monitoring and managing
the multi-asset SMC trading system.
"""
import streamlit as st
import pandas as pd
from datetime import datetime, timezone
import time

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
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Premium Modern CSS
st.markdown("""
<style>
    /* Main Background */
    .stApp {
        background-color: #0E1117;
        color: #FAFAFA;
    }
    
    /* Metrics Cards */
    div[data-testid="stMetricValue"] {
        font-size: 28px;
        color: #00FFD1;
        font-family: 'SF Mono', monospace;
    }
    div[data-testid="stMetricLabel"] {
        color: #A0A0A0;
    }
    .stMetric {
        background-color: #1A1C24;
        border: 1px solid #2D3748;
        padding: 15px;
        border-radius: 10px;
        box-shadow: 0 4px 6px rgba(0, 0, 0, 0.3);
    }
    
    /* Tables */
    .stDataFrame {
        border: 1px solid #2D3748;
        border-radius: 5px;
    }
    
    /* Sidebar */
    section[data-testid="stSidebar"] {
        background-color: #13151C;
        border-right: 1px solid #2D3748;
    }
    
    /* Custom Status Boxes */
    .success-box {
        padding: 4px 8px;
        border-radius: 4px;
        background-color: rgba(39, 174, 96, 0.2);
        color: #2ecc71;
        border: 1px solid #2ecc71;
        font-weight: bold;
    }
    .warning-box {
        padding: 4px 8px;
        border-radius: 4px;
        background-color: rgba(243, 156, 18, 0.2);
        color: #f1c40f;
        border: 1px solid #f1c40f;
        font-weight: bold;
    }
    .danger-box {
        padding: 4px 8px;
        border-radius: 4px;
        background-color: rgba(192, 57, 43, 0.2);
        color: #e74c3c;
        border: 1px solid #e74c3c;
        font-weight: bold;
    }
</style>
""", unsafe_allow_html=True)

# Sidebar - Auto Refresh
st.sidebar.title("⚡ SMC Terminal")
if st.sidebar.checkbox("🔄 Auto Refresh", value=True):
    time.sleep(2)  # 2s refresh rate
    st.rerun()

st.sidebar.markdown("---")
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
            "SMC": f"{snap.score_breakdown.get('smc', 0):.0f}",
            "Fib": f"{snap.score_breakdown.get('fib', 0):.0f}",
            "HTF": f"{snap.score_breakdown.get('htf', 0):.0f}",
            "ADX Score": f"{snap.score_breakdown.get('adx', 0):.0f}",
            "Cost": f"{snap.score_breakdown.get('cost', 0):.0f}",
            "Bias": snap.bias_htf,
            "Regime": snap.regime,
            "OB": f"${float(snap.ob_level):,.0f}" if snap.ob_level else "-",
            "FVG": f"${float(snap.fvg_band[0]):,.0f}-${float(snap.fvg_band[1]):,.0f}" if snap.fvg_band else "-",
            "Funding %": f"{float(snap.funding_rate):.3f}",
            "Block Reason": snap.block_reason_codes[0] if snap.block_reason_codes else "-",
            "Position": snap.pos_side if snap.pos_side else "-",
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
    
    # 1. Selector
    symbols = list(get_coin_snapshots().keys())
    if not symbols:
        st.warning("No data available yet.")
    else:
        symbol = st.selectbox("Select Coin", symbols)
        snap = get_coin_snapshots().get(symbol)
        
        if snap:
            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric("Price", f"${float(snap.spot_price):,.2f}")
            with col2:
                st.metric("Signal", snap.signal)
            with col3:
                st.metric("Quality Score", f"{snap.setup_quality:.0f}/100")
            
            st.markdown("---")
            
            # 2. Score Breakdown
            st.subheader("Signal Score Breakdown")
            scores = snap.score_breakdown
            if scores:
                cols = st.columns(5)
                cols[0].metric("SMC Quality", f"{scores.get('smc', 0):.0f}")
                cols[1].metric("Fib Confluence", f"{scores.get('fib', 0):.0f}")
                cols[2].metric("HTF Align", f"{scores.get('htf', 0):.0f}")
                cols[3].metric("ADX Strength", f"{scores.get('adx', 0):.0f}")
                cols[4].metric("Cost Eff.", f"{scores.get('cost', 0):.0f}")
            else:
                st.info("No active signal scoring data.")
            
            st.markdown("---")
            
            # 3. Market State
            st.subheader("Market State")
            col1, col2, col3 = st.columns(3)
            col1.info(f"**Bias:** {snap.bias_htf}")
            col2.info(f"**Regime:** {snap.regime}")
            if snap.pos_side:
                col3.success(f"**Position:** {snap.pos_side} (${float(snap.pos_notional):,.0f})")
            else:
                col3.info("**Position:** Flat")
                
            # 4. Recent Events for Coin
            st.subheader(f"Recent Events: {symbol}")
            events = get_event_feed(limit=20, symbol=symbol)
            if events:
                st.dataframe(pd.DataFrame(events), use_container_width=True)
            else:
                st.caption("No recent events for this symbol.")

elif page == "Execution Monitor":
    st.title("Execution Monitor")
    
    st.subheader("Recent Executions")
    trades = get_all_trades()
    
    if trades:
        # Convert to nice table
        data = []
        for t in trades:
            data.append({
                "Date": t.exited_at.strftime("%Y-%m-%d %H:%M"),
                "Symbol": t.symbol,
                "Side": t.side.value,
                "PnL": float(t.net_pnl),
                "Entry": float(t.entry_price),
                "Exit": float(t.exit_price),
                "Reason": t.exit_reason
            })
        
        df = pd.DataFrame(data)
        
        # Style PnL
        def color_pnl(val):
            color = '#4ade80' if val > 0 else '#f87171'
            return f'color: {color}'
            
        st.dataframe(
            df.style.applymap(color_pnl, subset=['PnL']),
            use_container_width=True,
            hide_index=True
        )
    else:
        st.info("No executions recorded yet.")

elif page == "Performance":
    st.title("Performance & Attribution")
    
    trades = get_all_trades()
    if not trades:
        st.info("Insufficient data for performance analysis.")
    else:
        # Basic Stats
        total_trades = len(trades)
        wins = len([t for t in trades if t.net_pnl > 0])
        losses = len([t for t in trades if t.net_pnl <= 0])
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
        total_pnl = sum([t.net_pnl for t in trades])
        
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Net PnL", f"${float(total_pnl):,.2f}")
        col2.metric("Win Rate", f"{win_rate:.1f}%")
        col3.metric("Trades", total_trades)
        col4.metric("Win/Loss", f"{wins}/{losses}")
        
        st.markdown("---")
        
        # Simple Equity Curve (Cumulative PnL)
        st.subheader("Cumulative PnL Curve")
        
        # Sort by date
        sorted_trades = sorted(trades, key=lambda x: x.exited_at)
        
        equity_data = []
        running_pnl = 0.0
        for t in sorted_trades:
            running_pnl += float(t.net_pnl)
            equity_data.append({
                "Date": t.exited_at,
                "Cumulative PnL": running_pnl
            })
            
        if equity_data:
            df_chart = pd.DataFrame(equity_data).set_index("Date")
            st.line_chart(df_chart)

    # Future: Advanced Attribution (Regime-based, Time-based, etc.)
    st.info("Advanced attribution metrics will populate as more trading data is collected.")
