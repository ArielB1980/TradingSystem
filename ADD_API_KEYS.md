# Add API Keys to App Platform

## Option 1: Using DigitalOcean Dashboard (Recommended)

1. **Go to:** https://cloud.digitalocean.com/apps
2. **Click:** Your app (`tradingbot-2tdzi`)
3. **Click:** Settings → App-Level Environment Variables
4. **Add these variables:**

   ```
   KRAKEN_API_KEY = Yn8ZB7+enpsmhv6QCC+QitHe/qz4+Tu25kqWfZmgwKckY24IAr4FD5lN
   KRAKEN_API_SECRET = HdPZPP2GYy4gGa1gvG5DvtmlzlcE2foIsIozOQRg6oovaswSuXSc1lvS3abJ+WEWK9r/GBKOtHbUxMNNgmp6PA==
   KRAKEN_FUTURES_API_KEY = 2k1daXUJari2fsDGsQ21rNgF1xeL3obeT+ojmNcpuS44SPMYXaKV6KMx
   KRAKEN_FUTURES_API_SECRET = 4h77HOI0onjBh4zgklakpVwLrbCg0GZNrCeOBOUQPMOIVcciOFEJ9yljOy2Fm746UznwVCpSqPbKsMqyxNOUmBoM
   ENVIRONMENT = prod
   ```

5. **Save** - App will restart automatically

## Option 2: Using API Script

If you have a DigitalOcean API token:

```bash
# Get your API token from:
# https://cloud.digitalocean.com/account/api/tokens

export DIGITALOCEAN_API_TOKEN=your_token
python scripts/add_env_vars.py
```

## Verify After Adding

Wait 1-2 minutes for restart, then:

```bash
curl https://tradingbot-2tdzi.ondigitalocean.app/quick-test
```

Should show: `"api_keys": "futures_configured"`

## Quick Copy-Paste for Dashboard

**Variable Name:** `KRAKEN_API_KEY`  
**Value:** `Yn8ZB7+enpsmhv6QCC+QitHe/qz4+Tu25kqWfZmgwKckY24IAr4FD5lN`

**Variable Name:** `KRAKEN_API_SECRET`  
**Value:** `HdPZPP2GYy4gGa1gvG5DvtmlzlcE2foIsIozOQRg6oovaswSuXSc1lvS3abJ+WEWK9r/GBKOtHbUxMNNgmp6PA==`

**Variable Name:** `KRAKEN_FUTURES_API_KEY`  
**Value:** `2k1daXUJari2fsDGsQ21rNgF1xeL3obeT+ojmNcpuS44SPMYXaKV6KMx`

**Variable Name:** `KRAKEN_FUTURES_API_SECRET`  
**Value:** `4h77HOI0onjBh4zgklakpVwLrbCg0GZNrCeOBOUQPMOIVcciOFEJ9yljOy2Fm746UznwVCpSqPbKsMqyxNOUmBoM`

**Variable Name:** `ENVIRONMENT`  
**Value:** `prod`
