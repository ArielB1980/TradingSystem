"""
Debug which credentials are actually being used for order placement.
"""
import os
import asyncio
from src.data.kraken_client import KrakenClient

# Set credentials
SPOT_KEY = 'sIHZanYflTqKAv9dsP0L5Xu+tjR2jFo5xI582NEQ2wAmqIoDIjm70MEq'
SPOT_SECRET = 'RIpGuxXd+bfgJPeajbeKrh4FWxxXqjIsmTo3Qvfr5/B9eNJ825xL7I/ddso6rjO2UGIyaHM/ctVtJmadaDsD8A=='
FUTURES_KEY = 'uG8IoCO8CLLIIghlZVIMWoM5nbBKscc3wlJDZEMIKW4A+Cmf+fuSB+Oy'
FUTURES_SECRET = 'MoBA5A7X1269Jv81zr+ur551GZe/nA7d5PasKu8L4M0dloy+hogmKKKePAWkBqfvxgpMEfoHpYxYFVUao010yyMb'

async def debug_credentials():
    """Check which credentials are being used."""
    
    print("\n=== Debugging Credential Usage ===\n")
    
    client = KrakenClient(
        api_key=SPOT_KEY,
        api_secret=SPOT_SECRET,
        futures_api_key=FUTURES_KEY,
        futures_api_secret=FUTURES_SECRET,
    )
    
    # Check what's stored
    print("[1] Credentials stored in client:")
    print(f"   Spot API Key: {client.api_key[:20]}...")
    print(f"   Futures API Key: {client.futures_api_key[:20]}...")
    
    # Now trace what happens in _get_futures_auth_headers
    print("\n[2] Checking _get_futures_auth_headers method...")
    url = "https://futures.kraken.com/derivatives/api/v3/sendorder"
    postdata = "symbol=PF_XBTUSD&side=buy&orderType=lmt&size=1&limitPrice=50000"
    
    headers = await client._get_futures_auth_headers(url, "POST", postdata)
    
    print(f"   URL: {url}")
    print(f"   APIKey header: {headers['APIKey'][:20]}...")
    print(f"   Authent header length: {len(headers['Authent'])}")
    print(f"   Nonce: {headers['Nonce']}")
    
    # Compare
    print("\n[3] Verification:")
    if headers['APIKey'] == FUTURES_KEY:
        print("   ✅ CORRECT: Using FUTURES API key")
    elif headers['APIKey'] == SPOT_KEY:
        print("   ❌ WRONG: Using SPOT API key!")
    else:
        print(f"   ⚠️  UNKNOWN: Using different key: {headers['APIKey'][:20]}...")
    
    # Check the actual endpoint
    print("\n[4] Endpoint check:")
    print(f"   Endpoint: {url}")
    if "futures.kraken.com" in url:
        print("   ✅ CORRECT: Using futures.kraken.com")
    else:
        print("   ❌ WRONG: Not using futures endpoint!")

if __name__ == "__main__":
    asyncio.run(debug_credentials())
