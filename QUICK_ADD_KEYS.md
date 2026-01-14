# Quick Add API Keys - Copy & Paste Guide

## Step-by-Step (Takes 2 minutes)

### 1. Open DigitalOcean Dashboard
👉 **Click:** https://cloud.digitalocean.com/apps/tradingbot-2tdzi/settings

### 2. Go to Environment Variables
- Click **"App-Level Environment Variables"** section
- Or scroll to **"Environment Variables"**

### 3. Add Each Variable (Click "Add Variable" 5 times)

**Variable 1:**
```
Key: KRAKEN_API_KEY
Value: Yn8ZB7+enpsmhv6QCC+QitHe/qz4+Tu25kqWfZmgwKckY24IAr4FD5lN
```

**Variable 2:**
```
Key: KRAKEN_API_SECRET
Value: HdPZPP2GYy4gGa1gvG5DvtmlzlcE2foIsIozOQRg6oovaswSuXSc1lvS3abJ+WEWK9r/GBKOtHbUxMNNgmp6PA==
```

**Variable 3:**
```
Key: KRAKEN_FUTURES_API_KEY
Value: 2k1daXUJari2fsDGsQ21rNgF1xeL3obeT+ojmNcpuS44SPMYXaKV6KMx
```

**Variable 4:**
```
Key: KRAKEN_FUTURES_API_SECRET
Value: 4h77HOI0onjBh4zgklakpVwLrbCg0GZNrCeOBOUQPMOIVcciOFEJ9yljOy2Fm746UznwVCpSqPbKsMqyxNOUmBoM
```

**Variable 5:**
```
Key: ENVIRONMENT
Value: prod
```

### 4. Save
- Click **"Save"** or **"Update"**
- App will automatically restart (takes 1-2 minutes)

### 5. Verify
After 1-2 minutes, run:
```bash
curl https://tradingbot-2tdzi.ondigitalocean.app/quick-test
```

Should show: `"api_keys": "futures_configured"`

---

## Direct Link (if logged in)
https://cloud.digitalocean.com/apps/tradingbot-2tdzi/settings

## All Values in One Place (for easy copy)

```
KRAKEN_API_KEY=Yn8ZB7+enpsmhv6QCC+QitHe/qz4+Tu25kqWfZmgwKckY24IAr4FD5lN
KRAKEN_API_SECRET=HdPZPP2GYy4gGa1gvG5DvtmlzlcE2foIsIozOQRg6oovaswSuXSc1lvS3abJ+WEWK9r/GBKOtHbUxMNNgmp6PA==
KRAKEN_FUTURES_API_KEY=2k1daXUJari2fsDGsQ21rNgF1xeL3obeT+ojmNcpuS44SPMYXaKV6KMx
KRAKEN_FUTURES_API_SECRET=4h77HOI0onjBh4zgklakpVwLrbCg0GZNrCeOBOUQPMOIVcciOFEJ9yljOy2Fm746UznwVCpSqPbKsMqyxNOUmBoM
ENVIRONMENT=prod
```
