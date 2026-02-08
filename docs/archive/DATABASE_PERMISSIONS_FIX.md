# Database Permissions Fix

## Problem

The database user doesn't have CREATE privileges on the `public` schema, causing table creation to fail with:

```
permission denied for schema public
```

## Solution

You need to grant CREATE privileges to your database user. The database is a **separate component** from your app in DigitalOcean.

### Step 1: Navigate to Your Database

1. In DigitalOcean Dashboard, go to **Databases** (not Apps)
2. Find your database (it should be named something like `dev-db-507728` or similar)
3. Click on it to open the database details page

### Step 2: Access Database Console

**Option A: Using DigitalOcean Console (Easiest)**

1. In the database details page, look for a **"Console"** tab or button
2. Click it to open an interactive SQL console
3. You should see a SQL prompt where you can run commands

**Option B: Using Connection Details + psql**

1. In the database details page, click **"Connection Details"** or **"Connection Pools"**
2. Copy the connection string (it will look like: `postgresql://doadmin:password@host:port/database?sslmode=require`)
3. Use this to connect via `psql` or any PostgreSQL client

**Option C: Using DigitalOcean CLI (doctl)**

```bash
# Install doctl if you haven't
# Then connect:
doctl databases connection <database-id> --format ConnectionString
```

### Step 3: Run SQL Commands

Once you're connected to the database console, run these commands:

```sql
-- First, identify your database username from DATABASE_URL
-- It's the part before the @ in: postgresql://USERNAME:password@host/db
-- Usually it's 'dbtradingbot' or 'doadmin'

-- Grant CREATE privilege on public schema
GRANT CREATE ON SCHEMA public TO dbtradingbot;

-- Grant default privileges for future tables
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO dbtradingbot;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO dbtradingbot;

-- Verify the grant worked
\dn+ public  -- List schema privileges
```

**Important:** Replace `dbtradingbot` with your actual database username from your `DATABASE_URL`.

### Step 4: Find Your Database Username

Your database username is in your `DATABASE_URL` environment variable. It's the part between `postgresql://` and `:`.

For example, if your DATABASE_URL is:
```
postgresql://dbtradingbot:password@host:port/database
```

Then your username is: `dbtradingbot`

### Step 5: Verify Tables Were Created

After granting permissions, restart your app. Then verify tables exist:

```sql
-- List all tables
\dt

-- Or query the information schema
SELECT table_name 
FROM information_schema.tables 
WHERE table_schema = 'public' 
ORDER BY table_name;
```

You should see:
- `candles`
- `trades`
- `positions`
- `system_events`
- `account_state`

## Alternative: Use a Different Schema

If you can't modify permissions on `public`, you can use a custom schema:

1. Create a new schema:
```sql
CREATE SCHEMA IF NOT EXISTS trading_app;
GRANT ALL ON SCHEMA trading_app TO dbtradingbot;
```

2. Update your `DATABASE_URL` to use this schema:
```
postgresql://user:pass@host/db?options=-csearch_path%3Dtrading_app
```

3. Restart your app - tables will be created in the new schema.

## Troubleshooting

**Can't find the database?**
- Check if the database was created as part of your App Platform app
- Look in DigitalOcean → Databases section (separate from Apps)

**Don't have superuser access?**
- Contact DigitalOcean support to grant permissions
- Or use the database owner account (usually `doadmin`)

**Still getting permission errors?**
- Make sure you're connected as a user with GRANT privileges (usually `doadmin` or `postgres`)
- Verify the username in GRANT command matches your DATABASE_URL username
