#!/usr/bin/env python3
"""
Geodata API server for serving published PostGIS data.

Provides REST endpoints to query spatial data:
- GET /api/layers - List available layers
- GET /api/layers/{name}/features?bbox=... - Query features

Usage:
    python serve_api.py --db "postgresql://localhost/geotorget"
    python serve_api.py --db "postgresql://localhost/geotorget" --port 8000
"""

import argparse
import os
import sys
from pathlib import Path

DEFAULT_DOWNLOADS_DIR = Path(__file__).parent / "downloads"


def main():
    parser = argparse.ArgumentParser(
        description="Geotorget geodata API server"
    )
    parser.add_argument(
        "-p", "--port",
        type=int,
        default=8000,
        help="Port to listen on (default: 8000)"
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
        required=False,
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

    if not db_connection:
        print("Error: Database connection required.")
        print("Use --db or set GEOTORGET_DB environment variable")
        print()
        print("Example:")
        print('  python serve_api.py --db "postgresql://user:pass@localhost/geotorget"')
        sys.exit(1)

    try:
        from src.lm_geotorget.serving.api import run_server

        print("Geotorget Geodata API Server")
        print("=" * 40)
        print(f"API:        http://{args.host}:{args.port}/api/layers")
        print(f"Docs:       http://{args.host}:{args.port}/docs")
        print(f"Downloads:  {args.downloads}")
        print(f"Schema:     {args.schema}")
        print()
        print("Press Ctrl+C to stop")
        print()

        run_server(
            db_connection=db_connection,
            downloads_dir=args.downloads,
            schema=args.schema,
            host=args.host,
            port=args.port
        )

    except ImportError as e:
        print(f"Error: Missing dependency - {e}")
        print("Install with: pip install fastapi uvicorn psycopg2-binary")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nShutting down...")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
