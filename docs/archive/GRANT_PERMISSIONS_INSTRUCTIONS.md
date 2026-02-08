# Grant Database Permissions - Quick Instructions

## Your Database Username

From your DATABASE_URL, your username is: **`dbtradingbot`**

## Option 1: Run Script from App Console (Easiest)

1. In DigitalOcean App Platform → Your App → Console tab
2. Run this command:

```bash
python scripts/grant_database_permissions.py
```

**Note:** This might fail if `dbtradingbot` doesn't have permission to grant privileges. If it fails, use Option 2.

## Option 2: Connect as Superuser and Run SQL

You need to connect as a database superuser (usually `doadmin`). Here's how:

### Step 1: Get Superuser Connection String

1. Go to DigitalOcean Dashboard → **Databases** (not Apps)
2. Click on your database
3. Click **"Connection Details"**
4. Look for the **"Admin"** or **"Superuser"** connection string
5. It will look like: `postgresql://doadmin:password@host:port/dbtradingbot?sslmode=require`

### Step 2: Connect and Run SQL

**Option A: Using DigitalOcean Database Console**

1. In the database details page, look for **"Console"** or **"Query"** tab
2. Click it to open SQL console
3. Run these commands:

```sql
GRANT CREATE ON SCHEMA public TO dbtradingbot;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO dbtradingbot;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO dbtradingbot;
```

**Option B: Using psql from Your Computer**

```bash
# Install psql if needed (macOS: brew install postgresql)
psql "postgresql://doadmin:YOUR_ADMIN_PASSWORD@app-65e2763f-0c06-4d87-a349-ddc49db0abf3-do-user-31978256-0.l.db.ondigitalocean.com:25060/dbtradingbot?sslmode=require"

# Then run:
GRANT CREATE ON SCHEMA public TO dbtradingbot;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO dbtradingbot;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO dbtradingbot;
\q
```

**Option C: Using the Script with Admin Credentials**

1. Get the admin connection string from DigitalOcean
2. Run:

```bash
python scripts/grant_database_permissions.py "postgresql://doadmin:ADMIN_PASSWORD@host:port/dbtradingbot?sslmode=require"
```

## Verify Permissions Were Granted

After running the commands, verify:

```sql
-- Check schema privileges
\dn+ public

-- Or query:
SELECT grantee, privilege_type 
FROM information_schema.role_table_grants 
WHERE table_schema = 'public' AND grantee = 'dbtradingbot';
```

## After Granting Permissions

1. **Restart your app** in DigitalOcean App Platform
2. The migration script should now successfully create tables
3. Check the logs to confirm tables were created

## Troubleshooting

**"permission denied" when running script as dbtradingbot?**
- This is expected - you need superuser privileges
- Use Option 2 with `doadmin` credentials

**Can't find database console?**
- The database is separate from your app
- Go to DigitalOcean → **Databases** (left sidebar), not Apps

**Still getting permission errors?**
- Make sure you're connected as `doadmin` (superuser)
- Verify the username in GRANT command matches: `dbtradingbot`
- Contact DigitalOcean support if issues persist
