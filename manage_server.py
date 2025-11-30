#!/usr/bin/env python3
"""
Management server for Geotorget dashboard operations.

Provides a web UI to:
- View downloaded orders
- Configure PostGIS connection
- Publish orders to PostGIS
- Monitor database status

Usage:
    python manage_server.py
    python manage_server.py --port 5000
    python manage_server.py --db "postgresql://localhost/geotorget"
"""

import argparse
import os
import sys
from pathlib import Path

DEFAULT_DOWNLOADS_DIR = Path(__file__).parent / "downloads"


def main():
    parser = argparse.ArgumentParser(
        description="Geotorget management server with dashboard"
    )
    parser.add_argument(
        "-p", "--port",
        type=int,
        default=5050,
        help="Port to listen on (default: 5050)"
    )
    parser.add_argument(
        "-d", "--downloads",
        type=Path,
        default=DEFAULT_DOWNLOADS_DIR,
        help=f"Downloads directory (default: {DEFAULT_DOWNLOADS_DIR})"
    )
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="PostgreSQL connection string (or set GEOTORGET_DB env var)"
    )
    parser.add_argument(
        "--schema",
        type=str,
        default="geotorget",
        help="PostGIS schema name (default: geotorget)"
    )
    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Host to bind to (default: 127.0.0.1)"
    )

    args = parser.parse_args()

    # Get database connection
    db_connection = args.db or os.environ.get("GEOTORGET_DB")

    # Ensure downloads directory exists
    args.downloads.mkdir(parents=True, exist_ok=True)

    try:
        from src.lm_geotorget.management.server import run_management_server

        print("Geotorget Management Server")
        print("=" * 40)
        print(f"Dashboard:  http://{args.host}:{args.port}/")
        print(f"Downloads:  {args.downloads}")
        if db_connection:
            print(f"Database:   configured")
        else:
            print(f"Database:   not configured (set in dashboard)")
        print()
        print("Press Ctrl+C to stop")
        print()

        run_management_server(
            downloads_dir=args.downloads,
            db_connection=db_connection,
            schema=args.schema,
            host=args.host,
            port=args.port
        )

    except ImportError as e:
        print(f"Error: Missing dependency - {e}")
        print("Install with: pip install flask psycopg2-binary")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nShutting down...")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
