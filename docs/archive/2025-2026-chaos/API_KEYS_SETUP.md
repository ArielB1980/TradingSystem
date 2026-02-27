# API Keys Setup Instructions

## ⚠️ SECURITY WARNING
**NEVER commit API keys to git or store them in code files!**

## Add to App Platform

1. **Go to DigitalOcean Dashboard:**
   - Navigate to: Apps → Your App (`tradingbot-2tdzi`)
   - Click **Settings** → **App-Level Environment Variables**

2. **Add these environment variables:**

   ```
   KRAKEN_API_KEY=Yn8ZB7+enpsmhv6QCC+QitHe/qz4+Tu25kqWfZmgwKckY24IAr4FD5lN
   KRAKEN_API_SECRET=HdPZPP2GYy4gGa1gvG5DvtmlzlcE2foIsIozOQRg6oovaswSuXSc1lvS3abJ+WEWK9r/GBKOtHbUxMNNgmp6PA==
   KRAKEN_FUTURES_API_KEY=2k1daXUJari2fsDGsQ21rNgF1xeL3obeT+ojmNcpuS44SPMYXaKV6KMx
   KRAKEN_FUTURES_API_SECRET=4h77HOI0onjBh4zgklakpVwLrbCg0GZNrCeOBOUQPMOIVcciOFEJ9yljOy2Fm746UznwVCpSqPbKsMqyxNOUmBoM
   ENVIRONMENT=prod
   ```

3. **Save Changes:**
   - Click **Save** or **Update**
   - App Platform will automatically restart your app

4. **Verify:**
   - Wait 1-2 minutes for restart
   - Visit: https://tradingbot-2tdzi.ondigitalocean.app/quick-test
   - Should show: `"api_keys": "futures_configured"`

## After Adding Keys

1. **Check Runtime Logs:**
   - Apps → Your App → Runtime Logs
   - Look for "Live trading started"
   - Check for any authentication errors

2. **Test API Connection:**
   - Visit: https://tradingbot-2tdzi.ondigitalocean.app/test
   - Should test API connectivity

3. **Monitor Trading:**
   - Check logs for signal generation
   - Monitor database for trade activity
   - Watch for position entries

## Security Best Practices

- ✅ Keys are encrypted at rest in App Platform
- ✅ Never commit keys to git (already in .gitignore)
- ✅ Rotate keys periodically
- ✅ Use read-only keys if possible
- ✅ Monitor for unauthorized access

## Troubleshooting

### Keys Not Working
- Verify keys are correct (no extra spaces)
- Check key permissions in Kraken dashboard
- Ensure keys are for correct environment (mainnet vs testnet)

### Authentication Errors
- Check Runtime Logs for specific error messages
- Verify key format (no line breaks)
- Ensure futures keys have futures trading permissions

### App Not Restarting
- Check App Platform status
- Verify environment variables are saved
- Check build/deploy logs
