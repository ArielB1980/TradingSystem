"""
Trading System Dashboard - Single Page Coin Monitor

Displays comprehensive real-time analysis for all tracked coins.
"""
import streamlit as st
import pandas as pd
from datetime import datetime, timezone
from src.dashboard.data_loader import load_all_coins, get_coin_detail
from src.monitoring.logger import get_logger

logger = get_logger(__name__)

# Page config
st.set_page_config(
    page_title="Trading System Monitor",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# Custom CSS
st.markdown("""
<style>
    /* Compact table styling */
    .dataframe {
        font-size: 12px;
        font-family: 'Courier New', monospace;
    }
    
    /* Header styling */
    .main-header {
        font-size: 32px;
        font-weight: bold;
        margin-bottom: 10px;
    }
    
    .status-bar {
        font-size: 14px;
        color: #888;
        margin-bottom: 20px;
    }
    
    /* Quality color coding */
    .quality-high { background-color: #2ecc71; color: white; }
    .quality-mid { background-color: #f39c12; color: white; }
    .quality-low { background-color: #e74c3c; color: white; }
    
    /* Remove extra padding */
    .block-container {
        padding-top: 2rem;
        padding-bottom: 0rem;
    }
</style>
""", unsafe_allow_html=True)

# Sidebar - Performance & Status
with st.sidebar:
    st.header("📊 System Performance")
    
    # Load performance metrics
    from src.monitoring.performance import calculate_performance_metrics
    metrics = calculate_performance_metrics(days=30)
    
    col_pnl, col_win = st.columns(2)
    with col_pnl:
        pnl = metrics.get('total_pnl', 0.0)
        st.metric("30d PnL", f"${pnl:,.2f}", delta=pnl)
    with col_win:
        win_rate = metrics.get('win_rate', 0.0)
        st.metric("Win Rate", f"{win_rate:.1f}%")
        
    st.metric("Sharpe Ratio", f"{metrics.get('sharpe_ratio', 0.0):.2f}")
    st.metric("Max Drawdown", f"{metrics.get('max_drawdown', 0.0):.2f}%", delta_color="inverse")
    
    st.divider()
    
    st.header("⚙️ System Status")
    
    # Kill switch status
    from src.monitoring.kill_switch import get_kill_switch
    ks = get_kill_switch()
    ks_status = ks.get_status()
    
    if ks_status.get('active'):
        st.error(f"🚨 KILL SWITCH ACTIVE")
        st.caption(f"Reason: {ks_status.get('reason')}")
    else:
        st.success("✅ System Operational")
        
    # Active Alerts (Placeholder)
    # st.subheader("🔔 Recent Alerts")
    # ...


# Header
st.markdown('<div class="main-header">🎯 Trading System Monitor</div>', unsafe_allow_html=True)

# Load data
@st.cache_data(ttl=10)
def load_dashboard_data():
    return load_all_coins()

coins = load_dashboard_data()

# Status bar
now = datetime.now(timezone.utc)
active_count = sum(1 for c in coins if c.status == "active")
st.markdown(
    f'<div class="status-bar">⚡ Live: {len(coins)} coins | '
    f'🟢 Active: {active_count} | '
    f'Last refresh: {now.strftime("%H:%M:%S UTC")}</div>',
    unsafe_allow_html=True
)

# Filters
# ... (Filters section unchanged) ...
st.markdown("### Filters")
col1, col2, col3, col4, col5 = st.columns(5)

with col1:
    signal_filter = st.selectbox("Signal", ["All", "LONG", "SHORT", "NO_SIGNAL"], index=0)
with col2:
    regime_filter = st.selectbox("Regime", ["All", "tight_range", "wide_structure", "trending"], index=0)
with col3:
    bias_filter = st.selectbox("Bias", ["All", "bullish", "bearish", "neutral"], index=0)
with col4:
    quality_threshold = st.slider("Min Quality", 0, 100, 0, 5)
with col5:
    status_filter = st.selectbox("Status", ["All", "active", "stale", "dead"], index=0)

# Apply filters
filtered_coins = coins

if signal_filter != "All":
    filtered_coins = [c for c in filtered_coins if c.signal == signal_filter]
if regime_filter != "All":
    filtered_coins = [c for c in filtered_coins if c.regime == regime_filter]
if bias_filter != "All":
    filtered_coins = [c for c in filtered_coins if c.bias == bias_filter]
if quality_threshold > 0:
    filtered_coins = [c for c in filtered_coins if c.quality >= quality_threshold]
if status_filter != "All":
    filtered_coins = [c for c in filtered_coins if c.status == status_filter]

st.markdown(f"**Showing {len(filtered_coins)} of {len(coins)} coins**")

# Build DataFrame
if filtered_coins:
    data = []
    for coin in filtered_coins:
        # Calculate time since last update
        age = (now - coin.last_update).total_seconds()
        if age < 60:
            last_update_str = f"{int(age)}s ago"
        elif age < 3600:
            last_update_str = f"{int(age/60)}m ago"
        else:
            last_update_str = f"{int(age/3600)}h ago"
        
        # Color code 24h change
        change = getattr(coin, 'change_24h', 0.0)
        
        data.append({
            "Status": coin.status_emoji,
            "Symbol": coin.symbol,
            "Price": f"${coin.price:.4f}",
            "24h %": f"{change:+.2f}%",
            "Signal": f"{coin.signal_emoji} {coin.signal}",
            "Regime": coin.regime,
            "Bias": coin.bias,
            "Quality": f"{coin.quality:.0f}",
            "SMC": coin.score_breakdown.get('smc', 0),
            "Fib": coin.score_breakdown.get('fib', 0),
            "HTF": coin.score_breakdown.get('htf', 0),
            "ADX": f"{coin.adx:.1f}",
            "ATR": f"{coin.atr:.4f}",
            "EMA200": coin.ema200_slope,
            "Last Update": last_update_str,
        })
    
    df = pd.DataFrame(data)
    
    # Display table
    st.dataframe(
        df,
        use_container_width=True,
        height=600,
        hide_index=True,
        column_config={
            "24h %": st.column_config.NumberColumn(
                "24h %",
                format="%.2f%%",
            )
        }
    )
    
    # Expandable detail section
    st.markdown("---")
    st.markdown("### Coin Detail")
    
    selected_symbol = st.selectbox(
        "Select coin for detailed analysis:",
        [c.symbol for c in filtered_coins],
        index=0
    )
    
    if selected_symbol:
        detail = get_coin_detail(selected_symbol)
        
        if detail:
            col1, col2 = st.columns(2)
            
            with col1:
                st.markdown("#### Latest Analysis")
                latest = detail['latest_analysis']
                st.write(f"**Regime:** {latest['regime']}")
                st.write(f"**Bias:** {latest['bias']}")
                st.write(f"**Signal:** {latest['signal']}")
                st.write(f"**Quality:** {latest['quality']:.0f}")
                st.write(f"**Timestamp:** {latest['timestamp'].strftime('%Y-%m-%d %H:%M:%S UTC')}")
                
                # Show Basis/Funding if available
                # (Assuming detail dict might need updates to carry this, or we fetch active position)
                from src.storage.repository import get_active_positions
                # This is a bit heavy for UI loop, but okay for single selection
                # Ideally pass this down from data_loader
                
            with col2:
                st.markdown("#### Recent Signals")
                for sig in detail['recent_signals'][:5]:
                    ts = sig['timestamp'].strftime('%H:%M')
                    st.write(f"• {ts}: {sig['signal']} (Q: {sig['quality']:.0f})")
        else:
            st.warning(f"No detail available for {selected_symbol}")

else:
    st.info("No coins match the current filters.")

# Auto-refresh
st.markdown("---")
st.caption("Dashboard auto-refreshes every 10 seconds")
