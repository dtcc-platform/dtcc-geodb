#!/bin/bash
# DTCC GeoDB Production Installation Script
# Run as root or with sudo

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}=== DTCC GeoDB Production Installation ===${NC}"

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}Please run as root or with sudo${NC}"
    exit 1
fi

# Configuration
APP_DIR="/opt/dtcc-geodb"
APP_USER="www-data"
DB_USER="dtcc_geodb"
DB_NAME="dtcc_geodb"

# Prompt for database password
read -sp "Enter PostgreSQL password for dtcc_geodb user: " DB_PASSWORD
echo

# Prompt for domain
read -p "Enter your domain name (or IP): " DOMAIN

echo -e "\n${YELLOW}Installing system dependencies...${NC}"
apt update
apt install -y python3 python3-pip python3-venv postgresql postgresql-contrib postgis nginx curl

echo -e "\n${YELLOW}Installing Martin tile server...${NC}"
if ! command -v martin &> /dev/null; then
    curl -LO https://github.com/maplibre/martin/releases/latest/download/martin-x86_64-unknown-linux-gnu.tar.gz
    tar -xzf martin-x86_64-unknown-linux-gnu.tar.gz
    mv martin /usr/local/bin/
    rm martin-x86_64-unknown-linux-gnu.tar.gz
    echo -e "${GREEN}Martin installed: $(martin --version)${NC}"
else
    echo -e "${GREEN}Martin already installed: $(martin --version)${NC}"
fi

echo -e "\n${YELLOW}Setting up PostgreSQL...${NC}"
sudo -u postgres psql -c "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE USER $DB_USER WITH PASSWORD '$DB_PASSWORD';"

sudo -u postgres psql -c "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE DATABASE $DB_NAME OWNER $DB_USER;"

sudo -u postgres psql -d $DB_NAME -c "CREATE EXTENSION IF NOT EXISTS postgis;"
sudo -u postgres psql -d $DB_NAME -c "CREATE SCHEMA IF NOT EXISTS geotorget AUTHORIZATION $DB_USER;"
sudo -u postgres psql -d $DB_NAME -c "GRANT ALL ON SCHEMA geotorget TO $DB_USER;"

echo -e "${GREEN}PostgreSQL configured${NC}"

echo -e "\n${YELLOW}Setting up application directory...${NC}"
mkdir -p $APP_DIR/downloads
mkdir -p /var/log/dtcc-geodb

# Copy application files (assuming script is run from project root)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

if [ -d "$PROJECT_DIR/src" ]; then
    cp -r "$PROJECT_DIR/src" $APP_DIR/
    cp -r "$PROJECT_DIR/martin.yaml" $APP_DIR/
    cp -r "$PROJECT_DIR/manage_server.py" $APP_DIR/
    echo -e "${GREEN}Application files copied${NC}"
else
    echo -e "${YELLOW}Source files not found. Please copy manually to $APP_DIR${NC}"
fi

echo -e "\n${YELLOW}Setting up Python environment...${NC}"
cd $APP_DIR
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install flask psycopg2-binary requests tqdm pyproj gunicorn

echo -e "\n${YELLOW}Creating environment file...${NC}"
cat > $APP_DIR/.env <<EOF
DATABASE_URL=postgresql://$DB_USER:$DB_PASSWORD@localhost/$DB_NAME
FLASK_ENV=production
DOWNLOADS_DIR=$APP_DIR/downloads
EOF

echo -e "\n${YELLOW}Setting permissions...${NC}"
chown -R $APP_USER:$APP_USER $APP_DIR
chmod 750 $APP_DIR
chmod 640 $APP_DIR/.env
chown -R $APP_USER:$APP_USER /var/log/dtcc-geodb

echo -e "\n${YELLOW}Installing systemd service...${NC}"
cp $SCRIPT_DIR/dtcc-geodb.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable dtcc-geodb

echo -e "\n${YELLOW}Configuring Nginx...${NC}"
cp $SCRIPT_DIR/nginx-dtcc-geodb.conf /etc/nginx/sites-available/dtcc-geodb
sed -i "s/your-domain.com/$DOMAIN/g" /etc/nginx/sites-available/dtcc-geodb
ln -sf /etc/nginx/sites-available/dtcc-geodb /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

echo -e "\n${YELLOW}Starting services...${NC}"
systemctl start dtcc-geodb
systemctl status dtcc-geodb --no-pager

echo -e "\n${GREEN}=== Installation Complete ===${NC}"
echo -e "Dashboard URL: http://$DOMAIN/"
echo -e "API Status:    http://$DOMAIN/api/db/status"
echo -e ""
echo -e "${YELLOW}Next steps:${NC}"
echo -e "1. Configure SSL: sudo certbot --nginx -d $DOMAIN"
echo -e "2. Configure firewall: sudo ufw allow 80,443/tcp"
echo -e "3. Check logs: sudo journalctl -u dtcc-geodb -f"
