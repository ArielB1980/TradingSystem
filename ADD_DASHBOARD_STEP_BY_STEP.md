# Step-by-Step: Add Dashboard Component

## Issue: Dashboard Component Not Showing

App Platform might not be reading `app.yaml` automatically. Let's add it manually.

## Step 1: Add Streamlit to Requirements ✅

I've added `streamlit>=1.28.0` to `requirements.txt`. Commit this:

```bash
git add requirements.txt
git commit -m "Add streamlit dependency"
git push
```

## Step 2: Add Component via App Platform UI

### Option A: Edit App Spec (Recommended)

1. **Go to:** https://cloud.digitalocean.com/apps/tradingbot-2tdzi/settings
2. **Scroll to:** "App Spec" section
3. **Click:** "Edit Spec" or "View Spec"
4. **Copy the entire `app.yaml` content** from your repo
5. **Paste it** into the spec editor
6. **Click:** "Save" or "Update"
7. **Wait:** App Platform will deploy all 3 components

### Option B: Add Component Manually

1. **Go to:** https://cloud.digitalocean.com/apps/tradingbot-2tdzi
2. **Click:** "Components" tab (or "Edit Components")
3. **Click:** "Add Component" or "+" button
4. **Select:** "Web Service"
5. **Configure:**
   - **Name:** `dashboard`
   - **Run Command:** `streamlit run src/dashboard/streamlit_app.py --server.port 8080 --server.address 0.0.0.0 --server.headless true`
   - **HTTP Port:** `8080`
   - **Instance Size:** `Basic XXS` (or smallest available)
   - **Routes:** Add route `/dashboard` (optional - component gets own URL)
6. **Environment Variables:**
   - Add `DATABASE_URL` (select from existing secrets)
   - Add `ENVIRONMENT` = `prod`
7. **Click:** "Save" or "Create"
8. **Wait:** Component will build and deploy

## Step 3: Verify Deployment

After adding the component:

1. **Check Components Tab:**
   - Should see 3 components: `web`, `worker`, `dashboard`
   - Dashboard status should be "Building" then "Running"

2. **Check Dashboard URL:**
   - Click on `dashboard` component
   - Look for "Live App URL" or "Public URL"
   - It will be something like: `https://dashboard-xxxxx.ondigitalocean.app`

3. **Test Dashboard:**
   ```bash
   curl -I <dashboard-url>
   ```
   Should return HTML (Content-Type: text/html)

## Step 4: Access Dashboard

Once deployed:
- **URL:** Check Components → Dashboard → URL
- **Or:** If route configured: `https://tradingbot-2tdzi.ondigitalocean.app/dashboard`
- **Open in browser:** Should see Streamlit dashboard

## Troubleshooting

### Component Still Not Showing
- **Check:** Settings → App Spec → Does it show dashboard?
- **If No:** App Platform might not be reading app.yaml - use Option B above
- **If Yes:** Check Build/Runtime Logs for errors

### Build Errors
- **Check:** Components → Dashboard → Runtime Logs
- **Common Issues:**
  - Missing `streamlit` in requirements.txt ✅ (just added)
  - Port conflict (should be 8080)
  - Database connection error

### Streamlit Not Starting
- **Check:** Runtime Logs for dashboard component
- **Verify:** Command is correct: `streamlit run src/dashboard/streamlit_app.py --server.port 8080 --server.address 0.0.0.0 --server.headless true`
- **Check:** `src/dashboard/streamlit_app.py` exists ✅

## Quick Checklist

- [x] Streamlit added to requirements.txt
- [ ] Commit and push requirements.txt
- [ ] Add dashboard component via App Platform UI
- [ ] Verify component appears in Components tab
- [ ] Check dashboard URL
- [ ] Test dashboard access

## Next Steps

1. **Commit requirements.txt:**
   ```bash
   git add requirements.txt
   git commit -m "Add streamlit for dashboard"
   git push
   ```

2. **Add component via App Platform UI** (Option A or B above)

3. **Wait 3-5 minutes** for deployment

4. **Check Components tab** for dashboard URL

5. **Access dashboard** and verify it works!
