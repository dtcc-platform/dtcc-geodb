# Production Deployment Guide

This guide covers deploying DTCC GeoDB Dashboard on a Linux server.

## Prerequisites

- Ubuntu 20.04+ or Debian 11+
- PostgreSQL 13+ with PostGIS
- Python 3.9+
- Root/sudo access

## 1. System Dependencies

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Python and PostgreSQL
sudo apt install -y python3 python3-pip python3-venv postgresql postgresql-contrib postgis

# Install Martin tile server
curl -LO https://github.com/maplibre/martin/releases/latest/download/martin-x86_64-unknown-linux-gnu.tar.gz
tar -xzf martin-x86_64-unknown-linux-gnu.tar.gz
sudo mv martin /usr/local/bin/
rm martin-x86_64-unknown-linux-gnu.tar.gz

# Verify Martin installation
martin --version
```

## 2. PostgreSQL Setup

```bash
# Create database user and database
sudo -u postgres psql <<EOF
CREATE USER dtcc_geodb WITH PASSWORD 'CHANGE_THIS_PASSWORD';
CREATE DATABASE dtcc_geodb OWNER dtcc_geodb;
\c dtcc_geodb
CREATE EXTENSION postgis;
CREATE SCHEMA geotorget AUTHORIZATION dtcc_geodb;
GRANT ALL ON SCHEMA geotorget TO dtcc_geodb;
EOF
```

## 3. Application Setup

```bash
# Create application directory
sudo mkdir -p /opt/dtcc-geodb
sudo chown $USER:$USER /opt/dtcc-geodb

# Clone or copy your project
cd /opt/dtcc-geodb
# git clone <your-repo> .
# OR copy files manually

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install --upgrade pip
pip install flask psycopg2-binary requests tqdm pyproj gunicorn
```

## 4. Environment Configuration

```bash
# Create environment file (edit with your values)
cat > /opt/dtcc-geodb/.env <<EOF
DATABASE_URL=postgresql://dtcc_geodb:CHANGE_THIS_PASSWORD@localhost/dtcc_geodb
FLASK_ENV=production
DOWNLOADS_DIR=/opt/dtcc-geodb/downloads
EOF

# Create downloads directory
mkdir -p /opt/dtcc-geodb/downloads

# Set permissions
sudo chown -R www-data:www-data /opt/dtcc-geodb
sudo chmod 750 /opt/dtcc-geodb
sudo chmod 640 /opt/dtcc-geodb/.env
```

## 5. Systemd Service

Copy the service file:

```bash
sudo cp /opt/dtcc-geodb/deploy/dtcc-geodb.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable dtcc-geodb
sudo systemctl start dtcc-geodb
```

Check status:

```bash
sudo systemctl status dtcc-geodb
sudo journalctl -u dtcc-geodb -f
```

## 6. Nginx Reverse Proxy

Install and configure Nginx:

```bash
sudo apt install -y nginx

# Copy configuration
sudo cp /opt/dtcc-geodb/deploy/nginx-dtcc-geodb.conf /etc/nginx/sites-available/dtcc-geodb

# Edit the server_name in the config file
sudo nano /etc/nginx/sites-available/dtcc-geodb

# Enable site
sudo ln -s /etc/nginx/sites-available/dtcc-geodb /etc/nginx/sites-enabled/

# Remove default site (optional)
sudo rm /etc/nginx/sites-enabled/default

# Test and reload
sudo nginx -t
sudo systemctl reload nginx
```

## 7. SSL with Let's Encrypt (Recommended)

```bash
# Install certbot
sudo apt install -y certbot python3-certbot-nginx

# Get certificate (replace with your domain)
sudo certbot --nginx -d your-domain.com

# Auto-renewal is configured automatically
sudo systemctl status certbot.timer
```

## 8. Firewall Configuration

```bash
sudo ufw allow 22/tcp    # SSH
sudo ufw allow 80/tcp    # HTTP
sudo ufw allow 443/tcp   # HTTPS
sudo ufw enable
```

## 9. Verify Deployment

```bash
# Check service status
sudo systemctl status dtcc-geodb

# Check logs
sudo journalctl -u dtcc-geodb -n 50

# Test API locally
curl http://localhost:5050/api/db/status

# Test via nginx
curl http://your-domain.com/api/db/status
```

## Maintenance

### View Logs

```bash
# Application logs
sudo journalctl -u dtcc-geodb -f

# Nginx access logs
sudo tail -f /var/log/nginx/access.log

# Nginx error logs
sudo tail -f /var/log/nginx/error.log
```

### Restart Services

```bash
sudo systemctl restart dtcc-geodb
sudo systemctl restart nginx
```

### Update Application

```bash
cd /opt/dtcc-geodb
sudo systemctl stop dtcc-geodb

# Pull updates
git pull

# Update dependencies if needed
source venv/bin/activate
pip install -r requirements.txt

sudo systemctl start dtcc-geodb
```

### Database Backup

```bash
# Manual backup
pg_dump -U dtcc-geodb -h localhost dtcc-geodb > backup_$(date +%Y%m%d).sql

# Restore
psql -U dtcc-geodb -h localhost dtcc-geodb < backup_20241201.sql
```

## Production Considerations

| Item | Recommendation |
|------|----------------|
| Workers | Adjust gunicorn workers based on CPU cores (2-4 per core) |
| Memory | Monitor with `htop`, ensure adequate RAM for PostGIS |
| Disk | Store downloads on separate volume if large |
| Monitoring | Consider Prometheus + Grafana for metrics |
| Backups | Schedule automated PostgreSQL backups |
| Updates | Keep system and dependencies updated |

## Troubleshooting

### Service won't start

```bash
# Check logs for errors
sudo journalctl -u dtcc-geodb -n 100 --no-pager

# Check permissions
ls -la /opt/dtcc-geodb/
```

### Database connection issues

```bash
# Test PostgreSQL connection
psql -U dtcc-geodb -h localhost -d dtcc-geodb -c "SELECT 1;"

# Check PostgreSQL is running
sudo systemctl status postgresql
```

### Martin not starting

```bash
# Check if Martin is installed
which martin

# Test Martin manually
DATABASE_URL="postgresql://dtcc-geodb:password@localhost/dtcc-geodb" martin --config /opt/dtcc-geodb/martin.yaml
```

### Nginx 502 Bad Gateway

```bash
# Check if gunicorn is running
sudo systemctl status dtcc-geodb

# Check socket/port binding
ss -tlnp | grep 5050
```
