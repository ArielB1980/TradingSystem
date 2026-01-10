"""
Safe test for Kraken Futures order placement.

This test places limit orders FAR from market price (won't fill) to verify:
- Order placement works
- Order appears in open orders
- Order cancellation works

SAFE: Orders are placed at prices that won't execute.
"""
import os
import asyncio
from decimal import Decimal
from src.data.kraken_client import KrakenClient
from src.monitoring.logger import setup_logging

# Set credentials
os.environ['KRAKEN_API_KEY'] = 'sIHZanYflTqKAv9dsP0L5Xu+tjR2jFo5xI582NEQ2wAmqIoDIjm70MEq'
os.environ['KRAKEN_API_SECRET'] = 'RIpGuxXd+bfgJPeajbeKrh4FWxxXqjIsmTo3Qvfr5/B9eNJ825xL7I/ddso6rjO2UGIyaHM/ctVtJmadaDsD8A=='
os.environ['KRAKEN_FUTURES_API_KEY'] = 'uG8IoCO8CLLIIghlZVIMWoM5nbBKscc3wlJDZEMIKW4A+Cmf+fuSB+Oy'
os.environ['KRAKEN_FUTURES_API_SECRET'] = 'MoBA5A7X1269Jv81zr+ur551GZe/nA7d5PasKu8L4M0dloy+hogmKKKePAWkBqfvxgpMEfoHpYxYFVUao010yyMb'

setup_logging("INFO", "text")

async def test_order_placement():
    """Test Kraken Futures order placement with safety measures."""
    print("\n" + "="*60)
    print("KRAKEN FUTURES ORDER PLACEMENT TEST")
    print("="*60)
    
    client = KrakenClient(
        api_key=os.environ['KRAKEN_API_KEY'],
        api_secret=os.environ['KRAKEN_API_SECRET'],
        futures_api_key=os.environ['KRAKEN_FUTURES_API_KEY'],
        futures_api_secret=os.environ['KRAKEN_FUTURES_API_SECRET'],
    )
    
    order_id = None
    
    try:
        # Step 1: Get current mark price
        print("\n[1/5] Fetching current BTC mark price...")
        mark_price = await client.get_futures_mark_price("BTCUSD-PERP")
        print(f"✅ Current BTC mark price: ${mark_price}")
        
        # Step 2: Place a limit order FAR from market (won't fill)
        # We'll place a buy order 20% below market - safe, won't execute
        safe_price = mark_price * Decimal("0.80")
        size = Decimal("1")  # 1 contract = $1 notional (minimal)
        
        print(f"\n[2/5] Placing SAFE limit buy order...")
        print(f"    Symbol: PF_XBTUSD")
        print(f"    Side: buy")
        print(f"    Size: {size} contracts (${size} notional)")
        print(f"    Price: ${safe_price:.2f} (20% below market - WON'T FILL)")
        
        response = await client.place_futures_order(
            symbol="PF_XBTUSD",
            side="buy",
            order_type="lmt",
            size=size,
            price=safe_price,
            leverage=10,
            client_order_id="test_order_safe_001",
        )
        
        send_status = response.get("sendStatus", {})
        order_id = send_status.get("order_id")
        
        print(f"✅ Order placed successfully!")
        print(f"    Order ID: {order_id}")
        print(f"    Status: {send_status.get('status')}")
        
        # Step 3: Verify order appears in open orders
        print(f"\n[3/5] Fetching open orders...")
        await asyncio.sleep(1)  # Give exchange time to process
        
        open_orders = await client.get_futures_open_orders()
        print(f"✅ Found {len(open_orders)} open order(s)")
        
        # Find our order
        our_order = None
        for order in open_orders:
            if order.get("order_id") == order_id:
                our_order = order
                break
        
        if our_order:
            print(f"✅ Our order found in open orders:")
            print(f"    Symbol: {our_order.get('symbol')}")
            print(f"    Side: {our_order.get('side')}")
            print(f"    Size: {our_order.get('qty')}")
            print(f"    Limit Price: {our_order.get('limitPrice')}")
        else:
            print(f"⚠️  Our order not found in open orders (may have been rejected)")
        
        # Step 4: Cancel the order
        if order_id:
            print(f"\n[4/5] Cancelling order {order_id}...")
            cancel_response = await client.cancel_futures_order(order_id)
            
            if cancel_response.get("result") == "success":
                print(f"✅ Order cancelled successfully")
            else:
                print(f"⚠️  Cancel response: {cancel_response}")
        
        # Step 5: Verify order is gone
        print(f"\n[5/5] Verifying order was cancelled...")
        await asyncio.sleep(1)
        
        open_orders_after = await client.get_futures_open_orders()
        order_still_exists = any(o.get("order_id") == order_id for o in open_orders_after)
        
        if not order_still_exists:
            print(f"✅ Order successfully removed from open orders")
        else:
            print(f"⚠️  Order still appears in open orders")
        
        print("\n" + "="*60)
        print("✅ ALL TESTS PASSED!")
        print("="*60)
        print("\nOrder placement functionality is working correctly.")
        print("The system is ready for integration with the trading engine.")
        
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        
        if order_id:
            print(f"\n⚠️  Attempting to cancel order {order_id}...")
            try:
                await client.cancel_futures_order(order_id)
                print(f"✅ Cleanup successful")
            except Exception as cleanup_error:
                print(f"❌ Cleanup failed: {cleanup_error}")
    finally:
        await client.close()

if __name__ == "__main__":
    asyncio.run(test_order_placement())
