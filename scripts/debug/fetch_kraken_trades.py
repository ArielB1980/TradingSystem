
import ccxt
import os
from datetime import datetime, timedelta

# API credentials from .env.local
config = {
    'apiKey': '2k1daXUJari2fsDGsQ21rNgF1xeL3obeT+ojmNcpuS44SPMYXaKV6KMx',
    'secret': '4h77HOI0onjBh4zgklakpVwLrbCg0GZNrCeOBOUQPMOIVcciOFEJ9yljOy2Fm746UznwVCpSqPbKsMqyxNOUmBoM',
    'enableRateLimit': True,
}

def fetch_trades():
    print("Connecting to Kraken Futures...")
    exchange = ccxt.krakenfutures(config)
    
    # Define "today" as from midnight UTC
    now = datetime.utcnow()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    since_timestamp = int(midnight.timestamp() * 1000)
    
    print(f"Fetching trades since {midnight} UTC...")
    
    try:
        # fetchMyTrades might not be supported on all exchanges/markets or might need params
        # For Kraken Futures, it usually works.
        trades = exchange.fetch_my_trades(since=since_timestamp)
        
        print(f"Found {len(trades)} trades today.")
        print("-" * 50)
        
        for trade in trades:
            # Format trade details
            ts = datetime.fromtimestamp(trade['timestamp'] / 1000)
            symbol = trade['symbol']
            side = trade['side']
            amount = trade['amount']
            price = trade['price']
            cost = trade['cost']
            fee = trade['fee']['cost'] if trade.get('fee') else 0
            
            print(f"[{ts}] {symbol} {side.upper()} {amount} @ {price} (Cost: {cost}, Fee: {fee})")
            
    except Exception as e:
        print(f"Error fetching trades: {e}")
        # Sometimes permissions or API differences cause issues.
        # Check if we need to set 'sandbox': False explicitly (default is False)

if __name__ == "__main__":
    fetch_trades()
