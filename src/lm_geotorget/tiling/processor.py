"""
Data processing pipeline for Lantmateriet Geotorget orders.

Orchestrates: detect type -> unzip -> read GeoPackage -> load to PostGIS
"""

import json
import os
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable

from .detector import DataType, DetectedOrder, detect_order_type, is_publishable
from .gpkg_reader import GeoPackageReader
from .postgis_loader import PostGISLoader, LoadResult


@dataclass
class ProcessResult:
    """Result of processing an order."""
    order_id: str
    data_type: DataType
    layers_processed: list[LoadResult] = field(default_factory=list)
    total_features: int = 0
    duration_seconds: float = 0.0
    success: bool = True
    error: Optional[str] = None


@dataclass
class ProcessingStatus:
    """Current processing status."""
    orders_processed: int = 0
    orders_failed: int = 0
    total_features: int = 0
    total_tables: int = 0
    last_processed: Optional[datetime] = None


class DataProcessor:
    """
    Process Geotorget orders and load them into PostGIS.

    Features:
    - Auto-detect data type from order contents
    - Extract GeoPackages from ZIPs
    - Load to PostGIS with coordinate transformation
    - Track processing status and incremental updates
    """

    def __init__(
        self,
        downloads_dir: Path,
        db_connection: str,
        schema: str = "geotorget",
        target_srid: int = 4326
    ):
        """
        Initialize the processor.

        Args:
            downloads_dir: Directory containing downloaded orders
            db_connection: PostgreSQL connection string
            schema: Schema name for PostGIS tables
            target_srid: Target SRID for coordinate transformation
        """
        self.downloads_dir = Path(downloads_dir)
        self.db_connection = db_connection
        self.schema = schema
        self.target_srid = target_srid
        self._loader: Optional[PostGISLoader] = None

    def _get_loader(self) -> PostGISLoader:
        """Get or create PostGIS loader."""
        if self._loader is None:
            self._loader = PostGISLoader(self.db_connection, self.schema)
        return self._loader

    def init_database(self):
        """Initialize the database with required schema and tables."""
        loader = self._get_loader()
        loader.init_database()

    def process_order(
        self,
        order_id: str,
        layers: Optional[list[str]] = None,
        progress_callback: Optional[Callable[[str, int, int], None]] = None
    ) -> ProcessResult:
        """
        Process a single order.

        Args:
            order_id: The order ID (directory name)
            layers: Optional list of specific layers to process (None = all)
            progress_callback: Optional callback(layer_name, current, total)

        Returns:
            ProcessResult with status and statistics
        """
        import time
        start_time = time.time()

        order_dir = self.downloads_dir / order_id
        if not order_dir.exists():
            return ProcessResult(
                order_id=order_id,
                data_type=DataType.UNKNOWN,
                success=False,
                error=f"Order directory not found: {order_dir}"
            )

        # Detect data type
        detected = detect_order_type(order_dir)

        if not is_publishable(detected.data_type):
            return ProcessResult(
                order_id=order_id,
                data_type=detected.data_type,
                success=False,
                error=f"Data type {detected.data_type.value} is not yet supported for publishing"
            )

        # Get loader
        loader = self._get_loader()

        # Find GeoPackage files to process
        gpkg_files = self._find_gpkg_files(detected, layers)

        layer_results: list[LoadResult] = []
        total_features = 0
        layers_to_process = []

        # First pass: discover all actual layers in all GPKG files
        for zip_name, inner_path, file_stem in gpkg_files:
            try:
                zip_path = order_dir / zip_name
                with self._extract_gpkg(zip_path, inner_path) as gpkg_path:
                    with GeoPackageReader(gpkg_path) as reader:
                        actual_layers = reader.list_layers()
                        for actual_layer in actual_layers:
                            # Filter by requested layers if specified
                            if layers and actual_layer not in layers:
                                continue
                            layers_to_process.append((zip_name, inner_path, actual_layer))
            except Exception as e:
                layer_results.append(LoadResult(
                    table_name=file_stem,
                    layer_name=file_stem,
                    feature_count=0,
                    duration_seconds=0,
                    success=False,
                    error=f"Failed to read GPKG: {e}"
                ))

        total_layers = len(layers_to_process)

        # Second pass: load each layer
        for i, (zip_name, inner_path, layer_name) in enumerate(layers_to_process):
            if progress_callback:
                progress_callback(layer_name, i + 1, total_layers)

            try:
                zip_path = order_dir / zip_name
                with self._extract_gpkg(zip_path, inner_path) as gpkg_path:
                    # Check if already current
                    if loader.is_layer_current(gpkg_path, layer_name, order_id):
                        # Skip - already loaded with same hash
                        continue

                    # Load to PostGIS
                    result = loader.load_layer(
                        gpkg_path=gpkg_path,
                        layer_name=layer_name,
                        target_srid=self.target_srid,
                        if_exists="replace",
                        order_id=order_id
                    )
                    layer_results.append(result)

                    if result.success:
                        total_features += result.feature_count

            except Exception as e:
                layer_results.append(LoadResult(
                    table_name=layer_name,
                    layer_name=layer_name,
                    feature_count=0,
                    duration_seconds=0,
                    success=False,
                    error=str(e)
                ))

        duration = time.time() - start_time
        all_success = all(r.success for r in layer_results) if layer_results else True

        return ProcessResult(
            order_id=order_id,
            data_type=detected.data_type,
            layers_processed=layer_results,
            total_features=total_features,
            duration_seconds=duration,
            success=all_success,
            error=None if all_success else "Some layers failed to load"
        )

    def _find_gpkg_files(
        self,
        detected: DetectedOrder,
        layers: Optional[list[str]]
    ) -> list[tuple[str, str, str]]:
        """
        Find GeoPackage files to process.

        Returns:
            List of (zip_name, inner_path, layer_name) tuples
        """
        gpkg_files = []

        for f in detected.files:
            if f.extension != ".gpkg":
                continue

            # Extract layer name from filename
            layer_name = Path(f.inner_path).stem
            for suffix in ["_sverige", "_sweden"]:
                if layer_name.endswith(suffix):
                    layer_name = layer_name[:-len(suffix)]
                    break

            # Filter by requested layers
            if layers and layer_name not in layers:
                continue

            gpkg_files.append((f.zip_name, f.inner_path, layer_name))

        return gpkg_files

    def _extract_gpkg(self, zip_path: Path, inner_path: str):
        """
        Extract a GeoPackage file from a ZIP to a temp file.

        Returns a context manager that yields the temp file path
        and cleans up afterwards.
        """
        class TempGpkg:
            def __init__(self, zip_path: Path, inner_path: str):
                self.zip_path = zip_path
                self.inner_path = inner_path
                self.temp_file = None

            def __enter__(self) -> Path:
                # Create temp file
                fd, path = tempfile.mkstemp(suffix=".gpkg")
                self.temp_file = Path(path)
                os.close(fd)

                # Extract to temp file
                with zipfile.ZipFile(self.zip_path, "r") as zf:
                    with zf.open(self.inner_path) as src:
                        with open(self.temp_file, "wb") as dst:
                            dst.write(src.read())

                return self.temp_file

            def __exit__(self, exc_type, exc_val, exc_tb):
                if self.temp_file and self.temp_file.exists():
                    self.temp_file.unlink()

        return TempGpkg(zip_path, inner_path)

    def process_all(
        self,
        progress_callback: Optional[Callable[[str, int, int], None]] = None
    ) -> list[ProcessResult]:
        """
        Process all orders in the downloads directory.

        Args:
            progress_callback: Optional callback(order_id, current, total)

        Returns:
            List of ProcessResult for each order
        """
        results = []

        # Find all order directories
        order_dirs = [
            d for d in self.downloads_dir.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        ]

        total = len(order_dirs)
        for i, order_dir in enumerate(order_dirs):
            if progress_callback:
                progress_callback(order_dir.name, i + 1, total)

            result = self.process_order(order_dir.name)
            results.append(result)

        return results

    def process_incremental(
        self,
        progress_callback: Optional[Callable[[str, int, int], None]] = None
    ) -> list[ProcessResult]:
        """
        Process only orders that have changed since last processing.

        Compares file hashes to skip already-processed orders.

        Args:
            progress_callback: Optional callback(order_id, current, total)

        Returns:
            List of ProcessResult for processed orders
        """
        results = []
        loader = self._get_loader()

        # Get existing metadata
        existing_metadata = {
            (m["order_id"], m["layer_name"]): m["source_hash"]
            for m in loader.get_metadata()
        }

        # Find order directories that may need processing
        order_dirs = [
            d for d in self.downloads_dir.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        ]

        orders_to_process = []
        for order_dir in order_dirs:
            detected = detect_order_type(order_dir)
            if not is_publishable(detected.data_type):
                continue

            # Check if any layer needs updating
            needs_update = False
            for f in detected.files:
                if f.extension == ".gpkg":
                    layer_name = Path(f.inner_path).stem
                    for suffix in ["_sverige", "_sweden"]:
                        if layer_name.endswith(suffix):
                            layer_name = layer_name[:-len(suffix)]
                            break

                    key = (order_dir.name, layer_name)
                    if key not in existing_metadata:
                        needs_update = True
                        break

            if needs_update:
                orders_to_process.append(order_dir)

        total = len(orders_to_process)
        for i, order_dir in enumerate(orders_to_process):
            if progress_callback:
                progress_callback(order_dir.name, i + 1, total)

            result = self.process_order(order_dir.name)
            results.append(result)

        return results

    def get_status(self) -> ProcessingStatus:
        """Get current processing status."""
        loader = self._get_loader()
        metadata = loader.get_metadata()
        tables = loader.list_tables()

        # Count unique orders
        order_ids = set(m["order_id"] for m in metadata)

        # Get latest timestamp
        latest = None
        for m in metadata:
            if m["loaded_at"] and (latest is None or m["loaded_at"] > latest):
                latest = m["loaded_at"]

        # Sum features
        total_features = sum(m["feature_count"] or 0 for m in metadata)

        return ProcessingStatus(
            orders_processed=len(order_ids),
            orders_failed=0,  # Would need error tracking
            total_features=total_features,
            total_tables=len(tables),
            last_processed=latest
        )

    def cleanup_stale_tables(self) -> list[str]:
        """
        Remove tables for orders that no longer exist in downloads.

        Returns:
            List of table names that were removed
        """
        loader = self._get_loader()
        metadata = loader.get_metadata()

        # Find orders that exist
        existing_orders = set(
            d.name for d in self.downloads_dir.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        )

        # Find metadata for non-existent orders
        stale = [
            m for m in metadata
            if m["order_id"] not in existing_orders
        ]

        removed = []
        conn = loader._get_conn()

        for m in stale:
            table_name = m["table_name"]
            try:
                with conn.cursor() as cur:
                    # Drop table
                    cur.execute(f'DROP TABLE IF EXISTS "{self.schema}"."{table_name}" CASCADE')
                    # Remove metadata
                    cur.execute(f"""
                        DELETE FROM "{self.schema}"._metadata
                        WHERE order_id = %s AND layer_name = %s
                    """, (m["order_id"], m["layer_name"]))
                conn.commit()
                removed.append(table_name)
            except Exception:
                conn.rollback()

        return removed

    def close(self):
        """Close database connections."""
        if self._loader:
            self._loader.close()
            self._loader = None


def get_order_info(order_dir: Path) -> dict:
    """
    Get information about an order for display.

    Args:
        order_dir: Path to the order directory

    Returns:
        Dict with order info suitable for dashboard display
    """
    detected = detect_order_type(order_dir)

    result = {
        "order_id": detected.order_id,
        "data_type": detected.data_type.value,
        "data_type_label": _get_type_label(detected.data_type),
        "is_publishable": is_publishable(detected.data_type),
        "layers": detected.layers,
        "file_count": len(detected.files),
        "total_size": detected.total_size,
        "total_size_mb": round(detected.total_size / (1024 * 1024), 2),
        "metadata": detected.metadata,
    }

    # Add LiDAR tile count for on-demand orders
    if detected.lidar_tiles:
        result["file_count"] = len(detected.lidar_tiles)
        result["lidar_tile_count"] = len(detected.lidar_tiles)

    return result


def _get_type_label(data_type: DataType) -> str:
    """Get human-readable label for data type."""
    labels = {
        DataType.VECTOR_GPKG: "Vector (GeoPackage)",
        DataType.LIDAR_LAZ: "LiDAR (LAZ)",
        DataType.LIDAR_INDEX: "LiDAR (On-Demand)",
        DataType.RASTER_DEM: "Raster (DEM)",
        DataType.RASTER_ORTHO: "Raster (Orthophoto)",
        DataType.UNKNOWN: "Unknown",
    }
    return labels.get(data_type, "Unknown")


def get_lidar_tiles(order_dir: Path) -> list[dict]:
    """
    Get LiDAR tiles for an on-demand order.

    Args:
        order_dir: Path to the order directory

    Returns:
        List of tile dicts with filename, coordinates, size, and download URL
    """
    detected = detect_order_type(order_dir)

    if detected.data_type != DataType.LIDAR_INDEX:
        return []

    tiles = []
    for tile in detected.lidar_tiles:
        # Convert grid to SWEREF99 TM meters for bounding box
        # grid_x is easting in km (e.g., 650 = 650,000 m)
        # grid_y is northing encoded as two digits (e.g., 60 = 6,600,000 m)
        # The northing has implicit leading "6" and is in 10km units
        min_x = tile.grid_x * 1000  # km to m
        min_y = 6000000 + tile.grid_y * 10000  # Convert to full northing
        max_x = min_x + 2500  # 2.5km tiles
        max_y = min_y + 2500

        tiles.append({
            "filename": tile.filename,
            "href": tile.href,
            "size": tile.size,
            "size_mb": round(tile.size / (1024 * 1024), 2),
            "grid_x": tile.grid_x,
            "grid_y": tile.grid_y,
            "bbox_sweref": [min_x, min_y, max_x, max_y],
        })

    return tiles


def get_lidar_tiles_geojson(order_dir: Path) -> dict:
    """
    Get LiDAR tiles as GeoJSON for map display.

    Args:
        order_dir: Path to the order directory

    Returns:
        GeoJSON FeatureCollection with tile polygons
    """
    from pyproj import Transformer

    tiles = get_lidar_tiles(order_dir)
    if not tiles:
        return {"type": "FeatureCollection", "features": []}

    # Transform from SWEREF99 TM (EPSG:3006) to WGS84 (EPSG:4326)
    transformer = Transformer.from_crs("EPSG:3006", "EPSG:4326", always_xy=True)

    features = []
    for tile in tiles:
        min_x, min_y, max_x, max_y = tile["bbox_sweref"]

        # Transform corners to WGS84
        lon1, lat1 = transformer.transform(min_x, min_y)
        lon2, lat2 = transformer.transform(max_x, max_y)

        features.append({
            "type": "Feature",
            "properties": {
                "filename": tile["filename"],
                "size_mb": tile["size_mb"],
                "grid_x": tile["grid_x"],
                "grid_y": tile["grid_y"],
            },
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [lon1, lat1],
                    [lon2, lat1],
                    [lon2, lat2],
                    [lon1, lat2],
                    [lon1, lat1],
                ]]
            }
        })

    return {"type": "FeatureCollection", "features": features}
