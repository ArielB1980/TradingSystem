"""
Debug script to check Kraken Futures API authentication.
Simply checks if we can access private endpoints.
"""
import os
import asyncio
import hashlib
import hmac
import base64
import time
import aiohttp
import ssl

def _require_env(name: str) -> str:
    v = os.getenv(name)
    if not v or not v.strip():
        raise SystemExit(f"Missing required env var: {name}")
    return v


def _ensure_allowed() -> None:
    if os.getenv("RUN_REAL_EXCHANGE_TESTS", "0").strip() not in ("1", "true", "TRUE", "yes", "YES"):
        raise SystemExit("Refusing to run real-exchange debug. Set RUN_REAL_EXCHANGE_TESTS=1 to enable.")

async def test_authentication():
    """Test authentication with a simple private endpoint."""
    _ensure_allowed()
    print("\n=== Testing Kraken Futures Authentication ===\n")
    
    # Test 1: Get account info (simpler endpoint)
    print("[1] Testing /account endpoint...")
    url = "https://futures.kraken.com/derivatives/api/v3/accounts"
    path = "/derivatives/api/v3/accounts"
    nonce = str(int(time.time() * 1000))
    
    # Method 1: SHA-256 then HMAC-SHA-512
    print(f"   Nonce: {nonce}")
    postdata = ""  # GET request
    message = postdata + nonce + path
    print(f"   Message: '{message}'")
    
    sha256_hash = hashlib.sha256(message.encode('utf-8')).digest()
    secret_decoded = base64.b64decode(_require_env("KRAKEN_FUTURES_API_SECRET"))
    signature = hmac.new(secret_decoded, sha256_hash, hashlib.sha512).digest()
    authent = base64.b64encode(signature).decode('utf-8')
    
    headers = {
        'APIKey': _require_env("KRAKEN_FUTURES_API_KEY"),
        'Authent': authent,
        'Nonce': nonce,
    }
    
    ssl_context = ssl.SSLContext()
    ssl_context.verify_mode = ssl.CERT_NONE
    connector = aiohttp.TCPConnector(ssl=ssl_context)
    
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.get(url, headers=headers) as response:
            status = response.status
            body = await response.text()
            
            print(f"   Status: {status}")
            print(f"   Response: {body[:200]}")
            
            if status == 200:
                print("   ✅ Authentication works!\n")
                return True
            else:
                print(f"   ❌ Authentication failed\n")
                
                # Try demo endpoint
                print("[2] Trying demo environment...")
                demo_url = "https://demo-futures.kraken.com/derivatives/api/v3/accounts"
                demo_path = "/derivatives/api/v3/accounts"
                nonce2 = str(int(time.time() * 1000))
                message2 = "" + nonce2 + demo_path
                sha256_hash2 = hashlib.sha256(message2.encode('utf-8')).digest()
                signature2 = hmac.new(secret_decoded, sha256_hash2, hashlib.sha512).digest()
                authent2 = base64.b64encode(signature2).decode('utf-8')
                
                headers2 = {
                    'APIKey': FUTURES_API_KEY,
                    'Authent': authent2,
                    'Nonce': nonce2,
                }
                
                async with session.get(demo_url, headers=headers2) as response2:
                    status2 = response2.status
                    body2 = await response2.text()
                    
                    print(f"   Status: {status2}")
                    print(f"   Response: {body2[:200]}")
                    
                    if status2 == 200:
                        print("   ✅ Demo environment works! Use demo-futures.kraken.com\n")
                        return "demo"
                    else:
                        print("   ❌ Demo also failed\n")
                        return False

if __name__ == "__main__":
    result = asyncio.run(test_authentication())
    if result:
        print(f"\n✅ Result: {result}")
    else:
        print("\n❌ Both production and demo authentication failed")
        print("   Possible issues:")
        print("   1. API keys don't have trading permissions")
        print("   2. API keys are for wrong environment (demo vs prod)")
        print("   3. Signature algorithm is incorrect")
