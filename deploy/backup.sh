#!/bin/bash
# DTCC GeoDB Database Backup Script
# Add to crontab: 0 2 * * * /opt/dtcc-geodb/deploy/backup.sh

set -e

# Configuration
BACKUP_DIR="/var/backups/dtcc-geodb"
DB_NAME="dtcc_geodb"
DB_USER="dtcc_geodb"
RETENTION_DAYS=30

# Create backup directory
mkdir -p $BACKUP_DIR

# Generate filename with timestamp
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/${DB_NAME}_${TIMESTAMP}.sql.gz"

# Perform backup
echo "Starting backup: $BACKUP_FILE"
pg_dump -U $DB_USER -h localhost $DB_NAME | gzip > $BACKUP_FILE

# Check backup size
SIZE=$(du -h $BACKUP_FILE | cut -f1)
echo "Backup completed: $SIZE"

# Remove old backups
echo "Removing backups older than $RETENTION_DAYS days..."
find $BACKUP_DIR -name "*.sql.gz" -mtime +$RETENTION_DAYS -delete

# List current backups
echo "Current backups:"
ls -lh $BACKUP_DIR/*.sql.gz 2>/dev/null || echo "No backups found"

echo "Backup complete!"
