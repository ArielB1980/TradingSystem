# Background Running Setup

**Date**: 2025-01-10  
**Status**: ✅ **CONFIGURED**

## Overview

The live trading system is now configured to run in the background with proper process management and logging.

## Management Scripts

Three scripts have been created to manage the background process:

### 1. Start Live Trading
```bash
scripts/start_live_trading.sh
```

**What it does:**
- Starts the live trading system in the background
- Uses `nohup` to survive terminal disconnects
- Redirects output to log files
- Creates PID file for process tracking
- Verifies the process started successfully

**Logs:**
- `logs/live_trading_stdout.log` - Standard output
- `logs/live_trading_stderr.log` - Standard error
- `logs/live_trading.log` - Combined logs (if configured)

### 2. Stop Live Trading
```bash
scripts/stop_live_trading.sh
```

**What it does:**
- Gracefully stops the running process (SIGTERM)
- Waits up to 30 seconds for graceful shutdown
- Force kills if necessary (SIGKILL)
- Removes PID file

### 3. Check Status
```bash
scripts/status_live_trading.sh
```

**What it does:**
- Checks if the process is running
- Shows process information (PID, CPU, memory, runtime)
- Shows log file sizes
- Cleans up stale PID files

## Usage

### Starting the System

```bash
cd /Users/arielbarack/Programming/PT_Cursor/TradingSystem
scripts/start_live_trading.sh
```

The system will:
- Start in the background
- Continue running even if you close the terminal
- Log all output to log files
- Store PID in `.live_trading.pid`

### Checking Status

```bash
scripts/status_live_trading.sh
```

Shows:
- Whether the system is running
- Process ID (PID)
- Process statistics (CPU, memory, runtime)
- Log file sizes

### Viewing Logs

```bash
# View stdout logs
tail -f logs/live_trading_stdout.log

# View stderr logs
tail -f logs/live_trading_stderr.log

# View both
tail -f logs/live_trading_stdout.log logs/live_trading_stderr.log
```

### Stopping the System

```bash
scripts/stop_live_trading.sh
```

This will:
1. Send SIGTERM (graceful shutdown)
2. Wait for process to stop
3. Force kill if necessary
4. Clean up PID file

## Process Management

### PID File

The system stores its process ID in:
```
.live_trading.pid
```

This file is:
- Created when the system starts
- Removed when the system stops
- Used to track if the system is running

### Background Execution

The system runs with `nohup`, which:
- Detaches from terminal
- Continues running after terminal closes
- Redirects output to log files
- Survives SSH disconnections

## Log Files

All output is logged to:

- **stdout**: `logs/live_trading_stdout.log`
- **stderr**: `logs/live_trading_stderr.log`

These files:
- Are created automatically
- Grow as the system runs
- Can be rotated if needed
- Should be monitored regularly

## Monitoring

### Check if Running

```bash
scripts/status_live_trading.sh
```

### View Real-time Logs

```bash
tail -f logs/live_trading_stdout.log
```

### Check Process Directly

```bash
# If PID file exists
PID=$(cat .live_trading.pid)
ps -p $PID

# Or search for process
ps aux | grep "run.py live"
```

## System Integration

### Systemd (Linux)

For Linux systems, you can create a systemd service:

```ini
[Unit]
Description=Live Trading System
After=network.target

[Service]
Type=simple
User=your_user
WorkingDirectory=/path/to/TradingSystem
ExecStart=/usr/bin/python3 run.py live --force
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### LaunchAgent (macOS)

For macOS, you can create a LaunchAgent:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.trading.live</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/path/to/TradingSystem/run.py</string>
        <string>live</string>
        <string>--force</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/path/to/TradingSystem</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/path/to/TradingSystem/logs/live_trading_stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/path/to/TradingSystem/logs/live_trading_stderr.log</string>
</dict>
</plist>
```

## Troubleshooting

### Process Not Starting

1. Check logs: `cat logs/live_trading_stderr.log`
2. Check Python path: `which python3`
3. Check permissions: `ls -la scripts/start_live_trading.sh`
4. Check if already running: `scripts/status_live_trading.sh`

### Process Died

1. Check stderr logs: `cat logs/live_trading_stderr.log`
2. Check system resources: `top` or `htop`
3. Check disk space: `df -h`
4. Restart: `scripts/start_live_trading.sh`

### Can't Stop Process

1. Force kill: `kill -9 $(cat .live_trading.pid)`
2. Find and kill: `pkill -f "run.py live"`
3. Remove PID file: `rm .live_trading.pid`

## Quick Reference

```bash
# Start
scripts/start_live_trading.sh

# Check status
scripts/status_live_trading.sh

# View logs
tail -f logs/live_trading_stdout.log

# Stop
scripts/stop_live_trading.sh
```

---

**✅ BACKGROUND RUNNING CONFIGURED**

The system is now set up to run in the background with proper process management and logging.
