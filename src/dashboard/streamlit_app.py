"""
Trading System Dashboard - Single Page Coin Monitor

Displays comprehensive real-time analysis for all tracked coins.
"""
import sys
from pathlib import Path

# Ensure project root is in path for imports
project_root = Path(__file__).parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

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
    from src.utils.kill_switch import get_kill_switch
    ks = get_kill_switch()
    ks_status = ks.get_status()
    
    if ks_status.get('active'):
        st.error(f"🚨 KILL SWITCH ACTIVE")
        st.caption(f"Reason: {ks_status.get('reason')}")
    else:
        st.success("✅ System Operational")
        
    st.subheader("🔔 Recent Events")
    try:
        from src.dashboard.utils import get_event_feed
        alerts = get_event_feed(limit=15)
        for a in alerts[:10]:
            st.caption(f"{a.get('timestamp', '')[:19]} [{a.get('type', '')}] {a.get('symbol', '')}")
            st.text(a.get('message', ''))
    except Exception as e:
        st.caption(f"Could not load events: {e}")


# Header
st.markdown('<div class="main-header">🎯 Trading System Monitor</div>', unsafe_allow_html=True)

# Load data
@st.cache_data(ttl=10)
def load_dashboard_data():
    return load_all_coins()

@st.cache_data(ttl=10)
def load_positions_data():
    from src.dashboard.positions_loader import load_active_positions
    return load_active_positions()

coins = load_dashboard_data()
positions = load_positions_data()

# Status bar with freshness info
now = datetime.now(timezone.utc)
active_count = sum(1 for c in coins if c.status == "active")
stale_count = sum(1 for c in coins if c.status == "stale")
dead_count = sum(1 for c in coins if c.status == "dead")

# Calculate average freshness (exclude Dead/Never Updated coins)
if coins:
    # Filter for coins updated within last 24 hours to give meaningful average
    valid_coins = [c for c in coins if c.last_update and (now - c.last_update).total_seconds() < 86400]
    
    if valid_coins:
        avg_age_seconds = sum((now - c.last_update).total_seconds() for c in valid_coins) / len(valid_coins)
        if avg_age_seconds < 300:
            freshness_emoji = "🟢"
        elif avg_age_seconds < 1800:
            freshness_emoji = "🟡"
        else:
            freshness_emoji = "🔴"
    else:
        freshness_emoji = "🔴"
        avg_age_seconds = 0
else:
    freshness_emoji = "⚪"
    avg_age_seconds = 0

st.markdown(
    f'<div class="status-bar">⚡ Live: {len(coins)} coins | '
    f'🟢 Active: {active_count} | 🟡 Stale: {stale_count} | 🔴 Dead: {dead_count} | '
    f'💰 Positions: {len(positions)} | '
    f'{freshness_emoji} Avg freshness: {int(avg_age_seconds/60)}m | '
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
# Open Positions Section
st.markdown("---")
st.markdown("### 💰 Open Positions")

if positions:
    positions_data = []
    total_pnl = 0.0
    total_margin = 0.0
    
    for pos in positions:
        # Format stop loss price
        sl_price = pos.get('initial_stop_price')
        if sl_price:
            sl_pct = ((sl_price - pos['entry_price']) / pos['entry_price']) * 100
            if pos['side'] == 'SHORT':
                sl_pct = -sl_pct
            sl_str = f"${sl_price:.4f} ({sl_pct:+.1f}%)"
        else:
            sl_str = "⚠️ None"
        
        # Format TP targets
        tp_targets = []
        if pos.get('tp1_price'):
            tp1_price = pos['tp1_price']
            tp1_pct = ((tp1_price - pos['entry_price']) / pos['entry_price']) * 100
            if pos['side'] == 'SHORT':
                tp1_pct = -tp1_pct
            tp_targets.append(f"TP1: ${tp1_price:.4f} ({tp1_pct:+.1f}%)")
        if pos.get('tp2_price'):
            tp2_price = pos['tp2_price']
            tp2_pct = ((tp2_price - pos['entry_price']) / pos['entry_price']) * 100
            if pos['side'] == 'SHORT':
                tp2_pct = -tp2_pct
            tp_targets.append(f"TP2: ${tp2_price:.4f} ({tp2_pct:+.1f}%)")
        if pos.get('final_target_price'):
            final_price = pos['final_target_price']
            final_pct = ((final_price - pos['entry_price']) / pos['entry_price']) * 100
            if pos['side'] == 'SHORT':
                final_pct = -final_pct
            tp_targets.append(f"Final: ${final_price:.4f} ({final_pct:+.1f}%)")
        tp_str = " | ".join(tp_targets) if tp_targets else "⚠️ None"
        
        # Format holding time
        hours = pos['holding_hours']
        if hours < 1:
            holding_str = f"{int(hours * 60)}m"
        elif hours < 24:
            holding_str = f"{int(hours)}h {int((hours % 1) * 60)}m"
        else:
            days = int(hours / 24)
            remaining_hours = int(hours % 24)
            holding_str = f"{days}d {remaining_hours}h"
        
        # Format opening time
        opened_at = pos['opened_at']
        if isinstance(opened_at, str):
            from datetime import datetime
            opened_at = datetime.fromisoformat(opened_at.replace('Z', '+00:00'))
        opened_str = opened_at.strftime('%Y-%m-%d %H:%M UTC')
        
        # Format liquidation price
        liq_str = f"${pos['liquidation_price']:.4f}" if pos.get('liquidation_price') else "N/A"
        
        positions_data.append({
            "Symbol": pos['symbol'],
            "Side": "🟢 LONG" if pos['side'] == 'LONG' else "🔴 SHORT",
            "Size": f"${pos['size_notional']:.2f}",
            "Leverage": f"{pos['leverage']:.1f}x",
            "Entry": f"${pos['entry_price']:.4f}",
            "Current": f"${pos['current_price']:.4f}",
            "Change %": f"{pos['change_pct']:+.2f}%" if pos['change_pct'] != 0 else "0.00%",
            "PnL": f"${pos['unrealized_pnl']:.2f}",
            "Stop Loss": sl_str,
            "TP Targets": tp_str,
            "Liquidation": liq_str,
            "Opened": opened_str,
            "Holding": holding_str,
            "Margin": f"${pos['margin_used']:.2f}",
        })
        
        total_pnl += pos['unrealized_pnl']
        total_margin += pos['margin_used']
    
    # Summary metrics
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Total Positions", len(positions))
    with col2:
        st.metric("Total PnL", f"${total_pnl:.2f}", delta=f"{total_pnl:.2f}")
    with col3:
        st.metric("Total Margin", f"${total_margin:.2f}")
    with col4:
        avg_leverage = sum(p['leverage'] for p in positions) / len(positions) if positions else 0
        st.metric("Avg Leverage", f"{avg_leverage:.1f}x")
    
    st.markdown("")  # Spacing
    
    # Positions table
    positions_df = pd.DataFrame(positions_data)
    st.dataframe(
        positions_df,
        use_container_width=True,
        hide_index=True,
        height=min(400, 50 + len(positions) * 35),
    )
else:
    st.info("No open positions")

st.markdown("---")


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
        
        # Format price - handle zero/invalid prices
        if coin.price > 0:
            price_str = f"${coin.price:.4f}"
        else:
            price_str = "N/A"
        
        # Format score breakdown values (handle missing keys)
        smc_score = coin.score_breakdown.get('smc', 0) if coin.score_breakdown else 0
        fib_score = coin.score_breakdown.get('fib', 0) if coin.score_breakdown else 0
        htf_score = coin.score_breakdown.get('htf', 0) if coin.score_breakdown else 0
        
        # Format data depth (candle count)
        candle_count = coin.candle_count if hasattr(coin, 'candle_count') else 0
        if candle_count >= 200:
            depth_str = f"✅ {candle_count}"
        elif candle_count >= 50:
            depth_str = f"🟡 {candle_count}"
        else:
            depth_str = f"🔴 {candle_count}"
        
        data.append({
            "Status": coin.status_emoji,
            "Symbol": coin.symbol,
            "Price": price_str,
            "24h %": f"{change:+.2f}%" if coin.price > 0 else "N/A",
            "Signal": f"{coin.signal_emoji} {coin.signal}",
            "Regime": coin.regime,
            "Bias": coin.bias,
            "Quality": f"{coin.quality:.0f}",
            "SMC": smc_score,
            "Fib": fib_score,
            "HTF": htf_score,
            "ADX": f"{coin.adx:.1f}" if coin.adx > 0 else "0.0",
            "ATR": f"{coin.atr:.4f}" if coin.atr > 0 else "0.0000",
            "EMA200": coin.ema200_slope,
            "Data Depth": depth_str,
            "Last Review": last_update_str,
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
        try:
            detail = get_coin_detail(selected_symbol)
            
            if detail:
                col1, col2 = st.columns(2)
                
                with col1:
                    st.markdown("#### Latest Analysis")
                    latest = detail.get('latest_analysis', {})
                    if latest:
                        st.write(f"**Regime:** {latest.get('regime', 'N/A')}")
                        st.write(f"**Bias:** {latest.get('bias', 'N/A')}")
                        st.write(f"**Signal:** {latest.get('signal', 'N/A')}")
                        # Safe retrieval: get() returns None if key exists but is None, so format crashes
                        quality = latest.get('quality')
                        if quality is None: quality = 0.0
                        st.write(f"**Quality:** {quality:.0f}")
                        timestamp = latest.get('timestamp')
                        if timestamp:
                            if isinstance(timestamp, str):
                                from datetime import datetime
                                timestamp = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                            st.write(f"**Timestamp:** {timestamp.strftime('%Y-%m-%d %H:%M:%S UTC')}")
                    else:
                        st.write("No analysis data available")
                    
                    # Show Basis/Funding if available
                    # (Assuming detail dict might need updates to carry this, or we fetch active position)
                    from src.storage.repository import get_active_positions
                    # This is a bit heavy for UI loop, but okay for single selection
                    # Ideally pass this down from data_loader
                    
                with col2:
                    st.markdown("#### Recent Signals")
                    recent_signals = detail.get('recent_signals', [])
                    if recent_signals:
                        for sig in recent_signals[:5]:
                            ts = sig.get('timestamp')
                            if ts:
                                if isinstance(ts, str):
                                    from datetime import datetime
                                    ts = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                                ts_str = ts.strftime('%H:%M')
                            else:
                                ts_str = "N/A"
                            signal = sig.get('signal', 'N/A')
                            quality = sig.get('quality')
                            if quality is None: quality = 0.0
                            st.write(f"• {ts_str}: {signal} (Q: {quality:.0f})")
                    else:
                        st.write("No recent signals")
            else:
                st.warning(f"No detail available for {selected_symbol}")
        except Exception as e:
            st.error(f"Error loading detail for {selected_symbol}: {str(e)}")
            import traceback
            st.code(traceback.format_exc())

else:
    st.info("No coins match the current filters.")

# Auto-refresh
st.markdown("---")
st.caption("Dashboard auto-refreshes every 10 seconds")

# Signals Log Section
st.markdown("---")
st.markdown("### 📊 Recent Signals Log")

@st.cache_data(ttl=10)
def load_recent_signals(limit: int = 50):
    """Load recent trading signals from database."""
    try:
        from src.storage.repository import get_recent_events
        from datetime import datetime, timezone, timedelta
        
        # Get recent DECISION_TRACE events
        events = get_recent_events(limit=limit * 3, event_type="DECISION_TRACE")
        
        # Filter for actual signals (not "no_signal")
        signals = []
        for event in events:
            details = event.get('details', {})
            signal_type = details.get('signal', '')
            
            if signal_type and signal_type != 'no_signal':
                signals.append({
                    'timestamp': event.get('timestamp'),
                    'symbol': event.get('symbol'),
                    'signal': signal_type,
                    'quality': details.get('setup_quality', 0),
                    'regime': details.get('regime', 'unknown'),
                    'bias': details.get('bias', 'neutral'),
                })
        
        # Limit and sort by timestamp (newest first)
        signals = sorted(signals, key=lambda x: x['timestamp'], reverse=True)[:limit]
        return signals
    except Exception as e:
        logger.error("Failed to load recent signals", error=str(e))
        return []

signals = load_recent_signals(limit=50)

if signals:
    # Convert to DataFrame for display
    signals_data = []
    for sig in signals:
        # Parse timestamp
        try:
            ts_str = sig['timestamp']
            if isinstance(ts_str, str):
                ts_dt = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
            else:
                ts_dt = ts_str
            
            # Format timestamp
            now = datetime.now(timezone.utc)
            minutes_ago = (now - ts_dt).total_seconds() / 60
            
            if minutes_ago < 60:
                time_str = f"{int(minutes_ago)}m ago"
            elif minutes_ago < 1440:
                time_str = f"{int(minutes_ago/60)}h ago"
            else:
                time_str = f"{int(minutes_ago/1440)}d ago"
            
            # Format signal type with emoji
            signal_emoji = "🟢" if sig['signal'] == 'long' else "🔴"
            signal_display = f"{signal_emoji} {sig['signal'].upper()}"
            
            signals_data.append({
                "Time": time_str,
                "Timestamp": ts_dt.strftime('%Y-%m-%d %H:%M:%S UTC'),
                "Symbol": sig['symbol'],
                "Signal": signal_display,
                "Quality": f"{sig['quality']:.1f}",
                "Regime": sig['regime'],
                "Bias": sig['bias'],
            })
        except Exception as e:
            logger.debug(f"Error formatting signal: {e}")
            continue
    
    if signals_data:
        signals_df = pd.DataFrame(signals_data)
        
        # Display summary
        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("Total Signals", len(signals_data))
        with col2:
            long_count = sum(1 for s in signals_data if "LONG" in s['Signal'])
            st.metric("LONG Signals", long_count)
        with col3:
            short_count = sum(1 for s in signals_data if "SHORT" in s['Signal'])
            st.metric("SHORT Signals", short_count)
        
        st.markdown("")  # Spacing
        
        # Display table
        st.dataframe(
            signals_df,
            use_container_width=True,
            hide_index=True,
            height=min(400, 50 + len(signals_data) * 35),
        )
    else:
        st.info("No signals to display")
else:
    st.info("No recent signals found")
