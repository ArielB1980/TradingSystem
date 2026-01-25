# Cloud Platform Secret Injection Behavior

## Overview

This document explains how cloud platforms (DigitalOcean App Platform, AWS, GCP, etc.) inject secrets and how our system handles timing issues.

## The Problem

Cloud platforms inject `RUN_TIME` scoped secrets **asynchronously** after container startup. This creates a timing mismatch:

1. **Container starts** → Code begins executing
2. **Config validation runs** → Checks for secrets (may not be available yet)
3. **Secrets are injected** → Platform injects RUN_TIME secrets
4. **Operations begin** → Secrets are now available

If code validates secrets at startup (step 2), it will fail even though secrets are correctly configured and will be available later.

## DigitalOcean App Platform Behavior

### Secret Scopes

DigitalOcean App Platform has three secret scopes:

1. **BUILD_TIME**: Available during build phase
2. **RUN_TIME**: Available after container starts (injected asynchronously)
3. **GENERAL**: Available at both build and runtime

### Injection Timing

- **BUILD_TIME** secrets: Available immediately during build
- **RUN_TIME** secrets: Injected **after** container starts, typically within 1-5 seconds
- **GENERAL** secrets: Available at both phases

### Detection

Our system detects DigitalOcean App Platform by checking:

```python
is_do_platform = (
    os.getenv("DIGITALOCEAN_APP_ID") or 
    os.path.exists("/workspace")
)
```

## Our Solution: Lazy Validation with Retry

### 1. Lazy Secret Validation

Instead of validating secrets at startup, we validate them **when they're actually needed**:

```python
# ❌ BAD: Validates at startup
def get_db():
    url = os.getenv("DATABASE_URL")  # May not be available yet!
    if not url:
        raise RuntimeError("DATABASE_URL required")
    return Database(url)

# ✅ GOOD: Validates when needed with retry
def get_db():
    url = get_secret_with_retry("DATABASE_URL", max_retries=5)
    return Database(url)
```

### 2. Retry Logic with Exponential Backoff

The `get_secret_with_retry()` function:

- Waits up to 5 seconds for secrets to be injected
- Uses exponential backoff (1s, 1.5s, 2.25s, 3.38s, 5s)
- Provides clear error messages distinguishing:
  - **Timing issues**: "Secret not yet available (cloud platform secret injection in progress)"
  - **Configuration errors**: "Secret not configured in app spec"

### 3. Environment Detection

The system automatically detects the deployment environment:

- **Cloud platforms**: Lenient validation, retry logic, warnings instead of failures
- **Local development**: Strict validation, fail-fast with helpful error messages

## Implementation Details

### Secret Manager (`src/utils/secret_manager.py`)

Provides utilities for secret management:

- `is_cloud_platform()`: Detects cloud deployment environment
- `get_secret_with_retry()`: Gets secret with retry logic
- `check_secret_availability()`: Checks if secret is available (non-blocking)
- `get_database_url()`: Gets DATABASE_URL with validation
- `get_kraken_api_key()`: Gets API key with validation
- `get_kraken_api_secret()`: Gets API secret with validation

### Usage Examples

```python
from src.utils.secret_manager import get_database_url, get_kraken_api_key

# Lazy validation - validates when actually needed
db_url = get_database_url()  # Retries if not immediately available
api_key = get_kraken_api_key()  # Retries if not immediately available
```

### Health Checks

Health check endpoints (`/api/health`) verify secret availability:

```json
{
  "status": "healthy",
  "secrets": {
    "DATABASE_URL": {
      "available": true,
      "error": null
    },
    "KRAKEN_FUTURES_API_KEY": {
      "available": true,
      "error": null
    }
  }
}
```

## Best Practices

### 1. Use Lazy Validation

✅ **DO**: Validate secrets when they're actually needed
```python
def get_db():
    url = get_secret_with_retry("DATABASE_URL")
    return Database(url)
```

❌ **DON'T**: Validate secrets at startup
```python
# At module level or startup
url = os.getenv("DATABASE_URL")
if not url:
    raise RuntimeError("DATABASE_URL required")
```

### 2. Provide Clear Error Messages

✅ **DO**: Distinguish timing issues from configuration errors
```python
if is_cloud_platform():
    error_msg = "Secret not yet available (cloud platform secret injection in progress)"
else:
    error_msg = "Secret not set (required for local development)"
```

❌ **DON'T**: Use generic error messages
```python
raise RuntimeError("Secret required")  # Doesn't explain why
```

### 3. Use Retry Logic for Cloud Platforms

✅ **DO**: Wait for secrets with exponential backoff
```python
url = get_secret_with_retry("DATABASE_URL", max_retries=5)
```

❌ **DON'T**: Fail immediately if secret not available
```python
url = os.getenv("DATABASE_URL")
if not url:
    raise RuntimeError("DATABASE_URL required")  # Fails too early
```

## Troubleshooting

### Secret Not Available After Retries

**Symptoms:**
- Error: "Secret not available after 5 retry attempts"
- Health check shows secret as unavailable

**Possible Causes:**
1. Secret not configured in app spec (check `.do/app.yaml`)
2. Secret scope is wrong (should be `RUN_TIME` for runtime secrets)
3. Secret name mismatch (check exact variable name)
4. Platform issue (secrets taking longer than expected)

**Solutions:**
1. Verify secret is in `.do/app.yaml` with correct scope
2. Check DigitalOcean dashboard for secret configuration
3. Verify secret name matches exactly (case-sensitive)
4. Check deployment logs for secret injection timing

### Secrets Available But Operations Fail

**Symptoms:**
- Health check shows secrets as available
- Operations still fail with authentication/connection errors

**Possible Causes:**
1. Secret value is incorrect (wrong API key, wrong database URL)
2. Secret format issue (extra spaces, line breaks)
3. Network/permissions issue (not a secret problem)

**Solutions:**
1. Verify secret values in DigitalOcean dashboard
2. Check for formatting issues (no extra spaces)
3. Test secret values manually (connect to DB, test API)

## Related Documentation

- `DEPLOYMENT_LESSONS_LEARNED.md`: Lessons learned from deployment debugging
- `src/utils/secret_manager.py`: Secret management implementation
- `.do/app.yaml`: DigitalOcean App Platform configuration
