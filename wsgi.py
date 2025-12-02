"""WSGI entry point for production deployment."""
import os
from pathlib import Path
from src.lm_geotorget.management.server import create_management_app

# Read configuration from environment variables
downloads_dir = Path(os.environ.get('DOWNLOADS_DIR', '/opt/dtcc-geodb/downloads'))
db_connection = os.environ.get('DATABASE_URL')
schema = os.environ.get('DB_SCHEMA', 'geotorget')

# Create the Flask application
app = create_management_app(
    downloads_dir=downloads_dir,
    db_connection=db_connection,
    schema=schema
)
