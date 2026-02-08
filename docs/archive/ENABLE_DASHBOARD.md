# Enable Dashboard - Quick Guide

## Option 1: Use App Spec File (Easiest)

I've created an `app.yaml` file with the dashboard component configured. To use it:

1. **Go to:** DigitalOcean Dashboard → Apps → Your App → Settings
2. **Click:** "Edit App Spec" or "App Spec" tab
3. **Copy the contents of `app.yaml`** from the repository
4. **Paste and Save** - App Platform will deploy all components including dashboard

## Option 2: Add Component Manually

1. **Go to:** https://cloud.digitalocean.com/apps/tradingbot-2tdzi/components
2. **Click:** "Add Component" or "Edit Spec"
3. **Add this service:**

```yaml
- name: dashboard
  type: web
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

4. **Save** - Dashboard will deploy

## Option 3: Use API Script

If you have a DigitalOcean API token:

```bash
export DIGITALOCEAN_API_TOKEN=your_token
python scripts/add_dashboard_component.py
```

## After Deployment

The dashboard will be available at:
- Check App Platform for the dashboard component URL
- Or it may be accessible via: `https://your-app-url/dashboard`

## What's Ready

✅ Streamlit added to requirements.txt  
✅ Dashboard process in Procfile  
✅ App spec file created (`app.yaml`)  
✅ API script ready (`scripts/add_dashboard_component.py`)  

Just need to add the component in App Platform!
