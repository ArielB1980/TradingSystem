# Dashboard Setup for App Platform

## Current Status

The dashboard is **NOT currently running** on App Platform. It needs to be added as a separate component.

## What Was Added

1. **Streamlit** added to `requirements.txt`
2. **Dashboard process** added to `Procfile`
3. Dashboard will run on port 8080 (App Platform default)

## How to Enable Dashboard

### Option 1: Add Dashboard Component in App Platform

1. **Go to:** DigitalOcean Dashboard → Apps → Your App → Components
2. **Click:** "Add Component" or "Edit Spec"
3. **Add a new service component:**

   ```yaml
   - name: dashboard
     type: web
     run_command: streamlit run src/dashboard/streamlit_app.py --server.port 8080 --server.address 0.0.0.0 --server.headless true
     http_port: 8080
     instance_count: 1
     instance_size_slug: basic-xxs
   ```

4. **Save** - App Platform will deploy the dashboard

### Option 2: Use App Spec File

If you're using an app spec file, add:

```yaml
services:
  - name: dashboard
    run_command: streamlit run src/dashboard/streamlit_app.py --server.port 8080 --server.address 0.0.0.0 --server.headless true
    http_port: 8080
    instance_count: 1
    instance_size_slug: basic-xxs
    routes:
      - path: /
```

## Access Dashboard

Once deployed, the dashboard will be available at:
- **URL:** https://tradingbot-2tdzi.ondigitalocean.app (if configured as main route)
- **Or:** Check App Platform for the dashboard component URL

## Dashboard Features

- Real-time coin monitoring
- Signal generation display
- Position tracking
- Performance metrics
- Data freshness indicators
- Filtering and search

## Troubleshooting

### Dashboard Not Accessible
- Check if dashboard component is running in App Platform
- Verify port 8080 is configured
- Check Runtime Logs for errors

### Streamlit Errors
- Ensure `streamlit` is in requirements.txt ✅ (already added)
- Check database connection
- Verify environment variables are set

### Port Conflicts
- Dashboard uses port 8080
- Health check uses port 8080 (web component)
- May need separate components or different ports

## Current Procfile

```
web: python -m src.health
worker: python run.py live --force
dashboard: streamlit run src/dashboard/streamlit_app.py --server.port 8080 --server.address 0.0.0.0 --server.headless true
```

Note: App Platform may need the dashboard configured as a separate component in the app spec, not just in Procfile.
