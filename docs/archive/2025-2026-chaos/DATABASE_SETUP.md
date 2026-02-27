# Database Setup for DigitalOcean

## Database Identifier
Your database ID: `e2db78ca-4d22-4203-822f-2e03ed2f08f7`

## Getting Connection Details

For DigitalOcean managed databases, you need to get the connection string from the DigitalOcean dashboard:

1. **Go to DigitalOcean Dashboard** → Databases → Your Database
2. **Click "Connection Details"** or "Connection String"
3. **Copy the connection string** (it will look like one of these formats):

### Format 1: Standard PostgreSQL Connection String
```
postgresql://username:password@host:port/database_name?sslmode=require
```

### Format 2: DigitalOcean Connection Pool (Recommended)
```
postgresql://username:password@host:port/database_name?sslmode=require&pool_size=10
```

## Setting Up Environment Variable

### On Your Server

1. **Create/Edit `.env` file**:
   ```bash
   cd ~/TradingSystem
   nano .env
   ```

2. **Add DATABASE_URL**:
   ```bash
   DATABASE_URL=postgresql://username:password@host:port/database_name?sslmode=require
   ```

3. **Secure the file**:
   ```bash
   chmod 600 .env
   ```

### Example Connection String

If your DigitalOcean database details are:
- **Host**: `db-postgresql-fra1-12345.db.ondigitalocean.com`
- **Port**: `25060`
- **Database**: `defaultdb`
- **Username**: `doadmin`
- **Password**: `your_password_here`

Then your `DATABASE_URL` would be:
```bash
DATABASE_URL=postgresql://doadmin:your_password_here@db-postgresql-fra1-12345.db.ondigitalocean.com:25060/defaultdb?sslmode=require
```

## Testing Connection

```bash
# Activate virtual environment
source venv/bin/activate

# Test database connection
python -c "
from src.storage.db import get_db
db = get_db()
with db.get_session() as session:
    result = session.execute('SELECT version();')
    print('PostgreSQL version:', result.fetchone()[0])
    print('✅ Database connection successful!')
"
```

## Initialize Database Tables

```bash
# Activate virtual environment
source venv/bin/activate

# Initialize tables (creates all required tables)
python -c "
from src.storage.db import get_db
db = get_db()
db.create_all()
print('✅ Database tables created!')
"
```

## Firewall Configuration

Make sure your DigitalOcean database firewall allows connections from your droplet:

1. **In DigitalOcean Dashboard** → Databases → Your Database → Settings
2. **Add your droplet's IP** to "Trusted Sources"
3. **Or allow all droplets** in the same region/data center

## SSL Connection

DigitalOcean managed databases require SSL. The connection string should include `?sslmode=require`.

If you get SSL errors, ensure:
- `sslmode=require` is in the connection string
- Your Python environment has `psycopg2` or `psycopg2-binary` installed:
  ```bash
  pip install psycopg2-binary
  ```

## Troubleshooting

### Connection Refused
- Check firewall settings in DigitalOcean dashboard
- Verify host, port, and database name
- Ensure droplet IP is in trusted sources

### Authentication Failed
- Double-check username and password
- Verify database name matches exactly
- Check if user has proper permissions

### SSL Required Error
- Add `?sslmode=require` to connection string
- Ensure `psycopg2-binary` is installed

### Database Not Found
- Verify database name in connection string
- Check if database exists in DigitalOcean dashboard

## Security Best Practices

1. **Never commit `.env` file** to git (should be in `.gitignore`)
2. **Use strong passwords** for database users
3. **Restrict firewall** to only your droplet IPs
4. **Use connection pooling** (already configured in code)
5. **Regular backups** via DigitalOcean dashboard

## Backup & Restore

### Backup
```bash
# From your droplet
pg_dump -h host -U username -d database_name > backup_$(date +%Y%m%d).sql
```

### Restore
```bash
# From your droplet
psql -h host -U username -d database_name < backup_YYYYMMDD.sql
```
