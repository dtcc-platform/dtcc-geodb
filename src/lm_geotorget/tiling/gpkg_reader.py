"""
GeoPackage reader using Python stdlib sqlite3.

Reads GeoPackage files without requiring GDAL or other heavy dependencies.
"""

import sqlite3
from pathlib import Path
from typing import Iterator, Optional
from dataclasses import dataclass


def gpb_to_wkb(gpb: bytes) -> bytes:
    """
    Convert GeoPackage Binary (GPB) to Well-Known Binary (WKB).

    GPB format:
    - 2 bytes: Magic 'GP' (0x47, 0x50)
    - 1 byte: Version
    - 1 byte: Flags (envelope type in bits 1-3)
    - 4 bytes: SRS ID
    - Variable: Envelope (0/32/48/64 bytes based on flags)
    - Rest: WKB payload

    Args:
        gpb: GeoPackage Binary geometry bytes

    Returns:
        WKB geometry bytes
    """
    if len(gpb) < 8:
        raise ValueError(f"GPB too short: {len(gpb)} bytes")

    # Check magic bytes
    if gpb[0:2] != b'GP':
        # Not GPB format, might already be WKB - return as-is
        return gpb

    flags = gpb[3]
    envelope_type = (flags >> 1) & 0x07

    # Envelope sizes: 0=none, 1=xy(32), 2=xyz(48), 3=xym(48), 4=xyzm(64)
    envelope_sizes = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}
    envelope_size = envelope_sizes.get(envelope_type, 0)

    # WKB starts after header (8 bytes) + envelope
    wkb_offset = 8 + envelope_size
    return gpb[wkb_offset:]


@dataclass
class LayerInfo:
    """Information about a GeoPackage layer."""
    name: str
    geometry_column: str
    geometry_type: str
    srid: int
    feature_count: int
    columns: list[tuple[str, str]]  # (name, type) pairs


@dataclass
class Feature:
    """A single feature from a GeoPackage layer."""
    fid: int
    geometry: bytes  # WKB geometry
    properties: dict


class GeoPackageReader:
    """
    Read GeoPackage files using sqlite3.

    GeoPackage is an OGC standard that uses SQLite as a container format.
    This reader accesses the spatial data directly without GDAL.
    """

    def __init__(self, path: Path):
        """
        Initialize reader with a GeoPackage file path.

        Args:
            path: Path to the .gpkg file
        """
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"GeoPackage not found: {path}")

        self._conn: Optional[sqlite3.Connection] = None

    def __enter__(self):
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._conn:
            self._conn.close()
            self._conn = None

    def _get_conn(self) -> sqlite3.Connection:
        """Get connection, creating if needed."""
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.path))
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def list_layers(self) -> list[str]:
        """
        List all vector layers in the GeoPackage.

        Returns:
            List of layer (table) names
        """
        conn = self._get_conn()
        cursor = conn.execute("""
            SELECT table_name
            FROM gpkg_contents
            WHERE data_type = 'features'
            ORDER BY table_name
        """)
        return [row[0] for row in cursor.fetchall()]

    def get_layer_info(self, layer: str) -> LayerInfo:
        """
        Get detailed information about a layer.

        Args:
            layer: Layer name

        Returns:
            LayerInfo with geometry type, SRID, columns, etc.
        """
        conn = self._get_conn()

        # Get geometry column info
        cursor = conn.execute("""
            SELECT column_name, geometry_type_name, srs_id
            FROM gpkg_geometry_columns
            WHERE table_name = ?
        """, (layer,))
        row = cursor.fetchone()
        if not row:
            raise ValueError(f"Layer not found: {layer}")

        geom_column = row[0]
        geom_type = row[1]
        srid = row[2]

        # Get feature count
        cursor = conn.execute(f'SELECT COUNT(*) FROM "{layer}"')
        feature_count = cursor.fetchone()[0]

        # Get column info (exclude geometry column and fid which is auto-generated in PostGIS)
        cursor = conn.execute(f'PRAGMA table_info("{layer}")')
        columns = [(row[1], row[2]) for row in cursor.fetchall()
                   if row[1] != geom_column and row[1].lower() != 'fid']

        return LayerInfo(
            name=layer,
            geometry_column=geom_column,
            geometry_type=geom_type,
            srid=srid,
            feature_count=feature_count,
            columns=columns
        )

    def get_srid(self, layer: str) -> int:
        """
        Get the SRID (spatial reference ID) for a layer.

        Args:
            layer: Layer name

        Returns:
            SRID (e.g., 3006 for SWEREF99 TM)
        """
        conn = self._get_conn()
        cursor = conn.execute("""
            SELECT srs_id
            FROM gpkg_geometry_columns
            WHERE table_name = ?
        """, (layer,))
        row = cursor.fetchone()
        if not row:
            raise ValueError(f"Layer not found: {layer}")
        return row[0]

    def get_schema(self, layer: str) -> dict[str, str]:
        """
        Get the schema (column names and types) for a layer.

        Args:
            layer: Layer name

        Returns:
            Dict mapping column names to SQLite types
        """
        conn = self._get_conn()
        cursor = conn.execute(f'PRAGMA table_info("{layer}")')
        return {row[1]: row[2] for row in cursor.fetchall()}

    def read_layer(
        self,
        layer: str,
        limit: Optional[int] = None,
        offset: int = 0
    ) -> Iterator[Feature]:
        """
        Read features from a layer.

        Args:
            layer: Layer name
            limit: Maximum number of features to return
            offset: Number of features to skip

        Yields:
            Feature objects with fid, geometry (WKB), and properties
        """
        conn = self._get_conn()
        info = self.get_layer_info(layer)

        # Build column list (excluding geometry)
        prop_columns = [col[0] for col in info.columns]
        columns_sql = ", ".join(f'"{c}"' for c in prop_columns)

        # Build query
        query = f"""
            SELECT fid, "{info.geometry_column}", {columns_sql}
            FROM "{layer}"
        """
        if limit:
            query += f" LIMIT {limit}"
        if offset:
            query += f" OFFSET {offset}"

        cursor = conn.execute(query)

        for row in cursor:
            fid = row[0]
            gpb = row[1]  # GeoPackage Binary format
            geom = gpb_to_wkb(gpb) if gpb else None  # Convert to WKB
            props = {prop_columns[i]: row[i + 2] for i in range(len(prop_columns))}

            yield Feature(fid=fid, geometry=geom, properties=props)

    def get_extent(self, layer: str) -> tuple[float, float, float, float]:
        """
        Get the bounding box extent of a layer.

        Args:
            layer: Layer name

        Returns:
            Tuple of (min_x, min_y, max_x, max_y)
        """
        conn = self._get_conn()

        # Try gpkg_contents first (faster)
        cursor = conn.execute("""
            SELECT min_x, min_y, max_x, max_y
            FROM gpkg_contents
            WHERE table_name = ?
        """, (layer,))
        row = cursor.fetchone()
        if row and all(v is not None for v in row):
            return (row[0], row[1], row[2], row[3])

        # Fall back to computing from data
        info = self.get_layer_info(layer)
        cursor = conn.execute(f"""
            SELECT
                MIN(MbrMinX("{info.geometry_column}")),
                MIN(MbrMinY("{info.geometry_column}")),
                MAX(MbrMaxX("{info.geometry_column}")),
                MAX(MbrMaxY("{info.geometry_column}"))
            FROM "{layer}"
        """)
        row = cursor.fetchone()
        if row:
            return (row[0], row[1], row[2], row[3])

        raise ValueError(f"Could not determine extent for layer: {layer}")

    def read_layer_as_wkb_list(
        self,
        layer: str,
        batch_size: int = 1000
    ) -> Iterator[list[tuple[int, bytes, dict]]]:
        """
        Read layer in batches for efficient bulk loading.

        Args:
            layer: Layer name
            batch_size: Number of features per batch

        Yields:
            Lists of (fid, wkb_geometry, properties) tuples
        """
        batch = []
        for feature in self.read_layer(layer):
            batch.append((feature.fid, feature.geometry, feature.properties))
            if len(batch) >= batch_size:
                yield batch
                batch = []
        if batch:
            yield batch

    def close(self):
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None
