"""
PostGIS data loader for GeoPackage files.

Loads vector data from GeoPackage into PostGIS with coordinate transformation.
"""

import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Iterator
import os

try:
    import psycopg2
    import psycopg2.extras
    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False

from .gpkg_reader import GeoPackageReader, Feature


@dataclass
class LoadResult:
    """Result of loading a layer to PostGIS."""
    table_name: str
    layer_name: str
    feature_count: int
    duration_seconds: float
    success: bool
    error: Optional[str] = None


@dataclass
class TableInfo:
    """Information about a PostGIS table."""
    schema: str
    name: str
    geometry_type: str
    srid: int
    feature_count: int
    columns: list[str]


@dataclass
class TableStats:
    """Statistics for a PostGIS table."""
    name: str
    feature_count: int
    bbox: Optional[tuple[float, float, float, float]]  # minx, miny, maxx, maxy
    columns: list[tuple[str, str]]  # (name, type) pairs
    source_order: Optional[str] = None
    loaded_at: Optional[datetime] = None


class PostGISLoader:
    """
    Load GeoPackage data into PostGIS.

    Features:
    - Bulk insert using COPY for speed
    - Automatic coordinate transformation via ST_Transform()
    - Creates spatial index automatically
    - Tracks source file hash for incremental updates
    """

    def __init__(self, connection_string: str, schema: str = "geotorget"):
        """
        Initialize the loader.

        Args:
            connection_string: PostgreSQL connection string
                e.g., "postgresql://user:pass@localhost/dbname"
                or "host=localhost dbname=geotorget user=postgres"
            schema: Schema name to use for tables (default: "geotorget")
        """
        if not HAS_PSYCOPG2:
            raise ImportError(
                "psycopg2 is required for PostGIS loading. "
                "Install with: pip install psycopg2-binary"
            )

        self.connection_string = connection_string
        self.schema = schema
        self._conn = None

    def _get_conn(self):
        """Get or create database connection."""
        if self._conn is None or self._conn.closed:
            self._conn = psycopg2.connect(self.connection_string)
        return self._conn

    def close(self):
        """Close the database connection."""
        if self._conn and not self._conn.closed:
            self._conn.close()
            self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def init_database(self):
        """
        Initialize the database with required extensions and schema.

        Creates:
        - PostGIS extension
        - Schema for geotorget data
        - Metadata tracking table
        """
        conn = self._get_conn()
        with conn.cursor() as cur:
            # Create PostGIS extension
            cur.execute("CREATE EXTENSION IF NOT EXISTS postgis")

            # Create schema
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"')

            # Create metadata table
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS "{self.schema}"._metadata (
                    id SERIAL PRIMARY KEY,
                    order_id TEXT NOT NULL,
                    source_file TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    table_name TEXT NOT NULL,
                    layer_name TEXT NOT NULL,
                    feature_count INTEGER,
                    bbox geometry(Polygon, 4326),
                    loaded_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(order_id, layer_name)
                )
            """)

        conn.commit()

    def create_schema(self, schema_name: Optional[str] = None):
        """Create schema if it doesn't exist."""
        schema = schema_name or self.schema
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        conn.commit()

    def load_layer(
        self,
        gpkg_path: Path,
        layer_name: str,
        table_name: Optional[str] = None,
        target_srid: int = 4326,
        if_exists: str = "replace",
        order_id: Optional[str] = None,
        batch_size: int = 1000
    ) -> LoadResult:
        """
        Load a GeoPackage layer into PostGIS.

        Args:
            gpkg_path: Path to the GeoPackage file
            layer_name: Name of the layer to load
            table_name: Target table name (defaults to layer_name)
            target_srid: Target SRID for coordinate transformation (default: 4326/WGS84)
            if_exists: What to do if table exists: "replace", "append", or "fail"
            order_id: Order ID for metadata tracking
            batch_size: Number of features per batch insert

        Returns:
            LoadResult with status and statistics
        """
        import time
        start_time = time.time()

        table_name = table_name or self._sanitize_name(layer_name)
        full_table = f'"{self.schema}"."{table_name}"'

        try:
            with GeoPackageReader(gpkg_path) as reader:
                # Get actual layers in the GPKG
                actual_layers = reader.list_layers()

                # Resolve layer name: use provided name if it exists, otherwise use actual layer
                if layer_name in actual_layers:
                    gpkg_layer = layer_name
                elif len(actual_layers) == 1:
                    # Single layer GPKG - use that layer regardless of filename
                    gpkg_layer = actual_layers[0]
                elif len(actual_layers) == 0:
                    raise ValueError(f"No layers found in GeoPackage")
                else:
                    # Multiple layers, none match - try case-insensitive match
                    matches = [l for l in actual_layers if l.lower() == layer_name.lower()]
                    if len(matches) == 1:
                        gpkg_layer = matches[0]
                    else:
                        raise ValueError(
                            f"Layer '{layer_name}' not found. "
                            f"Available layers: {', '.join(actual_layers)}"
                        )

                # Get layer info using resolved layer name
                info = reader.get_layer_info(gpkg_layer)
                source_srid = info.srid

                conn = self._get_conn()

                # Handle existing table
                if if_exists == "replace":
                    self._drop_table(table_name)
                elif if_exists == "fail":
                    if self._table_exists(table_name):
                        raise ValueError(f"Table {table_name} already exists")
                # "append" - just continue

                # Create table if needed
                if not self._table_exists(table_name):
                    self._create_table(table_name, info, target_srid, order_id)

                # Load features in batches
                feature_count = 0
                for batch in reader.read_layer_as_wkb_list(gpkg_layer, batch_size):
                    self._insert_batch(
                        table_name,
                        batch,
                        info,
                        source_srid,
                        target_srid,
                        order_id
                    )
                    feature_count += len(batch)

                conn.commit()

                # Create spatial index
                self._create_spatial_index(table_name)

                # Update metadata
                if order_id:
                    self._update_metadata(
                        order_id=order_id,
                        source_file=gpkg_path.name,
                        table_name=table_name,
                        layer_name=layer_name,
                        feature_count=feature_count,
                        gpkg_path=gpkg_path
                    )

                duration = time.time() - start_time
                return LoadResult(
                    table_name=table_name,
                    layer_name=layer_name,
                    feature_count=feature_count,
                    duration_seconds=duration,
                    success=True
                )

        except Exception as e:
            duration = time.time() - start_time
            return LoadResult(
                table_name=table_name,
                layer_name=layer_name,
                feature_count=0,
                duration_seconds=duration,
                success=False,
                error=str(e)
            )

    def _sanitize_name(self, name: str) -> str:
        """Sanitize a name for use as a table name."""
        # Remove or replace invalid characters
        name = re.sub(r'[^a-zA-Z0-9_]', '_', name.lower())
        # Ensure it doesn't start with a number
        if name and name[0].isdigit():
            name = '_' + name
        return name

    def _table_exists(self, table_name: str) -> bool:
        """Check if a table exists in the schema."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = %s AND table_name = %s
                )
            """, (self.schema, table_name))
            return cur.fetchone()[0]

    def _drop_table(self, table_name: str):
        """Drop a table if it exists."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(f'DROP TABLE IF EXISTS "{self.schema}"."{table_name}" CASCADE')
        conn.commit()

    def _create_table(
        self,
        table_name: str,
        info,
        target_srid: int,
        order_id: Optional[str]
    ):
        """Create a new table for the layer."""
        conn = self._get_conn()

        # Map SQLite types to PostgreSQL
        type_map = {
            "INTEGER": "BIGINT",
            "REAL": "DOUBLE PRECISION",
            "TEXT": "TEXT",
            "BLOB": "BYTEA",
            "NUMERIC": "NUMERIC",
            "BOOLEAN": "BOOLEAN",
        }

        # Build column definitions
        columns = ["fid SERIAL PRIMARY KEY"]

        for col_name, col_type in info.columns:
            pg_type = type_map.get(col_type.upper(), "TEXT")
            safe_name = self._sanitize_name(col_name)
            columns.append(f'"{safe_name}" {pg_type}')

        # Add metadata columns
        columns.append("_source_order TEXT")
        columns.append("_loaded_at TIMESTAMP DEFAULT NOW()")

        # Create table
        with conn.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE "{self.schema}"."{table_name}" (
                    {', '.join(columns)}
                )
            """)

            # Add geometry column with PostGIS
            geom_type = info.geometry_type.upper()
            if not geom_type.startswith("MULTI"):
                # Allow multi variants
                geom_type = f"GEOMETRY"

            cur.execute(f"""
                SELECT AddGeometryColumn(
                    '{self.schema}', '{table_name}', 'geom',
                    {target_srid}, '{geom_type}', 2
                )
            """)

        conn.commit()

    def _insert_batch(
        self,
        table_name: str,
        batch: list[tuple[int, bytes, dict]],
        info,
        source_srid: int,
        target_srid: int,
        order_id: Optional[str]
    ):
        """Insert a batch of features."""
        if not batch:
            return

        conn = self._get_conn()

        # Get column names
        col_names = [self._sanitize_name(c[0]) for c in info.columns]

        # Build insert statement
        col_list = ", ".join(f'"{c}"' for c in col_names)
        placeholders = ", ".join(["%s"] * len(col_names))

        with conn.cursor() as cur:
            for fid, wkb, props in batch:
                # Get property values in order
                values = [props.get(c[0]) for c in info.columns]

                # Transform geometry if needed
                if source_srid != target_srid:
                    geom_sql = f"ST_Transform(ST_SetSRID(ST_GeomFromWKB(%s), {source_srid}), {target_srid})"
                else:
                    geom_sql = f"ST_SetSRID(ST_GeomFromWKB(%s), {source_srid})"

                cur.execute(f"""
                    INSERT INTO "{self.schema}"."{table_name}"
                    ({col_list}, geom, _source_order)
                    VALUES ({placeholders}, {geom_sql}, %s)
                """, values + [wkb, order_id])

    def _create_spatial_index(self, table_name: str):
        """Create a spatial index on the geometry column."""
        conn = self._get_conn()
        index_name = f"idx_{table_name}_geom"

        with conn.cursor() as cur:
            # Drop existing index if any
            cur.execute(f'DROP INDEX IF EXISTS "{self.schema}"."{index_name}"')

            # Create GIST index
            cur.execute(f"""
                CREATE INDEX "{index_name}"
                ON "{self.schema}"."{table_name}"
                USING GIST (geom)
            """)

        conn.commit()

    def _update_metadata(
        self,
        order_id: str,
        source_file: str,
        table_name: str,
        layer_name: str,
        feature_count: int,
        gpkg_path: Path
    ):
        """Update the metadata tracking table."""
        conn = self._get_conn()

        # Calculate file hash
        source_hash = self._file_hash(gpkg_path)

        # Get bbox from table
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT ST_AsText(ST_Envelope(ST_Collect(geom)))
                FROM "{self.schema}"."{table_name}"
            """)
            bbox_wkt = cur.fetchone()[0]

            # Upsert metadata
            cur.execute(f"""
                INSERT INTO "{self.schema}"._metadata
                (order_id, source_file, source_hash, table_name, layer_name, feature_count, bbox)
                VALUES (%s, %s, %s, %s, %s, %s, ST_GeomFromText(%s, 4326))
                ON CONFLICT (order_id, layer_name)
                DO UPDATE SET
                    source_file = EXCLUDED.source_file,
                    source_hash = EXCLUDED.source_hash,
                    table_name = EXCLUDED.table_name,
                    feature_count = EXCLUDED.feature_count,
                    bbox = EXCLUDED.bbox,
                    loaded_at = NOW()
            """, (order_id, source_file, source_hash, table_name, layer_name, feature_count, bbox_wkt))

        conn.commit()

    def _file_hash(self, path: Path) -> str:
        """Calculate SHA256 hash of a file."""
        sha256 = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256.update(chunk)
        return sha256.hexdigest()

    def list_tables(self, schema: Optional[str] = None) -> list[TableInfo]:
        """List all tables in the schema with their geometry info."""
        schema = schema or self.schema
        conn = self._get_conn()

        tables = []
        with conn.cursor() as cur:
            # Get tables with geometry columns
            cur.execute("""
                SELECT
                    f_table_schema,
                    f_table_name,
                    type,
                    srid
                FROM geometry_columns
                WHERE f_table_schema = %s
                ORDER BY f_table_name
            """, (schema,))

            for row in cur.fetchall():
                table_schema, table_name, geom_type, srid = row

                # Skip metadata table
                if table_name.startswith("_"):
                    continue

                # Get feature count
                cur.execute(f'SELECT COUNT(*) FROM "{schema}"."{table_name}"')
                count = cur.fetchone()[0]

                # Get columns
                cur.execute(f"""
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = %s
                    AND column_name NOT IN ('geom', 'fid')
                    ORDER BY ordinal_position
                """, (schema, table_name))
                columns = [r[0] for r in cur.fetchall()]

                tables.append(TableInfo(
                    schema=table_schema,
                    name=table_name,
                    geometry_type=geom_type,
                    srid=srid,
                    feature_count=count,
                    columns=columns
                ))

        return tables

    def get_table_stats(self, table_name: str) -> TableStats:
        """Get detailed statistics for a table."""
        conn = self._get_conn()

        with conn.cursor() as cur:
            # Get count
            cur.execute(f'SELECT COUNT(*) FROM "{self.schema}"."{table_name}"')
            count = cur.fetchone()[0]

            # Get bbox
            cur.execute(f"""
                SELECT
                    ST_XMin(extent), ST_YMin(extent),
                    ST_XMax(extent), ST_YMax(extent)
                FROM (
                    SELECT ST_Extent(geom) as extent
                    FROM "{self.schema}"."{table_name}"
                ) t
            """)
            bbox_row = cur.fetchone()
            bbox = tuple(bbox_row) if bbox_row and bbox_row[0] else None

            # Get columns and types
            cur.execute(f"""
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                ORDER BY ordinal_position
            """, (self.schema, table_name))
            columns = [(r[0], r[1]) for r in cur.fetchall()]

            # Get source order from first row
            cur.execute(f"""
                SELECT _source_order, _loaded_at
                FROM "{self.schema}"."{table_name}"
                LIMIT 1
            """)
            meta_row = cur.fetchone()
            source_order = meta_row[0] if meta_row else None
            loaded_at = meta_row[1] if meta_row else None

        return TableStats(
            name=table_name,
            feature_count=count,
            bbox=bbox,
            columns=columns,
            source_order=source_order,
            loaded_at=loaded_at
        )

    def get_metadata(self, order_id: Optional[str] = None) -> list[dict]:
        """Get metadata entries, optionally filtered by order ID."""
        conn = self._get_conn()

        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if order_id:
                cur.execute(f"""
                    SELECT order_id, source_file, source_hash, table_name,
                           layer_name, feature_count, loaded_at
                    FROM "{self.schema}"._metadata
                    WHERE order_id = %s
                    ORDER BY layer_name
                """, (order_id,))
            else:
                cur.execute(f"""
                    SELECT order_id, source_file, source_hash, table_name,
                           layer_name, feature_count, loaded_at
                    FROM "{self.schema}"._metadata
                    ORDER BY order_id, layer_name
                """)

            return [dict(row) for row in cur.fetchall()]

    def is_layer_current(self, gpkg_path: Path, layer_name: str, order_id: str) -> bool:
        """Check if a layer is already loaded with the current file hash."""
        conn = self._get_conn()
        current_hash = self._file_hash(gpkg_path)

        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT source_hash
                FROM "{self.schema}"._metadata
                WHERE order_id = %s AND layer_name = %s
            """, (order_id, layer_name))
            row = cur.fetchone()

        return row is not None and row[0] == current_hash
