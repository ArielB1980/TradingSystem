# Manual Dashboard Component Setup

## Issue: Dashboard Component Not Showing

If the dashboard component isn't appearing in App Platform, you may need to add it manually or update the app spec through the UI.

## Option 1: Add Component via App Platform UI

### Step 1: Go to App Settings
1. Visit: https://cloud.digitalocean.com/apps/tradingbot-2tdzi
2. Click **"Settings"** tab
3. Scroll down to **"App Spec"** section
4. Click **"Edit Spec"** or **"View Spec"**

### Step 2: Add Dashboard Service
In the app spec YAML, add this service section:

```yaml
services:
  # ... existing web and worker services ...
  
  - name: dashboard
    github:
      repo: ArielB1980/TradingSystem
      branch: main
    run_command: streamlit run src/dashboard/streamlit_app.py --server.port 8080 --server.address 0.0.0.0 --server.headless true
    http_port: 8080
    instance_count: 1
    instance_size_slug: basic-xxs
    routes:
      - path: /dashboard
    envs:
      - key: DATABASE_URL
        scope: RUN_TIME
        type: SECRET
      - key: ENVIRONMENT
        value: prod
        scope: RUN_TIME
        type: GENERAL
```

### Step 3: Save and Deploy
- Click **"Save"** or **"Update"**
- App Platform will automatically deploy the new component

## Option 2: Use Procfile (Simpler Alternative)

If App Platform is using Procfile instead of app.yaml, update your Procfile:

```
web: python -m src.health
worker: python run.py live --force
dashboard: streamlit run src/dashboard/streamlit_app.py --server.port 8080 --server.address 0.0.0.0 --server.headless true
```

Then add the dashboard as a separate component in App Platform UI.

## Option 3: Create Separate App for Dashboard

If multi-component setup isn't working:

1. **Create New App:**
   - Go to Apps → Create → App Platform
   - Connect same GitHub repo
   - Select branch: `main`

2. **Configure Dashboard:**
   - Name: `trading-dashboard`
   - Run Command: `streamlit run src/dashboard/streamlit_app.py --server.port 8080 --server.address 0.0.0.0 --server.headless true`
   - Environment Variables:
     - `DATABASE_URL` (from your main app)
     - `ENVIRONMENT=prod`

3. **Deploy:**
   - This creates a separate dashboard app
   - Share same database connection

## Option 4: Check Current App Spec

1. Go to: App Platform → Your App → Settings → App Spec
2. **Copy the current spec** and check:
   - Does it have all 3 services (web, worker, dashboard)?
   - Is the dashboard service properly formatted?
   - Are there any YAML syntax errors?

## Troubleshooting

### Component Not Appearing
- **Check:** Settings → App Spec → Does it show dashboard?
- **If No:** The spec wasn't updated - use Option 1
- **If Yes:** Check Build/Runtime Logs for errors

### Build Errors
- Check Runtime Logs for dashboard component
- Verify `streamlit` is in requirements.txt ✅
- Check Streamlit command syntax

### Routing Issues
- Dashboard might get its own URL (not `/dashboard` route)
- Check Components tab for dashboard URL
- Each component gets unique domain in App Platform

## Quick Verification

After adding dashboard component:

```bash
# Check if dashboard component exists
# (You'll need to check App Platform UI)

# Once deployed, test dashboard URL
curl -I <dashboard-url-from-app-platform>
```

Should return HTML (Content-Type: text/html) when ready.
