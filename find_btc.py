import aiohttp
import asyncio
import ssl

async def find_btc():
    url = "https://futures.kraken.com/derivatives/api/v3/tickers"
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS)
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    connector = aiohttp.TCPConnector(ssl=ssl_context)
    
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.get(url) as resp:
            data =await resp.json()
            print("BTC/XBT Perpetuals:")
            for ticker in data['tickers']:
                symbol = ticker.get('symbol', '')
                if 'BTC' in symbol or 'XBT' in symbol:
                    print(f"  {symbol}: mark=${ticker.get('markPrice')}")

asyncio.run(find_btc())
