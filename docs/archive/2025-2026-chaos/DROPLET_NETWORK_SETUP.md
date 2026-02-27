# Droplet Network Setup for Database Access

## Issue: Database Hostname Not Resolving

The Droplet can't connect to the managed database because:

1. **Trusted Sources** - The database needs to allow connections from the Droplet's IP
2. **VPC/Network** - The Droplet and database might need to be in the same VPC

## Solution Steps:

### Step 1: Add Droplet IP to Database Trusted Sources

1. Go to **DigitalOcean Dashboard → Databases → Your Database**
2. Click **"Trusted Sources"** tab
3. Click **"Add Trusted Source"**
4. Add your Droplet's IP: **207.154.193.121**
5. Or add **0.0.0.0/0** (allow all - less secure but works for testing)

### Step 2: Verify Network Connectivity

After adding trusted source, test from Droplet:

```bash
ssh -i ~/.ssh/trading_droplet trading@207.154.193.121

# Test DNS resolution
nslookup app-65e2763f-0c06-4d87-a349-ddc49db0abf3-do-user-31978256-0.l.db.ondigitalocean.com

# Test connection
psql "postgresql://dbtradingbot:AVNS_3ZbhLloQP64uLYyhxoe@app-65e2763f-0c06-4d87-a349-ddc49db0abf3-do-user-31978256-0.l.db.ondigitalocean.com:25060/dbtradingbot?sslmode=require" -c "SELECT version();"
```

### Step 3: If Still Not Working - Check VPC

If the database and Droplet are in different VPCs:

1. **Option A:** Move Droplet to same VPC as database
   - DigitalOcean → Droplets → Your Droplet → Settings → Networking
   - Assign to same VPC as database

2. **Option B:** Use database's public endpoint (if available)
   - Check database connection details for public vs private endpoint

## Current Status

✅ **Completed:**
- Droplet created and accessible
- Application files deployed
- Dependencies installed
- .env file configured
- Systemd service created

⏳ **Pending:**
- Database network access (add Droplet IP to trusted sources)
- Grant database permissions (need doadmin credentials)
- Initialize database tables
- Start the service

## Next Steps After Network is Fixed:

1. **Grant Database Permissions:**
```bash
# Connect as doadmin (you'll need admin password)
psql "postgresql://doadmin:ADMIN_PASSWORD@host:port/dbtradingbot?sslmode=require"

# Run:
GRANT CREATE ON SCHEMA public TO dbtradingbot;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO dbtradingbot;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO dbtradingbot;
\q
```

2. **Initialize Database:**
```bash
ssh -i ~/.ssh/trading_droplet trading@207.154.193.121
cd ~/TradingSystem
source venv/bin/activate
python migrate_schema.py
```

3. **Start Service:**
```bash
sudo systemctl start trading-system.service
sudo systemctl status trading-system.service
```
