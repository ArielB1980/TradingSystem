# How to Get Admin Database Credentials

## The Problem

The `dbtradingbot` user cannot grant privileges to itself. You need **admin/superuser** credentials (usually `doadmin`) to grant permissions.

## Step-by-Step: Get Admin Connection String

### 1. Navigate to Your Database

1. Go to **DigitalOcean Dashboard**
2. Click **"Databases"** in the left sidebar (NOT "Apps")
3. Find and click on your database (it should be listed there)

### 2. Find Connection Details

1. In the database details page, look for:
   - **"Connection Details"** button/tab, OR
   - **"Connection Pools"** section, OR
   - **"Users & Databases"** tab

2. Look for **"Admin"** or **"Superuser"** connection string
   - It will look like: `postgresql://doadmin:XXXXX@host:port/database?sslmode=require`
   - The username will be `doadmin` (not `dbtradingbot`)

### 3. Alternative: Check Users Tab

1. In the database page, click **"Users & Databases"** tab
2. You should see:
   - `doadmin` (admin user)
   - `dbtradingbot` (your app user)
3. Click on `doadmin` to see its connection details

### 4. If You Can't Find Admin Credentials

**Option A: Reset Admin Password**
1. In database settings, look for "Reset Password" or "Users"
2. Reset the `doadmin` password
3. Use the new password in connection string

**Option B: Contact DigitalOcean Support**
- They can provide the admin connection string
- Or grant the permissions for you

## Once You Have Admin Connection String

### Method 1: Use Python Script

```bash
python scripts/grant_database_permissions.py "postgresql://doadmin:ADMIN_PASSWORD@host:port/dbtradingbot?sslmode=require"
```

### Method 2: Use Shell Script

```bash
chmod +x scripts/connect_and_grant_permissions.sh
./scripts/connect_and_grant_permissions.sh "postgresql://doadmin:ADMIN_PASSWORD@host:port/dbtradingbot?sslmode=require"
```

### Method 3: Use DigitalOcean Database Console

1. In the database page, look for **"Console"** or **"Query"** tab
2. Click it to open SQL console
3. Run:

```sql
GRANT CREATE ON SCHEMA public TO dbtradingbot;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO dbtradingbot;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO dbtradingbot;
```

### Method 4: Use psql from Your Computer

```bash
# Install psql if needed (macOS: brew install postgresql)
psql "postgresql://doadmin:ADMIN_PASSWORD@app-65e2763f-0c06-4d87-a349-ddc49db0abf3-do-user-31978256-0.l.db.ondigitalocean.com:25060/dbtradingbot?sslmode=require"

# Then run:
GRANT CREATE ON SCHEMA public TO dbtradingbot;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO dbtradingbot;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO dbtradingbot;
\q
```

## Verify Permissions Were Granted

After granting, verify:

```bash
python scripts/verify_database_permissions.py
```

Or test table creation:

```bash
python -c "
from src.storage.db import get_db
from sqlalchemy import inspect
db = get_db()
inspector = inspect(db.engine)
tables = inspector.get_table_names()
print('Tables:', tables)
"
```

You should see the tables list, not an empty list.
