# Database Permissions Fix

## Problem

The database user doesn't have CREATE privileges on the `public` schema, causing table creation to fail with:

```
permission denied for schema public
```

## Solution

You need to grant CREATE privileges to your database user. Connect to your PostgreSQL database as a superuser (or the database owner) and run:

### Option 1: Grant to Specific User (Recommended)

```sql
-- Replace 'dbtradingbot' with your actual database username
GRANT CREATE ON SCHEMA public TO dbtradingbot;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO dbtradingbot;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO dbtradingbot;
```

### Option 2: Grant to Public (Less Secure)

```sql
GRANT CREATE ON SCHEMA public TO PUBLIC;
```

### Option 3: Use a Different Schema

If you can't modify permissions, you can use a different schema by modifying your `DATABASE_URL`:

```
postgresql://user:pass@host/db?options=-csearch_path%3Dyour_schema
```

Then create the schema first:

```sql
CREATE SCHEMA IF NOT EXISTS your_schema;
GRANT ALL ON SCHEMA your_schema TO dbtradingbot;
```

## How to Connect

### DigitalOcean Managed Database

1. Go to DigitalOcean Dashboard → Databases → Your Database
2. Click "Connection Details" or use the "Console" feature
3. Connect as the database owner (usually `doadmin` or `postgres`)
4. Run the GRANT commands above

### Using psql Command Line

```bash
# Get connection string from DigitalOcean dashboard
psql "postgresql://doadmin:password@host:port/database?sslmode=require"

# Then run:
GRANT CREATE ON SCHEMA public TO dbtradingbot;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO dbtradingbot;
```

## Verify

After granting permissions, restart your app. The migration script should successfully create tables.

You can verify tables exist with:

```sql
\dt  -- List all tables
SELECT table_name FROM information_schema.tables WHERE table_schema = 'public';
```
