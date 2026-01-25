"""Quick test to see available Kraken Futures symbols."""
import aiohttp
import asyncio
import ssl
import json

async def get_symbols():
    url = "https://futures.kraken.com/derivatives/api/v3/tickers"
    ssl_context = ssl.SSLContext()
    ssl_context.verify_mode = ssl.CERT_NONE
    connector = aiohttp.TCPConnector(ssl=ssl_context)
    
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.get(url) as response:
            data = await response.json()
            print("Available Kraken Futures symbols:")
            for ticker in data.get('tickers', [])[:20]:  # First 20
                symbol = ticker.get('symbol')
                if 'BTC' in symbol or 'XBT' in symbol:
                    print(f"  {symbol} - mark: {ticker.get('markPrice')}")

asyncio.run(get_symbols())
