# Quick Fix Commands for Production Server

Run these commands on your production server:

## Step 1: Backup and Check Current Code

```bash
sudo cp /home/trading/TradingSystem/src/execution/futures_adapter.py /home/trading/TradingSystem/src/execution/futures_adapter.py.backup
sudo head -n 165 /home/trading/TradingSystem/src/execution/futures_adapter.py | tail -n 10
```

## Step 2: Apply Fix Using Python Script

Create a fix script on the server:

```bash
sudo cat > /tmp/apply_fix.py << 'EOF'
import re

file_path = '/home/trading/TradingSystem/src/execution/futures_adapter.py'

with open(file_path, 'r') as f:
    content = f.read()

# Find and replace the old code
old_pattern = r'(\s+# 1\. Fetch instrument metadata to get contract size\s+instruments = await self\.kraken_client\.get_futures_instruments\(\)\s+)instr = next\(\(i for i in instruments if i\[\'symbol\'\]\.upper\(\) == symbol\.upper\(\)\), None\)\s+if not instr:\s+raise ValueError\(f"Instrument specs for \{symbol\} not found"\)'

new_code = '''        # 1. Fetch instrument metadata to get contract size
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
'''

# Simple replacement - find the section and replace
lines = content.split('\n')
new_lines = []
i = 0
while i < len(lines):
    if '# 1. Fetch instrument metadata' in lines[i] and i + 3 < len(lines):
        # Found the section, replace it
        new_lines.append('        # 1. Fetch instrument metadata to get contract size')
        new_lines.append('        instruments = await self.kraken_client.get_futures_instruments()')
        new_lines.append('')
        new_lines.append('        # Try to find instrument by symbol - instruments API may return different formats')
        new_lines.append('        # Try: PF_AUDUSD, AUDUSD, AUD/USD:USD, etc.')
        new_lines.append('        instr = None')
        new_lines.append('        symbol_upper = symbol.upper()')
        new_lines.append('')
        new_lines.append('        # First try exact match')
        new_lines.append('        instr = next((i for i in instruments if i.get(\'symbol\', \'\').upper() == symbol_upper), None)')
        new_lines.append('')
        new_lines.append('        if not instr:')
        new_lines.append('            # Try without PF_ prefix')
        new_lines.append('            symbol_no_prefix = symbol_upper.replace(\'PF_\', \'\')')
        new_lines.append('            instr = next((i for i in instruments if i.get(\'symbol\', \'\').upper() == symbol_no_prefix), None)')
        new_lines.append('')
        new_lines.append('        if not instr:')
        new_lines.append('            # Try with /USD:USD format (CCXT unified)')
        new_lines.append('            base = symbol_upper.replace(\'PF_\', \'\').replace(\'USD\', \'\')')
        new_lines.append('            if base:')
        new_lines.append('                unified_format = f"{base}/USD:USD"')
        new_lines.append('                instr = next((i for i in instruments if i.get(\'symbol\', \'\').upper() == unified_format), None)')
        new_lines.append('')
        new_lines.append('        if not instr:')
        new_lines.append('            # Log available symbols for debugging (first 20 that contain similar base)')
        new_lines.append('            base_part = symbol_upper.replace(\'PF_\', \'\').replace(\'USD\', \'\').replace(\'/\', \'\')[:3]')
        new_lines.append('            similar = [i.get(\'symbol\', \'\') for i in instruments if base_part in i.get(\'symbol\', \'\').upper()][:20]')
        new_lines.append('            logger.error(')
        new_lines.append('                "Instrument specs not found",')
        new_lines.append('                requested_symbol=symbol,')
        new_lines.append('                similar_symbols=similar,')
        new_lines.append('                total_instruments=len(instruments),')
        new_lines.append('            )')
        new_lines.append('            raise ValueError(f"Instrument specs for {symbol} not found")')
        # Skip old lines until we find the next section
        while i < len(lines) and 'raise ValueError' not in lines[i]:
            i += 1
        i += 1  # Skip the raise line
        continue
    new_lines.append(lines[i])
    i += 1

with open(file_path, 'w') as f:
    f.write('\n'.join(new_lines))

print("Fix applied successfully!")
EOF

sudo python3 /tmp/apply_fix.py
```

## Step 3: Restart Service

```bash
sudo systemctl restart trading-system.service
sudo systemctl status trading-system.service
```

## Step 4: Monitor

```bash
sudo -u trading tail -f /home/trading/TradingSystem/logs/run.log | grep -E "Entry order submitted|Failed to submit|Instrument specs"
```
