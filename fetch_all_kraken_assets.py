"""
Fetch all available trading pairs from Kraken Spot and Futures APIs.
"""
import asyncio
import aiohttp
import ssl
import certifi
from typing import Set, Dict, List

async def fetch_all_kraken_assets():
    """Fetch all available assets from Kraken Spot and Futures."""
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    
    # Fetch Spot pairs
    print("Fetching Kraken Spot assets...")
    spot_pairs = set()
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_context)) as session:
        async with session.get("https://api.kraken.com/0/public/AssetPairs") as resp:
            data = await resp.json()
            if data.get('error'):
                print(f"Spot API Error: {data['error']}")
            else:
                pairs = data.get('result', {})
                for pair_name, pair_data in pairs.items():
                    # Get the standardized pair name
                    wsname = pair_data.get('wsname')
                    if wsname and '/USD' in wsname:
                        spot_pairs.add(wsname)
    
    print(f"Found {len(spot_pairs)} USD spot pairs")
    
    # Fetch Futures instruments
    print("\nFetching Kraken Futures assets...")
    futures_symbols = set()
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_context)) as session:
        async with session.get("https://futures.kraken.com/derivatives/api/v3/instruments") as resp:
            data = await resp.json()
            instruments = data.get('instruments', [])
            for instr in instruments:
                symbol = instr.get('symbol', '')
                # Filter for perpetual USD contracts
                if 'PF_' in symbol and 'USD' in symbol and symbol.endswith('USD'):
                    # Convert PF_BTCUSD to BTC/USD format
                    base = symbol.replace('PF_', '').replace('USD', '')
                    futures_symbols.add(f"{base}/USD")
    
    print(f"Found {len(futures_symbols)} USD futures pairs")
    
    # Find intersection (coins available on both Spot and Futures)
    both = spot_pairs.intersection(futures_symbols)
    print(f"\n{len(both)} coins available on BOTH Spot and Futures")
    
    # Sort and display
    sorted_both = sorted(list(both))
    
    print("\n=== Coins Available on Both Spot and Futures ===")
    for i, coin in enumerate(sorted_both, 1):
        print(f"{i:3d}. {coin}")
    
    # Also show Spot-only and Futures-only
    spot_only = spot_pairs - futures_symbols
    futures_only = futures_symbols - spot_pairs
    
    print(f"\n=== Summary ===")
    print(f"Both Spot & Futures: {len(both)}")
    print(f"Spot only: {len(spot_only)}")
    print(f"Futures only: {len(futures_only)}")
    
    return {
        'both': sorted_both,
        'spot_only': sorted(list(spot_only)),
        'futures_only': sorted(list(futures_only))
    }

if __name__ == "__main__":
    result = asyncio.run(fetch_all_kraken_assets())
    
    # Save to file for reference
    with open('kraken_assets_full.txt', 'w') as f:
        f.write("=== BOTH SPOT & FUTURES ===\n")
        for coin in result['both']:
            f.write(f"{coin}\n")
        f.write(f"\n=== SPOT ONLY ({len(result['spot_only'])}) ===\n")
        for coin in result['spot_only']:
            f.write(f"{coin}\n")
        f.write(f"\n=== FUTURES ONLY ({len(result['futures_only'])}) ===\n")
        for coin in result['futures_only']:
            f.write(f"{coin}\n")
    
    print("\nResults saved to kraken_assets_full.txt")
