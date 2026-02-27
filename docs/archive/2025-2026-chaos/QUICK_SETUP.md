# Quick Setup Guide - DigitalOcean App Platform

## Database Connection String

Your database is configured. Use this exact connection string:

```
DATABASE_URL=postgresql://dev-db-507728:AVNS_uzSwineC6iITlCPrW0a@app-0b6d8990-7a22-4f2f-bbf6-c5f54116bb6d-do-user-31978256-0.h.db.ondigitalocean.com:25060/dev-db-507728?sslmode=require
```

## Steps to Deploy

### 1. Set Environment Variables in App Platform

Go to: **DigitalOcean Dashboard → Apps → Your App → Settings → App-Level Environment Variables**

Add these variables:

```bash
# Database (REQUIRED)
DATABASE_URL=postgresql://dev-db-507728:AVNS_uzSwineC6iITlCPrW0a@app-0b6d8990-7a22-4f2f-bbf6-c5f54116bb6d-do-user-31978256-0.h.db.ondigitalocean.com:25060/dev-db-507728?sslmode=require

# Exchange Credentials (REQUIRED)
KRAKEN_API_KEY=your_kraken_api_key
KRAKEN_API_SECRET=your_kraken_api_secret
KRAKEN_FUTURES_API_KEY=your_futures_api_key
KRAKEN_FUTURES_API_SECRET=your_futures_api_secret

# Optional
ENVIRONMENT=prod
LOG_LEVEL=INFO
```

### 2. Commit and Push Files

```bash
git add .python-version Procfile
git commit -m "Add App Platform configuration"
git push
```

### 3. Verify Build

- App Platform will automatically rebuild
- Check build logs for success
- Verify Python 3.11 is used (not 3.13)

### 4. Initialize Database

After first deployment, you may need to initialize database tables. You can do this by:

**Option A: SSH into app (if available)**
```bash
# Run database initialization
python run.py migrate  # If you have migrations
# Or manually create tables
python -c "from src.storage.db import get_db; db = get_db(); db.create_all()"
```

**Option B: Add initialization script**
Create a one-time setup component or use a startup script.

### 5. Monitor Logs

- **App Platform → Runtime Logs** - Check for errors
- **Databases → Your Database → Metrics** - Monitor database connections

## Database Details

- **Host:** `app-0b6d8990-7a22-4f2f-bbf6-c5f54116bb6d-do-user-31978256-0.h.db.ondigitalocean.com`
- **Port:** `25060`
- **Database:** `dev-db-507728`
- **Username:** `dev-db-507728`
- **SSL:** Required (`sslmode=require`)

## Troubleshooting

### Connection Refused
- Check firewall settings in DigitalOcean dashboard
- Ensure App Platform IPs are allowed
- Verify database is running

### Authentication Failed
- Double-check username and password
- Ensure password doesn't have special characters that need URL encoding

### SSL Error
- Verify `?sslmode=require` is in connection string
- Check `psycopg2-binary` is installed (already in requirements.txt)

### Tables Don't Exist
- Run database initialization after first deploy
- Check logs for table creation errors

## Security Notes

⚠️ **IMPORTANT:** 
- Never commit `.env` files with real credentials
- The `DATABASE_URL.txt` file contains sensitive data - consider adding to `.gitignore`
- Use App Platform environment variables (encrypted at rest)
- Rotate database password periodically

## Next Steps

1. ✅ Set `DATABASE_URL` in App Platform
2. ✅ Set API credentials in App Platform  
3. ✅ Push `.python-version` and `Procfile` to GitHub
4. ✅ Monitor build and runtime logs
5. ✅ Initialize database tables after first deploy
6. ✅ Start trading!
