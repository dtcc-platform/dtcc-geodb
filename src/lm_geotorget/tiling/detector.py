"""
Data type detection for Lantmateriet Geotorget orders.

Scans downloaded ZIP files to determine data type and route to appropriate processor.
"""

from enum import Enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import json
import re
import zipfile


class DataType(Enum):
    """Supported data types from Lantmateriet."""
    VECTOR_GPKG = "vector_gpkg"      # GeoPackage -> PostGIS
    LIDAR_LAZ = "lidar_laz"          # LAZ/LAS -> file storage
    LIDAR_INDEX = "lidar_index"      # LiDAR tile index (on-demand download)
    RASTER_DEM = "raster_dem"        # GeoTIFF elevation
    RASTER_ORTHO = "raster_ortho"    # Orthophoto GeoTIFF/JP2
    UNKNOWN = "unknown"


@dataclass
class DetectedFile:
    """Information about a detected file within a ZIP."""
    zip_name: str
    inner_path: str
    extension: str
    size: int


@dataclass
class LidarTile:
    """Information about a LiDAR tile from an index file."""
    filename: str
    href: str
    size: int
    grid_x: int  # km in SWEREF99 TM (e.g., 640 = 6400000m)
    grid_y: int  # km in SWEREF99 TM (e.g., 50 = 500000m)


@dataclass
class DetectedOrder:
    """Result of order type detection."""
    order_id: str
    data_type: DataType
    files: list[DetectedFile] = field(default_factory=list)
    total_size: int = 0
    metadata: dict = field(default_factory=dict)
    layers: list[str] = field(default_factory=list)  # For GPKG: list of layer names
    lidar_tiles: list[LidarTile] = field(default_factory=list)  # For LiDAR index format


# File extension to data type mapping
EXTENSION_MAP = {
    ".gpkg": DataType.VECTOR_GPKG,
    ".laz": DataType.LIDAR_LAZ,
    ".las": DataType.LIDAR_LAZ,
    ".tif": DataType.RASTER_DEM,  # Could also be RASTER_ORTHO
    ".tiff": DataType.RASTER_DEM,
    ".jp2": DataType.RASTER_ORTHO,
}

# Product type hints from uttag.json
PRODUCT_TYPE_MAP = {
    "topografi": DataType.VECTOR_GPKG,
    "administrativ": DataType.VECTOR_GPKG,
    "fastighet": DataType.VECTOR_GPKG,
    "hojddata": DataType.RASTER_DEM,
    "hojdmodell": DataType.RASTER_DEM,
    "laserdata": DataType.LIDAR_LAZ,
    "ortofoto": DataType.RASTER_ORTHO,
}


def detect_order_type(order_dir: Path) -> DetectedOrder:
    """
    Scan an order directory to determine data type.

    Args:
        order_dir: Path to the downloaded order directory

    Returns:
        DetectedOrder with type, files, and metadata
    """
    order_id = order_dir.name
    detected_files: list[DetectedFile] = []
    total_size = 0
    metadata = {}

    # Load metadata from uttag.json if available
    uttag_path = order_dir / "uttag.json"
    if uttag_path.exists():
        try:
            with open(uttag_path, "r", encoding="utf-8") as f:
                metadata = json.load(f)
        except (json.JSONDecodeError, IOError):
            pass

    # Also check order_metadata.json
    order_meta_path = order_dir / "order_metadata.json"
    if order_meta_path.exists():
        try:
            with open(order_meta_path, "r", encoding="utf-8") as f:
                order_meta = json.load(f)
                metadata["order_metadata"] = order_meta
        except (json.JSONDecodeError, IOError):
            pass

    # Check for LiDAR index format first (files like "63_5", "64_4" containing JSON arrays)
    lidar_tiles = _detect_lidar_index(order_dir)
    if lidar_tiles:
        total_size = sum(tile.size for tile in lidar_tiles)
        return DetectedOrder(
            order_id=order_id,
            data_type=DataType.LIDAR_INDEX,
            files=[],
            total_size=total_size,
            metadata=metadata,
            layers=[],
            lidar_tiles=lidar_tiles
        )

    # Scan all ZIP files in the directory
    for zip_path in order_dir.glob("*.zip"):
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue

                    ext = Path(info.filename).suffix.lower()
                    detected_files.append(DetectedFile(
                        zip_name=zip_path.name,
                        inner_path=info.filename,
                        extension=ext,
                        size=info.file_size
                    ))
                    total_size += info.file_size
        except zipfile.BadZipFile:
            continue

    # Determine data type based on files
    data_type = _determine_type(detected_files, metadata)

    # Extract layer names for GeoPackage files
    layers = []
    if data_type == DataType.VECTOR_GPKG:
        layers = _extract_gpkg_layers(order_dir, detected_files)

    return DetectedOrder(
        order_id=order_id,
        data_type=data_type,
        files=detected_files,
        total_size=total_size,
        metadata=metadata,
        layers=layers
    )


def _detect_lidar_index(order_dir: Path) -> list[LidarTile]:
    """
    Detect LiDAR tile index format.

    LiDAR orders from Lantmateriet may come as index files (e.g., "63_5", "64_4")
    containing JSON arrays of tile download links instead of actual .laz files.

    Returns:
        List of LidarTile objects if index format detected, empty list otherwise.
    """
    tiles = []

    # Pattern for index files: digits_digits (e.g., "63_5", "64_4")
    index_pattern = re.compile(r'^(\d+)_(\d+)$')

    for file_path in order_dir.iterdir():
        if not file_path.is_file():
            continue

        match = index_pattern.match(file_path.name)
        if not match:
            continue

        # Try to parse as JSON array of tile links
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()

            # Check if it looks like JSON array
            if not content.strip().startswith('['):
                continue

            data = json.loads(content)
            if not isinstance(data, list):
                continue

            # Parse each tile entry
            for entry in data:
                if not isinstance(entry, dict):
                    continue

                href = entry.get('href', '')
                title = entry.get('title', '')
                length = entry.get('length', 0)

                # Only process .laz files
                if not title.endswith('.laz'):
                    continue

                # Parse coordinates from filename
                # Format: {scan_id}_{x}_{y}_{suffix}.laz (e.g., 20C020_650_60_0000.laz)
                tile_match = re.match(r'^[^_]+_(\d+)_(\d+)_.*\.laz$', title)
                if tile_match:
                    grid_x = int(tile_match.group(1))
                    grid_y = int(tile_match.group(2))
                else:
                    # Default to index file coordinates if can't parse
                    grid_x = int(match.group(1)) * 10
                    grid_y = int(match.group(2)) * 10

                tiles.append(LidarTile(
                    filename=title,
                    href=href,
                    size=length,
                    grid_x=grid_x,
                    grid_y=grid_y
                ))

        except (json.JSONDecodeError, IOError, ValueError):
            continue

    return tiles


def _determine_type(files: list[DetectedFile], metadata: dict) -> DataType:
    """Determine data type from files and metadata."""

    # Count file types
    type_counts: dict[DataType, int] = {}
    for f in files:
        if f.extension in EXTENSION_MAP:
            dtype = EXTENSION_MAP[f.extension]
            type_counts[dtype] = type_counts.get(dtype, 0) + 1

    # Check metadata for product type hints
    product_hint = None
    if metadata:
        # Check various metadata fields for product type
        for key in ["produkttyp", "product_type", "datatyp", "data_type"]:
            if key in metadata:
                value = str(metadata[key]).lower()
                for hint, dtype in PRODUCT_TYPE_MAP.items():
                    if hint in value:
                        product_hint = dtype
                        break

    # Disambiguate TIF files (DEM vs Ortho) using metadata
    if DataType.RASTER_DEM in type_counts and product_hint == DataType.RASTER_ORTHO:
        type_counts[DataType.RASTER_ORTHO] = type_counts.pop(DataType.RASTER_DEM)

    # Return the most common type
    if type_counts:
        return max(type_counts.keys(), key=lambda k: type_counts[k])

    # Fall back to metadata hint
    if product_hint:
        return product_hint

    return DataType.UNKNOWN


def _extract_gpkg_layers(order_dir: Path, files: list[DetectedFile]) -> list[str]:
    """Extract actual layer names from GeoPackage files."""
    import tempfile
    import os
    import sqlite3

    layers = []

    # Group files by zip
    gpkg_by_zip: dict[str, list[str]] = {}
    for f in files:
        if f.extension == ".gpkg":
            if f.zip_name not in gpkg_by_zip:
                gpkg_by_zip[f.zip_name] = []
            gpkg_by_zip[f.zip_name].append(f.inner_path)

    # Extract each GPKG and read its layers
    for zip_name, inner_paths in gpkg_by_zip.items():
        zip_path = order_dir / zip_name
        if not zip_path.exists():
            continue

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                for inner_path in inner_paths:
                    # Extract to temp file
                    fd, temp_path = tempfile.mkstemp(suffix=".gpkg")
                    try:
                        os.close(fd)
                        with zf.open(inner_path) as src:
                            with open(temp_path, "wb") as dst:
                                dst.write(src.read())

                        # Read layer names from GPKG
                        with sqlite3.connect(temp_path) as conn:
                            cursor = conn.execute("""
                                SELECT table_name
                                FROM gpkg_contents
                                WHERE data_type = 'features'
                            """)
                            for row in cursor.fetchall():
                                layers.append(row[0])
                    finally:
                        # Clean up temp file
                        try:
                            os.unlink(temp_path)
                        except OSError:
                            pass
        except (zipfile.BadZipFile, sqlite3.Error):
            continue

    return sorted(set(layers))


def get_type_label(data_type: DataType) -> str:
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


def get_type_color(data_type: DataType) -> str:
    """Get CSS color for data type badge."""
    colors = {
        DataType.VECTOR_GPKG: "#4CAF50",   # Green
        DataType.LIDAR_LAZ: "#9C27B0",      # Purple
        DataType.LIDAR_INDEX: "#E91E63",    # Pink
        DataType.RASTER_DEM: "#FF9800",     # Orange
        DataType.RASTER_ORTHO: "#2196F3",   # Blue
        DataType.UNKNOWN: "#757575",        # Gray
    }
    return colors.get(data_type, "#757575")


def is_publishable(data_type: DataType) -> bool:
    """Check if data type can be published to PostGIS."""
    return data_type == DataType.VECTOR_GPKG
