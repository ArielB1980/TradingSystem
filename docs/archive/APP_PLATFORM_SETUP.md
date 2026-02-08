# DigitalOcean App Platform Setup

Your app is deploying on DigitalOcean App Platform (not a Droplet). Here's what you need to configure:

## ✅ Build Completed Successfully

The build logs show:
- ✅ Python 3.13 installed (will use 3.11 after next deploy with `.python-version`)
- ✅ All dependencies installed successfully
- ✅ App image uploaded to DOCR

## Required Configuration

### 1. Environment Variables

In DigitalOcean App Platform dashboard → Your App → Settings → App-Level Environment Variables:

**Required:**
```bash
DATABASE_URL=postgresql://dev-db-507728:AVNS_uzSwineC6iITlCPrW0a@app-0b6d8990-7a22-4f2f-bbf6-c5f54116bb6d-do-user-31978256-0.h.db.ondigitalocean.com:25060/dev-db-507728?sslmode=require
```

**Exchange Credentials:**
```bash
KRAKEN_API_KEY=your_api_key
KRAKEN_API_SECRET=your_api_secret
KRAKEN_FUTURES_API_KEY=your_futures_api_key
KRAKEN_FUTURES_API_SECRET=your_futures_api_secret
```

**Optional:**
```bash
ENVIRONMENT=prod
LOG_LEVEL=INFO
```

### 2. Database Connection

Your database ID: `e2db78ca-4d22-4203-822f-2e03ed2f08f7`

1. **In App Platform Dashboard:**
   - Go to your app → Settings → Components
   - Add Database Component (if not already added)
   - Select your existing database: `e2db78ca-4d22-4203-822f-2e03ed2f08f7`
   - This will automatically set `DATABASE_URL` environment variable

2. **Or manually set DATABASE_URL:**
   - Get connection string from Databases → Your Database → Connection Details
   - Format: `postgresql://username:password@host:port/database_name?sslmode=require`
   - Add as environment variable in App Platform

### 3. Procfile

The `Procfile` tells App Platform how to run your app:

```
web: python run.py live
worker: python run.py live
```

This is already created in the repo.

### 4. Python Version

The `.python-version` file specifies Python 3.11 (compatible with all dependencies).

## App Platform vs Droplet

**App Platform (Current):**
- ✅ Managed platform (no server management)
- ✅ Automatic deployments from GitHub
- ✅ Built-in scaling
- ✅ Managed database integration
- ⚠️ More expensive
- ⚠️ Less control over system

**Droplet (Alternative):**
- ✅ Full control
- ✅ Lower cost
- ✅ Can run multiple services
- ⚠️ Requires server management
- ⚠️ Manual deployments

## Next Steps

1. **Set Environment Variables:**
   - Go to App Platform dashboard
   - Settings → App-Level Environment Variables
   - Add `DATABASE_URL` and API credentials

2. **Link Database:**
   - Components → Add Database Component
   - Select your database: `e2db78ca-4d22-4203-822f-2e03ed2f08f7`
   - This auto-configures `DATABASE_URL`

3. **Deploy:**
   - Push `.python-version` and `Procfile` to GitHub
   - App Platform will auto-deploy
   - Or trigger manual deploy from dashboard

4. **Check Logs:**
   - App Platform → Runtime Logs
   - Monitor for errors

## Troubleshooting

### Build Fails
- Check Python version compatibility
- Verify `requirements.txt` is correct
- Check build logs in App Platform

### App Won't Start
- Check `Procfile` syntax
- Verify environment variables are set
- Check runtime logs

### Database Connection Fails
- Verify `DATABASE_URL` is set correctly
- Check database firewall allows App Platform IPs
- Ensure SSL is enabled (`?sslmode=require`)

### Missing Dependencies
- Add to `requirements.txt`
- Push to GitHub
- App Platform will rebuild

## Monitoring

- **Logs:** App Platform → Runtime Logs
- **Metrics:** App Platform → Metrics tab
- **Database:** Databases → Your Database → Metrics

## Cost Optimization

- Use Basic plan for development
- Scale down when not trading
- Use connection pooling (already configured)
- Monitor resource usage
