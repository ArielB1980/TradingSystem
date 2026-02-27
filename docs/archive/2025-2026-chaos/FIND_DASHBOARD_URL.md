# How to Find Your Dashboard URL

## Quick Steps

### Step 1: Go to App Platform Components
1. Visit: **https://cloud.digitalocean.com/apps/tradingbot-2tdzi/components**
2. Look for the **"dashboard"** component in the list

### Step 2: Get Dashboard URL
- Click on the **"dashboard"** component
- Look for **"Live App URL"** or **"Public URL"**
- It will look like: `https://dashboard-xxxxx.ondigitalocean.app`
- **OR** if route is configured: `https://tradingbot-2tdzi.ondigitalocean.app/dashboard`

### Step 3: Access Dashboard
- Click the URL or copy/paste into browser
- Should see Streamlit dashboard interface

## Alternative: Check via App Platform

1. Go to: **https://cloud.digitalocean.com/apps/tradingbot-2tdzi**
2. Click **"Components"** tab
3. Find **"dashboard"** component
4. Click on it to see URL and status

## Quick Test

Once you have the URL, test it:
```bash
curl -I <dashboard-url>
```

Should return `Content-Type: text/html` when working.

## If Dashboard Not Showing

- Check Components tab - is dashboard component listed?
- Check component status - should be "Running" (green)
- Check Runtime Logs for any errors
- Wait 2-3 minutes if just deployed
