"""
Debug which credentials are actually being used for order placement.
"""
import os
import asyncio
from src.data.kraken_client import KrakenClient

def _require_env(name: str) -> str:
    v = os.getenv(name)
    if not v or not v.strip():
        raise SystemExit(f"Missing required env var: {name}")
    return v


def _ensure_allowed() -> None:
    if os.getenv("RUN_REAL_EXCHANGE_TESTS", "0").strip() not in ("1", "true", "TRUE", "yes", "YES"):
        raise SystemExit("Refusing to run real-exchange debug. Set RUN_REAL_EXCHANGE_TESTS=1 to enable.")

async def debug_credentials():
    """Check which credentials are being used."""
    _ensure_allowed()
    
    print("\n=== Debugging Credential Usage ===\n")
    
    client = KrakenClient(
        api_key=_require_env("KRAKEN_API_KEY"),
        api_secret=_require_env("KRAKEN_API_SECRET"),
        futures_api_key=_require_env("KRAKEN_FUTURES_API_KEY"),
        futures_api_secret=_require_env("KRAKEN_FUTURES_API_SECRET"),
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
    if headers['APIKey'] == _require_env("KRAKEN_FUTURES_API_KEY"):
        print("   ✅ CORRECT: Using FUTURES API key")
    elif headers['APIKey'] == _require_env("KRAKEN_API_KEY"):
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
