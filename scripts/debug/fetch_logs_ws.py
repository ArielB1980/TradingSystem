
import asyncio
import websockets
import sys
import ssl

# URL from the previous step
LOG_URL = "https://proxy-fra1-d694ff471381.ondigitalocean.app/?token=7it9TZfc_26UmHeJctpjgGMZd62wUmHx7Ju_LBCeVuFofLX-U2BaZ5UE0gCSKnDpuMAU32IMZfOJ1w8qzhanUV6QpkQ-ln9uySraMxVGT8u35MTAM212vqdb_byUekd1VZgpV290AeFW7kNiybEo1QRdmNsrinaqqxzV4Q-KUuMI53rriAyzgiHAMBHm4ABQQieXU4_4eAc0bQy-_7BgghegFYEr9w1oPAAMxrKG1awCDjPrOy-CvgKq9erop1cmxRUijoI0qRQk-g9iGJdlVMGfxwP8tTxyPjWmadkT91Y-Bo-GR1jlogUQyRCI2YH7PBFt0ZdFVzRwOH8Z1X60x3GOKRrN2O25P2DoNFIF4fKToBa0HVF183vDUTruVAqMU2vs7n-JSOPEOGjQ7-4Ih0qYB3CLvgV32JE4fSTUbS4P2mObU6Gb"

# Convert to WSS if necessary (websockets lib usually expects wss://)
if LOG_URL.startswith("https://"):
    LOG_URL = LOG_URL.replace("https://", "wss://")

async def fetch_logs():
    print(f"Connecting to {LOG_URL[:50]}...")
    
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE  # DO logs sometimes have cert issues or self-signed in proxy chains? usually valid, but safe for script.

    try:
        async with websockets.connect(LOG_URL) as websocket:
            print("Connected. Receiving logs...")
            try:
                while True:
                    # Wait for message with a timeout
                    message = await asyncio.wait_for(websocket.recv(), timeout=5.0)
                    print(message)
            except asyncio.TimeoutError:
                print("Timeout reached (5s silence). Stopping.")
            except Exception as e:
                print(f"Error reading: {e}")
    except Exception as e:
        print(f"Connection error: {e}")

if __name__ == "__main__":
    asyncio.run(fetch_logs())
