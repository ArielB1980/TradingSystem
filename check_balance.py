import os
import asyncio
import json
from src.data.kraken_client import KrakenClient
from src.monitoring.logger import setup_logging

# Set credentials (ensure these match your environment or represent the user's setup)
os.environ['KRAKEN_API_KEY'] = 'sIHZanYflTqKAv9dsP0L5Xu+tjR2jFo5xI582NEQ2wAmqIoDIjm70MEq'
os.environ['KRAKEN_API_SECRET'] = 'RIpGuxXd+bfgJPeajbeKrh4FWxxXqjIsmTo3Qvfr5/B9eNJ825xL7I/ddso6rjO2UGIyaHM/ctVtJmadaDsD8A=='
os.environ['KRAKEN_FUTURES_API_KEY'] = 'uG8IoCO8CLLIIghlZVIMWoM5nbBKscc3wlJDZEMIKW4A+Cmf+fuSB+Oy'
os.environ['KRAKEN_FUTURES_API_SECRET'] = 'MoBA5A7X1269Jv81zr+ur551GZe/nA7d5PasKu8L4M0dloy+hogmKKKePAWkBqfvxgpMEfoHpYxYFVUao010yyMb'

setup_logging("INFO", "text")

async def check_balance():
    print("\n" + "="*60)
    print("KRAKEN FUTURES BALANCE CHECK")
    print("="*60)
    
    client = KrakenClient(
        api_key=os.environ['KRAKEN_API_KEY'],
        api_secret=os.environ['KRAKEN_API_SECRET'],
        futures_api_key=os.environ['KRAKEN_FUTURES_API_KEY'],
        futures_api_secret=os.environ['KRAKEN_FUTURES_API_SECRET'],
    )
    
    try:
        balance = await client.get_futures_balance()
        
        print("\n✅ Balance fetched successfully!")
        
        # Display all non-zero balances
        print("\n--- Asset Balances ---")
        total_balance_detected = False
        
        # Standard CCXT 'total' dict
        for currency, amount in balance.get('total', {}).items():
            if amount > 0:
                total_balance_detected = True
                print(f"{currency}: {amount}")
        
        # Check raw info for comprehensive portfolio data
        # Kraken Futures API usually provides 'marginAccount' or similar in raw info
        info = balance.get('info', {})
        if info:
            print("\n--- Wallet Summary (Raw) ---")
            # Try to find portfolio value or equity
            # Common fields: 'portfolioValue', 'collateral', 'marginEquity'
            keys_to_check = ['portfolioValue', 'marginEquity', 'totalWalletBalance', 'walletBalance']
            
            found_val = False
            for k in keys_to_check:
                if k in info:
                    print(f"{k}: {info[k]}")
                    found_val = True
            
                # If structure is different, print keys to help debug
                print(f"Available keys in info: {list(info.keys())}")
                
                # Safely print json dump of accounts
                print(json.dumps(info['accounts'], indent=2))
        
        # --- Check Spot Balance ---
        print("\n" + "="*60)
        print("KRAKEN SPOT BALANCE CHECK")
        print("="*60)
        
        spot_balance = await client.get_spot_balance()
        print("\n--- Spot Assets ---")
        
        # Determine total approx value by fetching simple prices or just listing assets for user
        # For now, just list assets to solve the mystery
        for currency, amount in spot_balance.get('total', {}).items():
            if amount > 0:
                print(f"{currency}: {amount}")
                
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
    finally:
        await client.close()

if __name__ == "__main__":
    asyncio.run(check_balance())
