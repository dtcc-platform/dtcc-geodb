"""
Pure-Python WKB (Well-Known Binary) to GeoJSON geometry converter.

Supports Point, LineString, Polygon, and Multi* variants.
Uses only the stdlib struct module.
"""

import struct
from typing import Any


# WKB geometry type codes
WKB_POINT = 1
WKB_LINESTRING = 2
WKB_POLYGON = 3
WKB_MULTIPOINT = 4
WKB_MULTILINESTRING = 5
WKB_MULTIPOLYGON = 6
WKB_GEOMETRYCOLLECTION = 7

# Type codes with Z (1000+), M (2000+), ZM (3000+)
TYPE_MASK = 0xFF


def wkb_to_geojson(wkb: bytes) -> dict[str, Any]:
    """
    Convert WKB geometry to GeoJSON geometry dict.

    Args:
        wkb: WKB bytes (can be little or big endian)

    Returns:
        GeoJSON geometry dict with 'type' and 'coordinates'

    Raises:
        ValueError: If WKB is invalid or unsupported
    """
    if not wkb or len(wkb) < 5:
        raise ValueError("Invalid WKB: too short")

    # First byte indicates byte order
    byte_order = wkb[0]
    if byte_order == 0:
        endian = ">"  # Big endian
    elif byte_order == 1:
        endian = "<"  # Little endian
    else:
        raise ValueError(f"Invalid WKB byte order: {byte_order}")

    # Read geometry type (4 bytes)
    geom_type = struct.unpack(f"{endian}I", wkb[1:5])[0]

    # Handle EWKB (PostGIS extended WKB with SRID)
    has_srid = bool(geom_type & 0x20000000)
    offset = 5
    if has_srid:
        # Skip SRID (4 bytes)
        offset = 9

    # Mask out flags to get base type
    base_type = geom_type & TYPE_MASK

    return _parse_geometry(wkb, offset, base_type, endian)


def _parse_geometry(wkb: bytes, offset: int, geom_type: int, endian: str) -> dict:
    """Parse geometry at given offset."""

    if geom_type == WKB_POINT:
        return _parse_point(wkb, offset, endian)
    elif geom_type == WKB_LINESTRING:
        return _parse_linestring(wkb, offset, endian)
    elif geom_type == WKB_POLYGON:
        return _parse_polygon(wkb, offset, endian)
    elif geom_type == WKB_MULTIPOINT:
        return _parse_multi(wkb, offset, endian, "MultiPoint", WKB_POINT)
    elif geom_type == WKB_MULTILINESTRING:
        return _parse_multi(wkb, offset, endian, "MultiLineString", WKB_LINESTRING)
    elif geom_type == WKB_MULTIPOLYGON:
        return _parse_multi(wkb, offset, endian, "MultiPolygon", WKB_POLYGON)
    elif geom_type == WKB_GEOMETRYCOLLECTION:
        return _parse_geometry_collection(wkb, offset, endian)
    else:
        raise ValueError(f"Unsupported geometry type: {geom_type}")


def _parse_point(wkb: bytes, offset: int, endian: str) -> dict:
    """Parse Point geometry."""
    x, y = struct.unpack(f"{endian}dd", wkb[offset:offset + 16])
    return {"type": "Point", "coordinates": [x, y]}


def _parse_linestring(wkb: bytes, offset: int, endian: str) -> dict:
    """Parse LineString geometry."""
    num_points = struct.unpack(f"{endian}I", wkb[offset:offset + 4])[0]
    offset += 4
    coords = []
    for _ in range(num_points):
        x, y = struct.unpack(f"{endian}dd", wkb[offset:offset + 16])
        coords.append([x, y])
        offset += 16
    return {"type": "LineString", "coordinates": coords}


def _parse_polygon(wkb: bytes, offset: int, endian: str) -> dict:
    """Parse Polygon geometry."""
    num_rings = struct.unpack(f"{endian}I", wkb[offset:offset + 4])[0]
    offset += 4
    rings = []
    for _ in range(num_rings):
        num_points = struct.unpack(f"{endian}I", wkb[offset:offset + 4])[0]
        offset += 4
        ring = []
        for _ in range(num_points):
            x, y = struct.unpack(f"{endian}dd", wkb[offset:offset + 16])
            ring.append([x, y])
            offset += 16
        rings.append(ring)
    return {"type": "Polygon", "coordinates": rings}


def _parse_multi(
    wkb: bytes,
    offset: int,
    endian: str,
    geom_type_name: str,
    inner_type: int
) -> dict:
    """Parse Multi* geometry."""
    num_geoms = struct.unpack(f"{endian}I", wkb[offset:offset + 4])[0]
    offset += 4

    all_coords = []
    for _ in range(num_geoms):
        # Each sub-geometry has its own header
        inner_endian = "<" if wkb[offset] == 1 else ">"
        inner_geom_type = struct.unpack(f"{inner_endian}I", wkb[offset + 1:offset + 5])[0] & TYPE_MASK
        offset += 5

        if inner_type == WKB_POINT:
            geom = _parse_point(wkb, offset, inner_endian)
            all_coords.append(geom["coordinates"])
            offset += 16
        elif inner_type == WKB_LINESTRING:
            geom = _parse_linestring(wkb, offset, inner_endian)
            all_coords.append(geom["coordinates"])
            num_points = struct.unpack(f"{inner_endian}I", wkb[offset:offset + 4])[0]
            offset += 4 + num_points * 16
        elif inner_type == WKB_POLYGON:
            geom = _parse_polygon(wkb, offset, inner_endian)
            all_coords.append(geom["coordinates"])
            # Calculate offset advancement
            num_rings = struct.unpack(f"{inner_endian}I", wkb[offset:offset + 4])[0]
            offset += 4
            for _ in range(num_rings):
                num_points = struct.unpack(f"{inner_endian}I", wkb[offset:offset + 4])[0]
                offset += 4 + num_points * 16

    return {"type": geom_type_name, "coordinates": all_coords}


def _parse_geometry_collection(wkb: bytes, offset: int, endian: str) -> dict:
    """Parse GeometryCollection."""
    num_geoms = struct.unpack(f"{endian}I", wkb[offset:offset + 4])[0]
    offset += 4

    geometries = []
    for _ in range(num_geoms):
        inner_endian = "<" if wkb[offset] == 1 else ">"
        inner_geom_type = struct.unpack(f"{inner_endian}I", wkb[offset + 1:offset + 5])[0] & TYPE_MASK
        offset += 5
        geom = _parse_geometry(wkb, offset, inner_geom_type, inner_endian)
        geometries.append(geom)
        # Note: This doesn't properly track offset - GeometryCollections are complex
        # For full support, we'd need to return (geom, new_offset) from each parser

    return {"type": "GeometryCollection", "geometries": geometries}


def geojson_to_wkt(geojson: dict) -> str:
    """
    Convert GeoJSON geometry to WKT string.

    Args:
        geojson: GeoJSON geometry dict

    Returns:
        WKT string representation
    """
    geom_type = geojson["type"]

    if geom_type == "Point":
        coords = geojson["coordinates"]
        return f"POINT({coords[0]} {coords[1]})"

    elif geom_type == "LineString":
        coords = geojson["coordinates"]
        points = ", ".join(f"{c[0]} {c[1]}" for c in coords)
        return f"LINESTRING({points})"

    elif geom_type == "Polygon":
        rings = geojson["coordinates"]
        ring_strs = []
        for ring in rings:
            points = ", ".join(f"{c[0]} {c[1]}" for c in ring)
            ring_strs.append(f"({points})")
        return f"POLYGON({', '.join(ring_strs)})"

    elif geom_type == "MultiPoint":
        coords = geojson["coordinates"]
        points = ", ".join(f"({c[0]} {c[1]})" for c in coords)
        return f"MULTIPOINT({points})"

    elif geom_type == "MultiLineString":
        lines = geojson["coordinates"]
        line_strs = []
        for line in lines:
            points = ", ".join(f"{c[0]} {c[1]}" for c in line)
            line_strs.append(f"({points})")
        return f"MULTILINESTRING({', '.join(line_strs)})"

    elif geom_type == "MultiPolygon":
        polygons = geojson["coordinates"]
        poly_strs = []
        for poly in polygons:
            ring_strs = []
            for ring in poly:
                points = ", ".join(f"{c[0]} {c[1]}" for c in ring)
                ring_strs.append(f"({points})")
            poly_strs.append(f"({', '.join(ring_strs)})")
        return f"MULTIPOLYGON({', '.join(poly_strs)})"

    else:
        raise ValueError(f"Unsupported geometry type: {geom_type}")


def get_centroid(geojson: dict) -> tuple[float, float]:
    """
    Calculate approximate centroid of a GeoJSON geometry.

    Args:
        geojson: GeoJSON geometry dict

    Returns:
        (x, y) centroid coordinates
    """
    coords = _flatten_coords(geojson)
    if not coords:
        return (0.0, 0.0)

    x_sum = sum(c[0] for c in coords)
    y_sum = sum(c[1] for c in coords)
    n = len(coords)
    return (x_sum / n, y_sum / n)


def _flatten_coords(geojson: dict) -> list[list[float]]:
    """Flatten all coordinates from a GeoJSON geometry."""
    geom_type = geojson["type"]

    if geom_type == "Point":
        return [geojson["coordinates"]]
    elif geom_type == "LineString":
        return geojson["coordinates"]
    elif geom_type == "Polygon":
        return [c for ring in geojson["coordinates"] for c in ring]
    elif geom_type == "MultiPoint":
        return geojson["coordinates"]
    elif geom_type == "MultiLineString":
        return [c for line in geojson["coordinates"] for c in line]
    elif geom_type == "MultiPolygon":
        return [c for poly in geojson["coordinates"] for ring in poly for c in ring]
    elif geom_type == "GeometryCollection":
        return [c for g in geojson["geometries"] for c in _flatten_coords(g)]
    else:
        return []
