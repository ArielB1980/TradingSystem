# Manual File Update Instructions

Since the production server doesn't have git, update the file manually:

## File to Update
`/home/trading/TradingSystem/src/execution/futures_adapter.py`

## Lines to Replace (155-189)

**Find this code (around line 155):**
```python
        # 1. Fetch instrument metadata to get contract size
        instruments = await self.kraken_client.get_futures_instruments()
        instr = next((i for i in instruments if i['symbol'].upper() == symbol.upper()), None)
        
        if not instr:
            raise ValueError(f"Instrument specs for {symbol} not found")
```

**Replace with this:**
```python
        # 1. Fetch instrument metadata to get contract size
        instruments = await self.kraken_client.get_futures_instruments()
        
        # Try to find instrument by symbol - instruments API may return different formats
        # Try: PF_AUDUSD, AUDUSD, AUD/USD:USD, etc.
        instr = None
        symbol_upper = symbol.upper()
        
        # First try exact match
        instr = next((i for i in instruments if i.get('symbol', '').upper() == symbol_upper), None)
        
        if not instr:
            # Try without PF_ prefix
            symbol_no_prefix = symbol_upper.replace('PF_', '')
            instr = next((i for i in instruments if i.get('symbol', '').upper() == symbol_no_prefix), None)
        
        if not instr:
            # Try with /USD:USD format (CCXT unified)
            base = symbol_upper.replace('PF_', '').replace('USD', '')
            if base:
                unified_format = f"{base}/USD:USD"
                instr = next((i for i in instruments if i.get('symbol', '').upper() == unified_format), None)
        
        if not instr:
            # Log available symbols for debugging (first 20 that contain similar base)
            base_part = symbol_upper.replace('PF_', '').replace('USD', '').replace('/', '')[:3]
            similar = [i.get('symbol', '') for i in instruments if base_part in i.get('symbol', '').upper()][:20]
            logger.error(
                "Instrument specs not found",
                requested_symbol=symbol,
                similar_symbols=similar,
                total_instruments=len(instruments),
            )
            raise ValueError(f"Instrument specs for {symbol} not found")
```

## Steps to Apply

1. **Backup the file:**
```bash
sudo cp /home/trading/TradingSystem/src/execution/futures_adapter.py /home/trading/TradingSystem/src/execution/futures_adapter.py.backup
```

2. **Edit the file:**
```bash
sudo nano /home/trading/TradingSystem/src/execution/futures_adapter.py
```

3. **Find line 155** (search for "Fetch instrument metadata")

4. **Replace the code** as shown above

5. **Save and exit** (Ctrl+X, then Y, then Enter)

6. **Restart the service:**
```bash
sudo systemctl restart trading-system.service
```

7. **Verify:**
```bash
sudo systemctl status trading-system.service
```
