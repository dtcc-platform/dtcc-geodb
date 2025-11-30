#!/usr/bin/env python3
"""
Standalone client to download Lantmateriet Geotorget orders.

Usage:
    python download_order.py
    python download_order.py <order-id>
    python download_order.py <order-id> --output /path/to/folder
"""

import sys
import os
import json
import argparse
import requests
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

DEFAULT_OUTPUT_DIR = Path.home() / "Downloads" / "geotorget"

BASE_URL = "https://download-geotorget.lantmateriet.se/download"


def get_file_list(order_id: str) -> list[dict]:
    """Fetch the list of files for an order."""
    url = f"{BASE_URL}/{order_id}/files"
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return response.json()


def check_for_updates(order_id: str, order_dir: Path) -> dict:
    """
    Check if there are updates available for an order.

    Returns dict with:
        - has_update: bool
        - local_date: str (last download date)
        - remote_date: str (latest available date)
        - new_files: list of files with updates
    """
    result = {
        "has_update": False,
        "local_date": None,
        "remote_date": None,
        "new_files": [],
    }

    # Get local metadata
    metadata = load_order_metadata(order_dir)
    if not metadata:
        result["has_update"] = True
        result["new_files"] = ["All files (no local download)"]
        return result

    local_date = metadata.get("download_date")
    result["local_date"] = local_date

    # Get remote file list
    try:
        remote_files = get_file_list(order_id)
    except Exception as e:
        print(f"Error checking updates: {e}")
        return result

    if not remote_files:
        return result

    # Get latest remote update date
    remote_dates = []
    for f in remote_files:
        updated = f.get("updated")
        if updated:
            remote_dates.append(updated)

    if remote_dates:
        result["remote_date"] = max(remote_dates)

    # Compare dates
    if local_date and result["remote_date"]:
        # Parse dates for comparison
        try:
            local_dt = datetime.fromisoformat(local_date.replace("Z", "+00:00"))
            remote_dt = datetime.fromisoformat(result["remote_date"].replace("Z", "+00:00"))

            if remote_dt > local_dt:
                result["has_update"] = True
                # Find which files are newer
                for f in remote_files:
                    updated = f.get("updated")
                    if updated:
                        file_dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                        if file_dt > local_dt:
                            result["new_files"].append(f.get("title", "unknown"))
        except (ValueError, TypeError):
            pass

    return result


def check_all_updates(base_dir: Path) -> None:
    """Check for updates on all downloaded orders."""
    print(f"Checking for updates in: {base_dir}")
    print()

    has_any_updates = False

    for subdir in sorted(base_dir.iterdir()):
        if subdir.is_dir():
            order_id = subdir.name
            metadata = load_order_metadata(subdir)
            if metadata:
                result = check_for_updates(order_id, subdir)

                local_str = result["local_date"][:10] if result["local_date"] else "N/A"
                remote_str = result["remote_date"][:10] if result["remote_date"] else "N/A"

                if result["has_update"]:
                    has_any_updates = True
                    print(f"[UPDATE] {order_id}")
                    print(f"         Local: {local_str}  Remote: {remote_str}")
                    if result["new_files"]:
                        print(f"         New/updated files: {len(result['new_files'])}")
                    print()
                else:
                    print(f"[OK]     {order_id} (up to date)")

    print()
    if has_any_updates:
        print("To download updates, run:")
        print("  python download_order.py <order-id>")
    else:
        print("All orders are up to date.")


def format_size(size_bytes: int) -> str:
    """Format byte size to human readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def download_file(file_info: dict, output_dir: Path, progress_bar=None) -> tuple[str, bool, str]:
    """
    Download a single file.

    Returns:
        Tuple of (filename, success, message)
    """
    title = file_info["title"]
    href = file_info["href"]
    total_size = file_info.get("length", 0)
    display_size = file_info.get("displaySize", "unknown size")
    output_path = output_dir / title

    if output_path.exists():
        if progress_bar:
            progress_bar.update(total_size)
        return (title, True, "already exists, skipped")

    try:
        response = requests.get(href, stream=True, timeout=300)
        response.raise_for_status()

        with open(output_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    if progress_bar:
                        progress_bar.update(len(chunk))

        return (title, True, f"downloaded ({display_size})")
    except Exception as e:
        return (title, False, str(e))


def save_order_metadata(order_id: str, order_dir: Path, files: list[dict]) -> None:
    """Save order metadata for dashboard aggregation."""
    metadata = {
        "order_id": order_id,
        "download_date": datetime.now().isoformat(),
        "files": files,
    }
    meta_path = order_dir / "order_metadata.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)


def load_order_metadata(order_dir: Path) -> dict | None:
    """Load order metadata from a directory."""
    meta_path = order_dir / "order_metadata.json"
    if not meta_path.exists():
        return None
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def load_uttag_data(order_dir: Path) -> dict:
    """Load uttag.json data from order directory."""
    uttag_path = order_dir / "uttag.json"
    uttag_data = {}
    if uttag_path.exists():
        try:
            with open(uttag_path, "r", encoding="utf-8") as f:
                geojson = json.load(f)
                for feature in geojson.get("features", []):
                    props = feature.get("properties", {})
                    filename = props.get("filnamn", "")
                    if filename:
                        uttag_data[filename] = {
                            "statistik": props.get("statistik", []),
                            "geometry": feature.get("geometry"),
                        }
        except Exception:
            pass
    return uttag_data


def sweref99_to_wgs84(e: float, n: float) -> tuple[float, float] | None:
    """
    Convert SWEREF99 TM coordinates to WGS84 lat/lng.
    Uses pyproj if available, otherwise returns None for out-of-range coords.
    """
    # Check if coordinates are within reasonable SWEREF99 TM range first
    # Easting should be roughly 200,000 - 1,000,000
    # Northing should be roughly 6,000,000 - 7,700,000
    if not (100000 < e < 1100000 and 6000000 < n < 7800000):
        return None

    # Try pyproj first (most accurate)
    try:
        from pyproj import Transformer
        transformer = Transformer.from_crs("EPSG:3006", "EPSG:4326", always_xy=True)
        lng, lat = transformer.transform(e, n)
        return (lat, lng)
    except ImportError:
        pass

    import math

    # SWEREF99 TM parameters
    a = 6378137.0
    f = 1 / 298.257222101
    k0 = 0.9996
    lon0 = math.radians(15.0)
    false_easting = 500000.0

    e2 = f * (2 - f)
    e_prime2 = e2 / (1 - e2)
    n_param = f / (2 - f)
    n2 = n_param ** 2
    n3 = n_param ** 3
    n4 = n_param ** 4

    x = e - false_easting
    y = n

    A = a / (1 + n_param) * (1 + n2/4 + n4/64)
    xi = y / (k0 * A)
    eta = x / (k0 * A)

    d1 = n_param/2 - 2*n2/3 + 37*n3/96
    d2 = n2/48 + n3/15
    d3 = 17*n3/480

    xi_prime = xi - (d1*math.sin(2*xi)*math.cosh(2*eta) +
                     d2*math.sin(4*xi)*math.cosh(4*eta) +
                     d3*math.sin(6*xi)*math.cosh(6*eta))
    eta_prime = eta - (d1*math.cos(2*xi)*math.sinh(2*eta) +
                       d2*math.cos(4*xi)*math.sinh(4*eta) +
                       d3*math.cos(6*xi)*math.sinh(6*eta))

    phi_star = math.asin(math.sin(xi_prime) / math.cosh(eta_prime))

    lat = phi_star + math.sin(phi_star)*math.cos(phi_star)*(
        e_prime2 +
        e_prime2**2 * (5 - math.tan(phi_star)**2) / 6 +
        e_prime2**3 * (9 + 4*math.tan(phi_star)**2) / 120
    )

    lon = lon0 + math.atan(math.sinh(eta_prime) / math.cos(xi_prime))

    return (math.degrees(lat), math.degrees(lon))


def get_order_bounds_wgs84(uttag_data: dict) -> list | None:
    """Extract combined bounds from uttag data and convert to WGS84."""
    if not uttag_data:
        return None

    min_e, min_n = float('inf'), float('inf')
    max_e, max_n = float('-inf'), float('-inf')

    for filename, data in uttag_data.items():
        geom = data.get("geometry")
        if geom and geom.get("type") == "Polygon":
            for ring in geom.get("coordinates", []):
                for coord in ring:
                    e, n = coord[0], coord[1]
                    min_e = min(min_e, e)
                    min_n = min(min_n, n)
                    max_e = max(max_e, e)
                    max_n = max(max_n, n)

    if min_e == float('inf'):
        return None

    # Convert corners to WGS84
    sw = sweref99_to_wgs84(min_e, min_n)
    ne = sweref99_to_wgs84(max_e, max_n)

    # If conversion failed (out of range coords), use Sweden's bounds as fallback
    if sw is None or ne is None:
        # Sweden approximate bounds
        sw = (55.3, 10.9)  # Southern tip
        ne = (69.1, 24.2)  # Northern tip

    return [sw, ne]  # [[lat, lng], [lat, lng]]


def generate_dashboard(base_dir: Path) -> Path:
    """Generate a combined HTML dashboard for all downloaded orders."""

    # Scan for all order directories
    orders = []
    uttag_json_data = {}  # Store raw uttag.json for modal display
    for subdir in sorted(base_dir.iterdir()):
        if subdir.is_dir():
            metadata = load_order_metadata(subdir)
            if metadata:
                metadata["_dir"] = subdir
                metadata["_uttag"] = load_uttag_data(subdir)
                metadata["_bounds"] = get_order_bounds_wgs84(metadata["_uttag"])
                orders.append(metadata)

                # Load raw uttag.json for modal
                uttag_path = subdir / "uttag.json"
                if uttag_path.exists():
                    try:
                        with open(uttag_path, "r", encoding="utf-8") as f:
                            uttag_json_data[metadata["order_id"]] = json.load(f)
                    except Exception:
                        pass

    # Calculate totals
    total_orders = len(orders)
    total_files = 0
    total_size = 0
    total_objects = 0

    # Build order rows
    order_rows = []
    for order in orders:
        order_id = order.get("order_id", "Unknown")
        download_date = order.get("download_date", "")
        files = order.get("files", [])
        order_dir = order.get("_dir")
        uttag_data = order.get("_uttag", {})

        order_size = sum(f.get("length", 0) for f in files)
        order_files = len(files)

        # Count objects from uttag
        order_objects = 0
        for f in files:
            meta = uttag_data.get(f.get("title", ""), {})
            stats = meta.get("statistik", [])
            order_objects += sum(s.get("antalObjekt", 0) for s in stats)

        total_files += order_files
        total_size += order_size
        total_objects += order_objects

        # Format date
        try:
            dt = datetime.fromisoformat(download_date)
            date_str = dt.strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            date_str = download_date

        # Get bounds for map
        bounds = order.get("_bounds")
        bounds_attr = ""
        if bounds:
            bounds_attr = f'data-bounds="{bounds[0][0]},{bounds[0][1]},{bounds[1][0]},{bounds[1][1]}"'

        map_button = ""
        if bounds:
            map_button = f'<button class="map-btn" onclick="event.stopPropagation(); showMapForOrder(\'{order_id}\', \'{bounds[0][0]},{bounds[0][1]},{bounds[1][0]},{bounds[1][1]}\')">Map</button>'

        # Detect data type for publish button
        data_type_badge = ""
        publish_button = ""
        try:
            from src.lm_geotorget.tiling.detector import detect_order_type, is_publishable, get_type_label, get_type_color
            detected = detect_order_type(order_dir)
            type_label = get_type_label(detected.data_type)
            type_color = get_type_color(detected.data_type)
            data_type_badge = f'<span class="data-type-badge" style="background: {type_color};">{type_label}</span>'

            if is_publishable(detected.data_type):
                publish_button = f'<button class="publish-btn" onclick="event.stopPropagation(); showPublishModal(\'{order_id}\')">Publish</button>'
        except ImportError:
            pass

        order_rows.append(f"""
        <tr class="order-row" onclick="toggleOrder('{order_id}')">
            <td><span class="expand-icon" id="icon-{order_id}">+</span> <code>{order_id[:8]}...</code> {data_type_badge} {map_button} {publish_button}</td>
            <td>{date_str}</td>
            <td>{order_files}</td>
            <td>{format_size(order_size)}</td>
            <td>{order_objects:,}</td>
        </tr>
        """)

        # Build file rows for this order (hidden by default)
        for f in files:
            title = f.get("title", "")
            size = f.get("length", 0)
            display_size = f.get("displaySize", "N/A")

            local_path = order_dir / title
            exists = local_path.exists()
            status_class = "status-ok" if exists else "status-missing"
            status_text = "OK" if exists else "Missing"

            meta = uttag_data.get(title, {})
            stats = meta.get("statistik", [])
            file_objects = sum(s.get("antalObjekt", 0) for s in stats)

            stats_html = ""
            if stats:
                stats_items = [f"{s.get('tabellnamn', '?')}: {s.get('antalObjekt', 0):,}" for s in stats]
                stats_html = ", ".join(stats_items)
            else:
                stats_html = "-"

            # Make uttag.json clickable
            if title == "uttag.json":
                title_html = f'<span class="clickable" onclick="event.stopPropagation(); showUttagModal(\'{order_id}\')">{title}</span>'
            else:
                title_html = title

            order_rows.append(f"""
            <tr class="file-row" data-order="{order_id}" style="display: none;">
                <td style="padding-left: 2.5rem;">{title_html}</td>
                <td><span class="status {status_class}">{status_text}</span></td>
                <td>1</td>
                <td>{display_size}</td>
                <td title="{stats_html}">{file_objects:,}</td>
            </tr>
            """)

    # Empty state
    if not order_rows:
        order_rows.append("""
        <tr>
            <td colspan="5" class="empty-state">
                No orders downloaded yet.<br>
                Run: <code>python download_order.py &lt;order-id&gt;</code>
            </td>
        </tr>
        """)

    # Prepare blueprint data for order nodes
    blueprint_orders = []
    for order in orders:
        order_id = order.get("order_id", "")
        files = order.get("files", [])
        uttag_data = order.get("_uttag", {})
        order_objects = 0
        for f in files:
            meta = uttag_data.get(f.get("title", ""), {})
            stats = meta.get("statistik", [])
            order_objects += sum(s.get("antalObjekt", 0) for s in stats)
        blueprint_orders.append({
            "order_id": order_id,
            "file_count": len(files),
            "object_count": order_objects,
            "download_date": order.get("download_date", ""),
        })

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Geotorget Downloads</title>
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/drawflow@0.0.59/dist/drawflow.min.css" />
    <script src="https://cdn.jsdelivr.net/npm/drawflow@0.0.59/dist/drawflow.min.js"></script>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Montserrat:wght@300;500;600;700&display=swap');
        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }}
        :root {{
            --gold: #FADA36;
            --gold-dim: rgba(250, 218, 54, 0.2);
            --gold-subtle: rgba(250, 218, 54, 0.1);
            --dark-bg: #101016;
            --dark-secondary: #1b1b22;
            --dark-card: rgba(255, 255, 255, 0.05);
            --border-subtle: rgba(255, 255, 255, 0.08);
            --text-primary: #ffffff;
            --text-secondary: rgba(255, 255, 255, 0.5);
            --text-dim: rgba(255, 255, 255, 0.35);
        }}
        body {{
            font-family: 'Montserrat', -apple-system, BlinkMacSystemFont, sans-serif;
            background: linear-gradient(180deg, var(--dark-bg) 0%, var(--dark-secondary) 100%);
            min-height: 100vh;
            color: var(--text-primary);
            line-height: 1.6;
            padding: 2rem;
        }}
        .container {{
            max-width: 1400px;
            margin: 0 auto;
        }}
        .page-header {{
            margin-bottom: 2rem;
        }}
        h1 {{
            color: var(--gold);
            font-size: 1.98em;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            margin-bottom: 0.5rem;
        }}
        .subtitle {{
            color: var(--text-secondary);
            font-size: 0.9rem;
            font-weight: 300;
        }}
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 1rem;
            margin-bottom: 2rem;
        }}
        .stat-card {{
            background: var(--dark-card);
            border: 1px solid var(--border-subtle);
            border-radius: 8px;
            padding: 1.5rem;
            backdrop-filter: blur(12px);
        }}
        .stat-card h3 {{
            font-size: 0.75rem;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.06em;
            font-weight: 500;
            margin-bottom: 0.5rem;
        }}
        .stat-card .value {{
            font-size: 1.8rem;
            font-weight: 700;
            color: var(--gold);
        }}
        .card {{
            background: var(--dark-card);
            border: 1px solid var(--border-subtle);
            border-radius: 8px;
            overflow: hidden;
            backdrop-filter: blur(12px);
        }}
        .card-header {{
            padding: 1rem 1.5rem;
            border-bottom: 1px solid var(--border-subtle);
            background: rgba(255, 255, 255, 0.02);
        }}
        .card-header h2 {{
            font-size: 1.1rem;
            color: var(--gold);
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.06em;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
        }}
        th, td {{
            padding: 0.75rem 1rem;
            text-align: left;
            border-bottom: 1px solid var(--border-subtle);
        }}
        th {{
            background: rgba(255, 255, 255, 0.02);
            font-weight: 600;
            font-size: 0.75rem;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.06em;
        }}
        .order-row {{
            cursor: pointer;
            background: transparent;
            transition: background 0.2s;
        }}
        .order-row:hover {{
            background: var(--gold-subtle);
        }}
        .file-row {{
            background: rgba(255, 255, 255, 0.02);
            font-size: 0.9rem;
        }}
        .file-row:hover {{
            background: var(--gold-subtle);
        }}
        .expand-icon {{
            display: inline-block;
            width: 1rem;
            font-weight: bold;
            color: var(--gold);
        }}
        code {{
            background: rgba(250, 218, 54, 0.15);
            color: var(--gold);
            padding: 0.2rem 0.4rem;
            border-radius: 4px;
            font-size: 0.85rem;
            font-family: 'Monaco', 'Consolas', monospace;
        }}
        .status {{
            display: inline-block;
            padding: 0.2rem 0.5rem;
            border-radius: 4px;
            font-size: 0.7rem;
            font-weight: 600;
            text-transform: uppercase;
        }}
        .status-ok {{
            background: rgba(72, 187, 120, 0.2);
            color: #48bb78;
            border: 1px solid rgba(72, 187, 120, 0.3);
        }}
        .status-missing {{
            background: rgba(245, 101, 101, 0.2);
            color: #f56565;
            border: 1px solid rgba(245, 101, 101, 0.3);
        }}
        .empty-state {{
            text-align: center;
            padding: 3rem !important;
            color: var(--text-secondary);
        }}
        .footer {{
            margin-top: 2rem;
            text-align: center;
            color: var(--text-dim);
            font-size: 0.85rem;
            font-weight: 300;
        }}
        .footer a {{
            color: var(--gold);
            text-decoration: none;
        }}
        .footer a:hover {{
            text-decoration: underline;
        }}
        #map-container {{
            display: none;
            margin-bottom: 2rem;
        }}
        #map-container.visible {{
            display: block;
        }}
        #map {{
            height: 400px;
            border-radius: 8px;
            border: 1px solid var(--border-subtle);
        }}
        .map-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 0.5rem;
        }}
        .map-header h3 {{
            color: var(--text-primary);
            font-size: 1rem;
            font-weight: 600;
        }}
        .map-close {{
            background: var(--dark-card);
            border: 1px solid var(--border-subtle);
            color: var(--text-primary);
            padding: 0.25rem 0.75rem;
            border-radius: 4px;
            cursor: pointer;
            font-size: 0.85rem;
            font-family: inherit;
            transition: all 0.2s;
        }}
        .map-close:hover {{
            background: var(--gold-subtle);
            border-color: var(--gold);
        }}
        .selected-row {{
            background: var(--gold-dim) !important;
        }}
        .clickable {{
            color: var(--gold);
            cursor: pointer;
            text-decoration: underline;
        }}
        .clickable:hover {{
            opacity: 0.8;
        }}
        .map-btn {{
            background: var(--gold);
            color: var(--dark-bg);
            border: none;
            padding: 0.25rem 0.5rem;
            border-radius: 4px;
            font-size: 0.75rem;
            font-weight: 600;
            cursor: pointer;
            margin-left: 0.5rem;
            font-family: inherit;
            transition: all 0.2s;
        }}
        .map-btn:hover {{
            opacity: 0.9;
            transform: translateY(-1px);
        }}
        .publish-btn {{
            background: #4CAF50;
            color: white;
            border: none;
            padding: 0.25rem 0.5rem;
            border-radius: 4px;
            font-size: 0.75rem;
            font-weight: 600;
            cursor: pointer;
            margin-left: 0.5rem;
            font-family: inherit;
            transition: all 0.2s;
        }}
        .publish-btn:hover {{
            background: #45a049;
            transform: translateY(-1px);
        }}
        .data-type-badge {{
            padding: 0.15rem 0.4rem;
            border-radius: 3px;
            font-size: 0.65rem;
            font-weight: 600;
            margin-left: 0.5rem;
            color: white;
            text-transform: uppercase;
        }}
        .publish-modal-content {{
            padding: 1.5rem;
        }}
        .publish-layer-list {{
            max-height: 300px;
            overflow-y: auto;
            margin: 1rem 0;
            border: 1px solid var(--border-subtle);
            border-radius: 4px;
            padding: 0.5rem;
        }}
        .publish-layer-item {{
            display: flex;
            align-items: center;
            padding: 0.5rem;
            border-bottom: 1px solid var(--border-subtle);
        }}
        .publish-layer-item:last-child {{
            border-bottom: none;
        }}
        .publish-layer-item input[type="checkbox"] {{
            margin-right: 0.75rem;
            width: 18px;
            height: 18px;
        }}
        .publish-progress {{
            display: none;
            margin-top: 1rem;
        }}
        .publish-progress.visible {{
            display: block;
        }}
        .progress-bar {{
            height: 8px;
            background: var(--dark-bg);
            border-radius: 4px;
            overflow: hidden;
            margin-top: 0.5rem;
        }}
        .progress-bar-fill {{
            height: 100%;
            background: var(--gold);
            width: 0%;
            transition: width 0.3s;
        }}
        .publish-status {{
            margin-top: 1rem;
            padding: 1rem;
            border-radius: 4px;
            display: none;
        }}
        .publish-status.success {{
            display: block;
            background: rgba(76, 175, 80, 0.2);
            border: 1px solid #4CAF50;
        }}
        .publish-status.error {{
            display: block;
            background: rgba(244, 67, 54, 0.2);
            border: 1px solid #f44336;
        }}
        .modal-overlay {{
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0, 0, 0, 0.8);
            z-index: 1000;
            justify-content: center;
            align-items: center;
            backdrop-filter: blur(4px);
        }}
        .modal-overlay.visible {{
            display: flex;
        }}
        .modal {{
            background: var(--dark-secondary);
            border: 1px solid var(--border-subtle);
            border-radius: 8px;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.5);
            max-width: 80%;
            max-height: 80%;
            overflow: hidden;
            display: flex;
            flex-direction: column;
        }}
        .modal-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 1rem 1.5rem;
            border-bottom: 1px solid var(--border-subtle);
            background: rgba(255, 255, 255, 0.02);
        }}
        .modal-header h3 {{
            margin: 0;
            color: var(--gold);
            font-size: 1rem;
            font-weight: 600;
        }}
        .modal-close {{
            background: none;
            border: none;
            font-size: 1.5rem;
            cursor: pointer;
            color: var(--text-secondary);
            padding: 0;
            line-height: 1;
            transition: color 0.2s;
        }}
        .modal-close:hover {{
            color: var(--gold);
        }}
        .modal-body {{
            padding: 1rem;
            overflow: auto;
            max-height: 60vh;
        }}
        .modal-body pre {{
            margin: 0;
            white-space: pre-wrap;
            word-wrap: break-word;
            font-size: 0.85rem;
            background: var(--dark-bg);
            color: var(--text-primary);
            padding: 1rem;
            border-radius: 4px;
            overflow-x: auto;
            border: 1px solid var(--border-subtle);
        }}
        /* Custom Scrollbar */
        ::-webkit-scrollbar {{
            width: 8px;
            height: 8px;
        }}
        ::-webkit-scrollbar-track {{
            background: var(--dark-bg);
        }}
        ::-webkit-scrollbar-thumb {{
            background: var(--gold-dim);
            border-radius: 4px;
        }}
        ::-webkit-scrollbar-thumb:hover {{
            background: var(--gold);
        }}
        /* DTCC Header */
        .dtcc-header {{
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            height: 56px;
            background: linear-gradient(180deg, rgba(16, 16, 22, 0.95) 0%, rgba(27, 27, 34, 0.9) 100%);
            backdrop-filter: blur(12px);
            border-bottom: 1px solid var(--border-subtle);
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 0 2rem;
            z-index: 100;
        }}
        .dtcc-header .logo {{
            display: flex;
            align-items: center;
            gap: 0.75rem;
        }}
        .dtcc-header .logo img {{
            height: 28px;
            width: auto;
        }}
        .dtcc-header .logo-text {{
            font-size: 0.7rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            line-height: 1.2;
            color: var(--text-primary);
        }}
        .dtcc-header nav {{
            display: flex;
            gap: 2rem;
        }}
        .dtcc-header nav a {{
            color: var(--gold);
            text-decoration: none;
            font-size: 0.75rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            transition: opacity 0.2s ease;
        }}
        .dtcc-header nav a:hover {{
            opacity: 0.7;
        }}
        /* Adjust body padding for fixed header */
        body {{
            padding-top: calc(56px + 2rem);
        }}
        /* Tab System */
        .tab-bar {{
            display: flex;
            gap: 0;
            margin-bottom: 1.5rem;
            border-bottom: 2px solid var(--border-subtle);
        }}
        .tab-btn {{
            padding: 0.75rem 1.5rem;
            background: none;
            border: none;
            font-size: 0.85rem;
            font-weight: 600;
            color: var(--text-secondary);
            cursor: pointer;
            border-bottom: 2px solid transparent;
            margin-bottom: -2px;
            transition: all 0.2s;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            font-family: inherit;
        }}
        .tab-btn:hover {{
            color: var(--text-primary);
        }}
        .tab-btn.active {{
            color: var(--gold);
            border-bottom-color: var(--gold);
        }}
        .tab-panel {{
            display: none;
        }}
        .tab-panel.active {{
            display: block;
        }}
        /* Blueprint Editor */
        .blueprint-container {{
            background: var(--dark-card);
            border: 1px solid var(--border-subtle);
            border-radius: 8px;
            overflow: hidden;
        }}
        .blueprint-toolbar {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 0.75rem 1rem;
            background: rgba(255, 255, 255, 0.02);
            border-bottom: 1px solid var(--border-subtle);
        }}
        .toolbar-title {{
            font-weight: 600;
            color: var(--gold);
            text-transform: uppercase;
            letter-spacing: 0.06em;
            font-size: 0.85rem;
        }}
        .toolbar-actions {{
            display: flex;
            gap: 0.5rem;
        }}
        .toolbar-actions button {{
            padding: 0.4rem 0.8rem;
            font-size: 0.75rem;
            border: 1px solid var(--border-subtle);
            border-radius: 4px;
            background: var(--dark-card);
            color: var(--text-primary);
            cursor: pointer;
            transition: all 0.2s;
            font-family: inherit;
            font-weight: 500;
        }}
        .toolbar-actions button:hover {{
            background: var(--gold-subtle);
            border-color: var(--gold);
            color: var(--gold);
        }}
        #drawflow-canvas {{
            height: 600px;
            background: var(--dark-bg);
            background-image: radial-gradient(circle, rgba(250, 218, 54, 0.1) 1px, transparent 1px);
            background-size: 20px 20px;
        }}
        /* Drawflow Overrides */
        .drawflow .drawflow-node {{
            border-radius: 8px;
            border: 2px solid var(--border-subtle);
            background: var(--dark-secondary);
            color: var(--text-primary);
            min-width: 200px;
            font-family: inherit;
        }}
        .drawflow .drawflow-node.selected {{
            border-color: var(--gold);
            box-shadow: 0 0 15px rgba(250, 218, 54, 0.3);
        }}
        .drawflow .drawflow-node .title-box {{
            padding: 0.5rem 0.75rem;
            border-bottom: 1px solid var(--border-subtle);
            font-weight: 600;
            font-size: 0.8rem;
            border-radius: 6px 6px 0 0;
            text-transform: uppercase;
            letter-spacing: 0.06em;
        }}
        .drawflow .drawflow-node.order-node {{
            border-color: var(--gold);
        }}
        .drawflow .drawflow-node.order-node .title-box {{
            background: var(--gold);
            color: var(--dark-bg);
        }}
        .drawflow .drawflow-node.database-node {{
            border-color: #48bb78;
        }}
        .drawflow .drawflow-node.database-node .title-box {{
            background: #48bb78;
            color: var(--dark-bg);
        }}
        .drawflow .drawflow-node.filter-node {{
            border-color: #9f7aea;
        }}
        .drawflow .drawflow-node.filter-node .title-box {{
            background: #9f7aea;
            color: var(--dark-bg);
        }}
        .drawflow .drawflow-node .box {{
            padding: 0.5rem 0.75rem;
        }}
        .node-info {{
            font-size: 0.75rem;
            color: var(--text-secondary);
        }}
        .node-info .label {{
            color: var(--text-dim);
        }}
        .node-info .value {{
            color: var(--text-primary);
            font-weight: 500;
        }}
        .node-edit-btn {{
            margin-top: 0.5rem;
            padding: 0.25rem 0.5rem;
            font-size: 0.7rem;
            background: var(--gold);
            color: var(--dark-bg);
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-weight: 600;
            font-family: inherit;
            transition: all 0.2s;
        }}
        .node-edit-btn:hover {{
            opacity: 0.9;
        }}
        .drawflow .connection .main-path {{
            stroke: var(--gold);
            stroke-width: 3px;
        }}
        .drawflow .drawflow-node .input,
        .drawflow .drawflow-node .output {{
            width: 14px;
            height: 14px;
            background: var(--dark-secondary);
            border: 2px solid var(--text-secondary);
        }}
        .drawflow .drawflow-node .output {{
            background: var(--gold);
            border-color: var(--gold);
        }}
        .drawflow .drawflow-node .input {{
            background: #48bb78;
            border-color: #48bb78;
        }}
        /* Context Menu */
        .context-menu {{
            display: none;
            position: fixed;
            background: var(--dark-secondary);
            border: 1px solid var(--border-subtle);
            border-radius: 6px;
            box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
            min-width: 180px;
            z-index: 1000;
            overflow: hidden;
        }}
        .context-menu.visible {{
            display: block;
        }}
        .context-menu-item {{
            padding: 0.6rem 1rem;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 0.5rem;
            font-size: 0.85rem;
            color: var(--text-primary);
            transition: all 0.2s;
        }}
        .context-menu-item:hover {{
            background: var(--gold-subtle);
            color: var(--gold);
        }}
        .context-menu-item .icon {{
            width: 16px;
            text-align: center;
            color: var(--gold);
        }}
        .context-menu-divider {{
            height: 1px;
            background: var(--border-subtle);
            margin: 0.25rem 0;
        }}
        /* Database/Filter Modal Form */
        .config-modal {{
            width: 400px;
        }}
        .config-modal .modal-body {{
            padding: 1.5rem;
        }}
        .form-group {{
            margin-bottom: 1rem;
        }}
        .form-group label {{
            display: block;
            font-size: 0.75rem;
            font-weight: 600;
            color: var(--text-secondary);
            margin-bottom: 0.4rem;
            text-transform: uppercase;
            letter-spacing: 0.06em;
        }}
        .form-group input,
        .form-group select {{
            width: 100%;
            padding: 0.6rem 0.75rem;
            border: 1px solid var(--border-subtle);
            border-radius: 4px;
            font-size: 0.9rem;
            background: var(--dark-bg);
            color: var(--text-primary);
            font-family: inherit;
        }}
        .form-group input:focus,
        .form-group select:focus {{
            outline: none;
            border-color: var(--gold);
            box-shadow: 0 0 0 3px rgba(250, 218, 54, 0.1);
        }}
        .form-group input::placeholder {{
            color: var(--text-dim);
        }}
        .form-actions {{
            display: flex;
            justify-content: flex-end;
            gap: 0.75rem;
            margin-top: 1.5rem;
        }}
        .btn-primary {{
            background: var(--gold);
            color: var(--dark-bg);
            border: none;
            padding: 0.5rem 1rem;
            border-radius: 4px;
            cursor: pointer;
            font-weight: 600;
            font-family: inherit;
            transition: all 0.2s;
        }}
        .btn-primary:hover {{
            opacity: 0.9;
        }}
        .btn-secondary {{
            background: transparent;
            color: var(--text-secondary);
            border: 1px solid var(--border-subtle);
            padding: 0.5rem 1rem;
            border-radius: 4px;
            cursor: pointer;
            font-family: inherit;
            transition: all 0.2s;
        }}
        .btn-secondary:hover {{
            border-color: var(--text-secondary);
            color: var(--text-primary);
        }}
    </style>
    <script>
        let map = null;
        let boundsRect = null;
        let selectedOrderId = null;

        // Uttag data embedded per order
        const uttagData = {json.dumps(uttag_json_data)};

        function showUttagModal(orderId) {{
            const data = uttagData[orderId];
            if (!data) {{
                alert('No uttag.json data available for this order');
                return;
            }}
            const modal = document.getElementById('modal-overlay');
            const title = document.getElementById('modal-title');
            const content = document.getElementById('modal-content');

            title.textContent = `uttag.json - ${{orderId.substring(0, 8)}}...`;
            content.textContent = JSON.stringify(data, null, 2);
            modal.classList.add('visible');
        }}

        function closeModal() {{
            document.getElementById('modal-overlay').classList.remove('visible');
        }}

        // Close modal on escape key
        document.addEventListener('keydown', function(e) {{
            if (e.key === 'Escape') closeModal();
        }});

        function initMap() {{
            map = L.map('map').setView([62.5, 17.5], 1);
            L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
                attribution: '&copy; OpenStreetMap contributors'
            }}).addTo(map);
        }}

        function showMapForOrder(orderId, bounds) {{
            const container = document.getElementById('map-container');
            const title = document.getElementById('map-title');

            // Update selection styling
            document.querySelectorAll('.order-row').forEach(row => row.classList.remove('selected-row'));
            const selectedRow = document.querySelector(`tr[onclick*="${{orderId}}"]`);
            if (selectedRow) selectedRow.classList.add('selected-row');

            if (!map) {{
                initMap();
            }}

            // Remove previous bounds rectangle
            if (boundsRect) {{
                map.removeLayer(boundsRect);
            }}

            // Parse bounds: sw_lat,sw_lng,ne_lat,ne_lng
            const [swLat, swLng, neLat, neLng] = bounds.split(',').map(Number);
            const sw = L.latLng(swLat, swLng);
            const ne = L.latLng(neLat, neLng);
            const latLngBounds = L.latLngBounds(sw, ne);

            // Draw rectangle
            boundsRect = L.rectangle(latLngBounds, {{
                color: '#4299e1',
                weight: 2,
                fillColor: '#4299e1',
                fillOpacity: 0.2
            }}).addTo(map);

            // Fit map to bounds with padding, limit max zoom
            map.fitBounds(latLngBounds, {{ padding: [50, 50], maxZoom: 4 }});

            // Show container and update title
            container.classList.add('visible');
            title.textContent = `Extent: ${{orderId.substring(0, 8)}}...`;
            selectedOrderId = orderId;

            // Invalidate size after display change
            setTimeout(() => map.invalidateSize(), 100);
        }}

        function closeMap() {{
            document.getElementById('map-container').classList.remove('visible');
            document.querySelectorAll('.order-row').forEach(row => row.classList.remove('selected-row'));
            selectedOrderId = null;
        }}

        function toggleOrder(orderId) {{
            const rows = document.querySelectorAll(`tr[data-order="${{orderId}}"]`);
            const icon = document.getElementById(`icon-${{orderId}}`);
            const isExpanding = rows[0]?.style.display === 'none';

            rows.forEach(row => {{
                row.style.display = isExpanding ? 'table-row' : 'none';
            }});

            if (icon) {{
                icon.textContent = isExpanding ? '-' : '+';
            }}

            // Close map if collapsing the selected order
            if (!isExpanding && selectedOrderId === orderId) {{
                closeMap();
            }}
        }}

        // Publish Modal Functions
        let currentPublishOrderId = null;
        const orderLayerData = {json.dumps({o.get("order_id", ""): [Path(f.get("title", "")).stem.replace("_sverige", "").replace("_sweden", "") for f in o.get("files", []) if f.get("title", "").endswith(".zip")] for o in orders})};

        function showPublishModal(orderId) {{
            currentPublishOrderId = orderId;
            document.getElementById('publish-order-id').textContent = orderId;

            // Reset state
            document.getElementById('publish-progress').classList.remove('visible');
            const statusEl = document.getElementById('publish-status');
            statusEl.className = 'publish-status';
            statusEl.textContent = '';
            statusEl.style.display = 'none';
            document.getElementById('publish-submit-btn').disabled = false;

            // Populate layer list using safe DOM methods
            const layerList = document.getElementById('publish-layer-list');
            layerList.replaceChildren(); // Clear existing content

            const layers = orderLayerData[orderId] || [];

            if (layers.length === 0) {{
                const p = document.createElement('p');
                p.style.color = 'var(--text-secondary)';
                p.textContent = 'No GeoPackage layers found in this order.';
                layerList.appendChild(p);
            }} else {{
                layers.forEach((layer) => {{
                    const label = document.createElement('label');
                    label.className = 'publish-layer-item';

                    const checkbox = document.createElement('input');
                    checkbox.type = 'checkbox';
                    checkbox.name = 'layer';
                    checkbox.value = layer;
                    checkbox.checked = true;

                    label.appendChild(checkbox);
                    label.appendChild(document.createTextNode(layer));
                    layerList.appendChild(label);
                }});
            }}

            document.getElementById('publish-modal').classList.add('visible');
        }}

        function closePublishModal() {{
            document.getElementById('publish-modal').classList.remove('visible');
            currentPublishOrderId = null;
        }}

        function executePublish() {{
            const dbConnection = document.getElementById('publish-db-connection').value.trim();
            if (!dbConnection) {{
                alert('Please enter a database connection string');
                return;
            }}

            // Get selected layers
            const checkboxes = document.querySelectorAll('#publish-layer-list input[type="checkbox"]:checked');
            const layers = Array.from(checkboxes).map(cb => cb.value);

            if (layers.length === 0) {{
                alert('Please select at least one layer to publish');
                return;
            }}

            // Show progress
            document.getElementById('publish-progress').classList.add('visible');
            document.getElementById('publish-submit-btn').disabled = true;
            document.getElementById('publish-progress-text').textContent = 'Publishing...';
            document.getElementById('publish-progress-fill').style.width = '0%';

            // Note: In a browser-only context, we can't actually run the Python publish.
            // This would require a running API server. Show instructions instead.
            setTimeout(() => {{
                document.getElementById('publish-progress').classList.remove('visible');
                const status = document.getElementById('publish-status');
                status.className = 'publish-status';

                // Build command safely using textContent
                const command = 'python download_order.py --publish ' + currentPublishOrderId +
                    ' --layers ' + layers.join(',') +
                    ' --db "' + dbConnection + '"';

                // Clear and rebuild status content safely
                status.replaceChildren();

                const title = document.createElement('p');
                const strong = document.createElement('strong');
                strong.textContent = 'To publish from command line:';
                title.appendChild(strong);
                status.appendChild(title);

                const pre = document.createElement('pre');
                pre.style.cssText = 'background: var(--dark-bg); padding: 0.5rem; border-radius: 4px; overflow-x: auto; margin-top: 0.5rem;';
                pre.textContent = command;
                status.appendChild(pre);

                const note = document.createElement('p');
                note.style.cssText = 'margin-top: 1rem; color: var(--text-secondary);';
                note.textContent = 'Or if you have the API server running, you can publish via the API.';
                status.appendChild(note);

                status.style.display = 'block';
                status.style.background = 'rgba(250, 218, 54, 0.1)';
                status.style.border = '1px solid var(--gold)';
                document.getElementById('publish-submit-btn').disabled = false;
            }}, 500);
        }}

        // Close publish modal on escape
        document.addEventListener('keydown', function(e) {{
            if (e.key === 'Escape') {{
                closePublishModal();
            }}
        }});
    </script>
</head>
<body>
    <header class="dtcc-header">
        <div class="logo">
            <img src="https://dtcc.chalmers.se/dtcc-logo.png" alt="DTCC Logo">
            <div class="logo-text">Digital Twin<br>Cities Centre</div>
        </div>
        <nav>
            <a href="https://dtcc.chalmers.se/projects">Projects</a>
            <a href="https://dtcc.chalmers.se/partners">Partners</a>
            <a href="https://dtcc.chalmers.se/about">About</a>
            <a href="https://github.com/dtcc-platform">GitHub</a>
        </nav>
    </header>

    <div class="container">
        <header class="page-header">
            <h1>Geotorget Downloads</h1>
            <p class="subtitle">Lantmateriet geodata downloads</p>
        </header>

        <div class="stats-grid">
            <div class="stat-card">
                <h3>Orders</h3>
                <div class="value">{total_orders}</div>
            </div>
            <div class="stat-card">
                <h3>Total Files</h3>
                <div class="value">{total_files}</div>
            </div>
            <div class="stat-card">
                <h3>Total Size</h3>
                <div class="value">{format_size(total_size)}</div>
            </div>
            <div class="stat-card">
                <h3>Total Objects</h3>
                <div class="value">{total_objects:,}</div>
            </div>
        </div>

        <div class="tab-bar">
            <button class="tab-btn active" data-tab="datasets" onclick="switchTab('datasets')">Datasets</button>
            <button class="tab-btn" data-tab="blueprint" onclick="switchTab('blueprint')">Blueprint</button>
        </div>

        <div class="tab-panel active" id="tab-datasets">
            <div id="map-container">
                <div class="map-header">
                    <h3 id="map-title">Data Extent</h3>
                    <button class="map-close" onclick="closeMap()">Close Map</button>
                </div>
                <div id="map"></div>
            </div>

            <div class="card">
                <div class="card-header">
                    <h2>Downloaded Orders</h2>
                </div>
                <table>
                    <thead>
                        <tr>
                            <th>Order ID / Filename</th>
                            <th>Date / Status</th>
                            <th>Files</th>
                            <th>Size</th>
                            <th>Objects</th>
                        </tr>
                    </thead>
                    <tbody>
                        {"".join(order_rows)}
                    </tbody>
                </table>
            </div>
        </div>

        <div class="tab-panel" id="tab-blueprint">
            <div class="blueprint-container">
                <div class="blueprint-toolbar">
                    <span class="toolbar-title">Blueprint Editor</span>
                    <div class="toolbar-actions">
                        <button onclick="BlueprintEditor.autoLayout()">Auto Layout</button>
                        <button onclick="BlueprintEditor.clearCanvas()">Clear</button>
                        <button onclick="BlueprintEditor.exportBlueprint()">Export JSON</button>
                    </div>
                </div>
                <div id="drawflow-canvas"></div>
            </div>
            <p style="margin-top: 1rem; color: var(--text-dim); font-size: 0.8rem; font-weight: 300;">
                Right-click on canvas to add Database or Filter nodes. Drag from output ports to input ports to connect.
            </p>
        </div>

        <div class="footer">
            Generated on {datetime.now().strftime("%Y-%m-%d %H:%M:%S")} |
            Data from <a href="https://geotorget.lantmateriet.se/">Lantmateriet Geotorget</a>
        </div>
    </div>

    <div id="modal-overlay" class="modal-overlay" onclick="if(event.target === this) closeModal()">
        <div class="modal">
            <div class="modal-header">
                <h3 id="modal-title">uttag.json</h3>
                <button class="modal-close" onclick="closeModal()">&times;</button>
            </div>
            <div class="modal-body">
                <pre id="modal-content"></pre>
            </div>
        </div>
    </div>

    <!-- Context Menu -->
    <div id="context-menu" class="context-menu">
        <div class="context-menu-item" data-action="add-database">
            <span class="icon">+</span> Add Database Node
        </div>
        <div class="context-menu-item" data-action="add-filter">
            <span class="icon">+</span> Add Filter Node
        </div>
        <div class="context-menu-divider"></div>
        <div class="context-menu-item" data-action="delete-node" style="display: none;">
            <span class="icon">x</span> Delete Node
        </div>
    </div>

    <!-- Database Config Modal -->
    <div id="database-modal" class="modal-overlay" onclick="if(event.target === this) BlueprintEditor.closeDatabaseModal()">
        <div class="modal config-modal">
            <div class="modal-header">
                <h3>Database Connection</h3>
                <button class="modal-close" onclick="BlueprintEditor.closeDatabaseModal()">&times;</button>
            </div>
            <div class="modal-body">
                <form id="database-form" onsubmit="event.preventDefault(); BlueprintEditor.saveDatabaseConfig();">
                    <div class="form-group">
                        <label>Connection Name</label>
                        <input type="text" name="connectionName" required>
                    </div>
                    <div class="form-group">
                        <label>Database Type</label>
                        <select name="dbType">
                            <option value="PostgreSQL">PostgreSQL</option>
                            <option value="MySQL">MySQL</option>
                            <option value="SQLite">SQLite</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>Host</label>
                        <input type="text" name="host" value="localhost">
                    </div>
                    <div class="form-group">
                        <label>Port</label>
                        <input type="number" name="port" value="5432">
                    </div>
                    <div class="form-group">
                        <label>Database</label>
                        <input type="text" name="database" required>
                    </div>
                    <div class="form-group">
                        <label>Username</label>
                        <input type="text" name="username">
                    </div>
                    <div class="form-group">
                        <label>Password</label>
                        <input type="password" name="password">
                    </div>
                    <div class="form-actions">
                        <button type="button" class="btn-secondary" onclick="BlueprintEditor.closeDatabaseModal()">Cancel</button>
                        <button type="submit" class="btn-primary">Save</button>
                    </div>
                </form>
            </div>
        </div>
    </div>

    <!-- Filter Config Modal -->
    <div id="filter-modal" class="modal-overlay" onclick="if(event.target === this) BlueprintEditor.closeFilterModal()">
        <div class="modal config-modal">
            <div class="modal-header">
                <h3>Filter Configuration</h3>
                <button class="modal-close" onclick="BlueprintEditor.closeFilterModal()">&times;</button>
            </div>
            <div class="modal-body">
                <form id="filter-form" onsubmit="event.preventDefault(); BlueprintEditor.saveFilterConfig();">
                    <div class="form-group">
                        <label>Filter Name</label>
                        <input type="text" name="filterName" value="Filter" required>
                    </div>
                    <div class="form-group">
                        <label>Field</label>
                        <input type="text" name="field" placeholder="e.g., objekttyp">
                    </div>
                    <div class="form-group">
                        <label>Operator</label>
                        <select name="operator">
                            <option value="=">=</option>
                            <option value="!=">!=</option>
                            <option value=">">&gt;</option>
                            <option value="<">&lt;</option>
                            <option value=">=">>=</option>
                            <option value="<="><=</option>
                            <option value="contains">contains</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>Value</label>
                        <input type="text" name="value" placeholder="e.g., 123">
                    </div>
                    <div class="form-actions">
                        <button type="button" class="btn-secondary" onclick="BlueprintEditor.closeFilterModal()">Cancel</button>
                        <button type="submit" class="btn-primary">Save</button>
                    </div>
                </form>
            </div>
        </div>
    </div>

    <!-- Publish to PostGIS Modal -->
    <div id="publish-modal" class="modal-overlay" onclick="if(event.target === this) closePublishModal()">
        <div class="modal" style="width: 500px;">
            <div class="modal-header">
                <h3>Publish to PostGIS</h3>
                <button class="modal-close" onclick="closePublishModal()">&times;</button>
            </div>
            <div class="modal-body publish-modal-content">
                <p>Order: <code id="publish-order-id"></code></p>
                <p style="margin-top: 0.5rem; color: var(--text-secondary); font-size: 0.85rem;">
                    This will extract GeoPackage layers and load them into PostGIS.
                    Requires a running PostgreSQL database with PostGIS extension.
                </p>

                <h4 style="margin-top: 1rem;">Select Layers</h4>
                <div id="publish-layer-list" class="publish-layer-list">
                    <!-- Layer checkboxes will be populated dynamically -->
                </div>

                <div class="form-group" style="margin-top: 1rem;">
                    <label>Database Connection</label>
                    <input type="text" id="publish-db-connection"
                           placeholder="postgresql://user:pass@localhost/geotorget"
                           style="width: 100%; padding: 0.5rem; font-family: monospace;">
                    <small style="color: var(--text-secondary);">Or set GEOTORGET_DB environment variable</small>
                </div>

                <div id="publish-progress" class="publish-progress">
                    <span id="publish-progress-text">Publishing...</span>
                    <div class="progress-bar">
                        <div id="publish-progress-fill" class="progress-bar-fill"></div>
                    </div>
                </div>

                <div id="publish-status" class="publish-status"></div>

                <div class="form-actions" style="margin-top: 1.5rem;">
                    <button type="button" class="btn-secondary" onclick="closePublishModal()">Cancel</button>
                    <button type="button" class="btn-primary" id="publish-submit-btn" onclick="executePublish()">
                        Publish to PostGIS
                    </button>
                </div>
            </div>
        </div>
    </div>

    <script>
        // Blueprint Editor - Note: This is a local tool where all data comes from
        // the user's own files and inputs, not from untrusted external sources.
        const BlueprintEditor = {{
            editor: null,
            orderData: {json.dumps(blueprint_orders)},
            contextMenuX: 0,
            contextMenuY: 0,
            contextMenuNodeId: null,
            editingNodeId: null,

            init: function() {{
                const container = document.getElementById('drawflow-canvas');
                if (!container) return;

                this.editor = new Drawflow(container);
                this.editor.reroute = true;
                this.editor.reroute_fix_curvature = true;
                this.editor.force_first_input = false;

                this.editor.start();

                if (!this.loadBlueprint()) {{
                    this.createOrderNodes();
                }}

                this.bindEvents();

                this.editor.on('nodeCreated', () => this.saveBlueprint());
                this.editor.on('nodeRemoved', () => this.saveBlueprint());
                this.editor.on('connectionCreated', () => this.saveBlueprint());
                this.editor.on('connectionRemoved', () => this.saveBlueprint());
                this.editor.on('nodeMoved', () => this.saveBlueprint());
            }},

            bindEvents: function() {{
                const canvas = document.getElementById('drawflow-canvas');
                const contextMenu = document.getElementById('context-menu');

                canvas.addEventListener('contextmenu', (e) => {{
                    e.preventDefault();
                    const nodeEl = e.target.closest('.drawflow-node');
                    const nodeId = nodeEl ? nodeEl.id.replace('node-', '') : null;
                    this.showContextMenu(e.clientX, e.clientY, nodeId);
                }});

                document.addEventListener('click', () => this.hideContextMenu());

                contextMenu.addEventListener('click', (e) => {{
                    const item = e.target.closest('.context-menu-item');
                    if (!item) return;

                    const action = item.dataset.action;
                    if (action === 'add-database') {{
                        this.addDatabaseNode(this.contextMenuX, this.contextMenuY);
                    }} else if (action === 'add-filter') {{
                        this.addFilterNode(this.contextMenuX, this.contextMenuY);
                    }} else if (action === 'delete-node' && this.contextMenuNodeId) {{
                        this.deleteNode(this.contextMenuNodeId);
                    }}
                    this.hideContextMenu();
                }});
            }},

            escapeHtml: function(text) {{
                const div = document.createElement('div');
                div.textContent = text;
                return div.textContent;
            }},

            createOrderNodes: function() {{
                const startY = 100;
                const spacing = 150;

                this.orderData.forEach((order, index) => {{
                    const orderId = this.escapeHtml(order.order_id.substring(0, 8));
                    const html = '<div class="title-box">Order</div>' +
                        '<div class="box"><div class="node-info">' +
                        '<div><span class="label">ID:</span> <span class="value">' + orderId + '...</span></div>' +
                        '<div><span class="label">Files:</span> <span class="value">' + order.file_count + '</span></div>' +
                        '<div><span class="label">Objects:</span> <span class="value">' + order.object_count.toLocaleString() + '</span></div>' +
                        '</div></div>';

                    this.editor.addNode(
                        'OrderNode', 0, 1, 100, startY + (index * spacing),
                        'order-node',
                        {{ orderId: order.order_id, fileCount: order.file_count, objectCount: order.object_count }},
                        html
                    );
                }});
            }},

            addDatabaseNode: function(x, y) {{
                const canvas = document.getElementById('drawflow-canvas');
                const rect = canvas.getBoundingClientRect();
                const posX = (x - rect.left) / this.editor.zoom - this.editor.precanvas.getBoundingClientRect().left / this.editor.zoom + this.editor.canvas_x / this.editor.zoom;
                const posY = (y - rect.top) / this.editor.zoom - this.editor.precanvas.getBoundingClientRect().top / this.editor.zoom + this.editor.canvas_y / this.editor.zoom;

                const nodeData = {{
                    connectionName: 'New Database',
                    dbType: 'PostgreSQL',
                    host: 'localhost',
                    port: 5432,
                    database: '',
                    username: '',
                    password: ''
                }};

                const html = this.getDatabaseNodeHtml(nodeData);

                const nodeId = this.editor.addNode(
                    'DatabaseNode', 1, 0, posX, posY,
                    'database-node', nodeData, html
                );

                this.editingNodeId = nodeId;
                this.showDatabaseModal(nodeId);
            }},

            getDatabaseNodeHtml: function(data) {{
                const name = this.escapeHtml(data.connectionName || 'New Database');
                const dbType = this.escapeHtml(data.dbType || 'PostgreSQL');
                const host = this.escapeHtml(data.host || 'localhost');
                const db = this.escapeHtml(data.database || '-');
                const nodeId = data.nodeId || 0;
                return '<div class="title-box">Database</div>' +
                    '<div class="box"><div class="node-info">' +
                    '<div><span class="label">Name:</span> <span class="value">' + name + '</span></div>' +
                    '<div><span class="label">Type:</span> <span class="value">' + dbType + '</span></div>' +
                    '<div><span class="label">Host:</span> <span class="value">' + host + '</span></div>' +
                    '<div><span class="label">DB:</span> <span class="value">' + db + '</span></div>' +
                    '</div><button class="node-edit-btn" onclick="event.stopPropagation(); BlueprintEditor.showDatabaseModal(' + nodeId + ')">Edit</button></div>';
            }},

            addFilterNode: function(x, y) {{
                const canvas = document.getElementById('drawflow-canvas');
                const rect = canvas.getBoundingClientRect();
                const posX = (x - rect.left) / this.editor.zoom - this.editor.precanvas.getBoundingClientRect().left / this.editor.zoom + this.editor.canvas_x / this.editor.zoom;
                const posY = (y - rect.top) / this.editor.zoom - this.editor.precanvas.getBoundingClientRect().top / this.editor.zoom + this.editor.canvas_y / this.editor.zoom;

                const nodeData = {{
                    filterName: 'Filter',
                    field: '',
                    operator: '=',
                    value: ''
                }};

                const html = this.getFilterNodeHtml(nodeData);

                const nodeId = this.editor.addNode(
                    'FilterNode', 1, 1, posX, posY,
                    'filter-node', nodeData, html
                );

                this.editingNodeId = nodeId;
                this.showFilterModal(nodeId);
            }},

            getFilterNodeHtml: function(data) {{
                const name = this.escapeHtml(data.filterName || 'Filter');
                const condition = data.field ?
                    this.escapeHtml(data.field + ' ' + data.operator + ' ' + data.value) :
                    'Not configured';
                const nodeId = data.nodeId || 0;
                return '<div class="title-box">Filter</div>' +
                    '<div class="box"><div class="node-info">' +
                    '<div><span class="label">Name:</span> <span class="value">' + name + '</span></div>' +
                    '<div><span class="label">Condition:</span> <span class="value">' + condition + '</span></div>' +
                    '</div><button class="node-edit-btn" onclick="event.stopPropagation(); BlueprintEditor.showFilterModal(' + nodeId + ')">Edit</button></div>';
            }},

            showContextMenu: function(x, y, nodeId) {{
                const menu = document.getElementById('context-menu');
                this.contextMenuX = x;
                this.contextMenuY = y;
                this.contextMenuNodeId = nodeId;

                const deleteItem = menu.querySelector('[data-action="delete-node"]');
                if (nodeId) {{
                    const nodeData = this.editor.getNodeFromId(nodeId);
                    deleteItem.style.display = (nodeData.name !== 'OrderNode') ? 'flex' : 'none';
                }} else {{
                    deleteItem.style.display = 'none';
                }}

                menu.style.left = x + 'px';
                menu.style.top = y + 'px';
                menu.classList.add('visible');
            }},

            hideContextMenu: function() {{
                document.getElementById('context-menu').classList.remove('visible');
            }},

            deleteNode: function(nodeId) {{
                this.editor.removeNodeId('node-' + nodeId);
            }},

            showDatabaseModal: function(nodeId) {{
                this.editingNodeId = nodeId;
                const node = this.editor.getNodeFromId(nodeId);
                const data = node ? node.data : {{}};

                const form = document.getElementById('database-form');
                form.connectionName.value = data.connectionName || 'New Database';
                form.dbType.value = data.dbType || 'PostgreSQL';
                form.host.value = data.host || 'localhost';
                form.port.value = data.port || 5432;
                form.database.value = data.database || '';
                form.username.value = data.username || '';
                form.password.value = '';

                document.getElementById('database-modal').classList.add('visible');
            }},

            closeDatabaseModal: function() {{
                document.getElementById('database-modal').classList.remove('visible');
                this.editingNodeId = null;
            }},

            saveDatabaseConfig: function() {{
                if (!this.editingNodeId) return;

                const form = document.getElementById('database-form');
                const nodeData = {{
                    nodeId: this.editingNodeId,
                    connectionName: form.connectionName.value,
                    dbType: form.dbType.value,
                    host: form.host.value,
                    port: parseInt(form.port.value) || 5432,
                    database: form.database.value,
                    username: form.username.value,
                    password: form.password.value
                }};

                this.editor.updateNodeDataFromId(this.editingNodeId, nodeData);
                this.updateNodeContent(this.editingNodeId, this.getDatabaseNodeHtml(nodeData));

                this.closeDatabaseModal();
                this.saveBlueprint();
            }},

            showFilterModal: function(nodeId) {{
                this.editingNodeId = nodeId;
                const node = this.editor.getNodeFromId(nodeId);
                const data = node ? node.data : {{}};

                const form = document.getElementById('filter-form');
                form.filterName.value = data.filterName || 'Filter';
                form.field.value = data.field || '';
                form.operator.value = data.operator || '=';
                form.value.value = data.value || '';

                document.getElementById('filter-modal').classList.add('visible');
            }},

            closeFilterModal: function() {{
                document.getElementById('filter-modal').classList.remove('visible');
                this.editingNodeId = null;
            }},

            saveFilterConfig: function() {{
                if (!this.editingNodeId) return;

                const form = document.getElementById('filter-form');
                const nodeData = {{
                    nodeId: this.editingNodeId,
                    filterName: form.filterName.value,
                    field: form.field.value,
                    operator: form.operator.value,
                    value: form.value.value
                }};

                this.editor.updateNodeDataFromId(this.editingNodeId, nodeData);
                this.updateNodeContent(this.editingNodeId, this.getFilterNodeHtml(nodeData));

                this.closeFilterModal();
                this.saveBlueprint();
            }},

            updateNodeContent: function(nodeId, html) {{
                const nodeEl = document.getElementById('node-' + nodeId);
                if (nodeEl) {{
                    const contentEl = nodeEl.querySelector('.drawflow_content_node');
                    if (contentEl) {{
                        contentEl.replaceChildren();
                        contentEl.insertAdjacentHTML('beforeend', html);
                    }}
                }}
            }},

            saveBlueprint: function() {{
                const state = {{
                    version: 1,
                    lastModified: new Date().toISOString(),
                    drawflow: this.editor.export()
                }};
                localStorage.setItem('lm-geotorget-blueprint', JSON.stringify(state));
            }},

            loadBlueprint: function() {{
                const saved = localStorage.getItem('lm-geotorget-blueprint');
                if (!saved) return false;

                try {{
                    const state = JSON.parse(saved);
                    if (state.version !== 1) return false;

                    this.editor.import(state.drawflow);
                    this.reconcileOrderNodes();
                    return true;
                }} catch (e) {{
                    console.error('Failed to load blueprint:', e);
                    return false;
                }}
            }},

            reconcileOrderNodes: function() {{
                const currentOrderIds = new Set(this.orderData.map(o => o.order_id));
                const exportData = this.editor.export();
                const homeModule = exportData.drawflow?.Home?.data || {{}};

                const canvasOrderIds = new Set();
                Object.values(homeModule).forEach(node => {{
                    if (node.name === 'OrderNode' && node.data?.orderId) {{
                        canvasOrderIds.add(node.data.orderId);
                    }}
                }});

                let yOffset = 100;
                this.orderData.forEach((order) => {{
                    if (!canvasOrderIds.has(order.order_id)) {{
                        const orderId = this.escapeHtml(order.order_id.substring(0, 8));
                        const html = '<div class="title-box">Order</div>' +
                            '<div class="box"><div class="node-info">' +
                            '<div><span class="label">ID:</span> <span class="value">' + orderId + '...</span></div>' +
                            '<div><span class="label">Files:</span> <span class="value">' + order.file_count + '</span></div>' +
                            '<div><span class="label">Objects:</span> <span class="value">' + order.object_count.toLocaleString() + '</span></div>' +
                            '</div></div>';

                        this.editor.addNode(
                            'OrderNode', 0, 1, 100, yOffset,
                            'order-node',
                            {{ orderId: order.order_id, fileCount: order.file_count, objectCount: order.object_count }},
                            html
                        );
                        yOffset += 150;
                    }}
                }});
            }},

            exportBlueprint: function() {{
                const state = {{
                    version: 1,
                    exportDate: new Date().toISOString(),
                    drawflow: this.editor.export()
                }};

                const blob = new Blob([JSON.stringify(state, null, 2)], {{ type: 'application/json' }});
                const url = URL.createObjectURL(blob);

                const a = document.createElement('a');
                a.href = url;
                a.download = 'geotorget-blueprint-' + new Date().toISOString().split('T')[0] + '.json';
                a.click();

                URL.revokeObjectURL(url);
            }},

            autoLayout: function() {{
                const exportData = this.editor.export();
                const nodes = exportData.drawflow?.Home?.data || {{}};

                const orderNodes = [];
                const filterNodes = [];
                const dbNodes = [];

                Object.entries(nodes).forEach(([id, node]) => {{
                    if (node.name === 'OrderNode') orderNodes.push(id);
                    else if (node.name === 'FilterNode') filterNodes.push(id);
                    else if (node.name === 'DatabaseNode') dbNodes.push(id);
                }});

                const spacing = 150;
                const startY = 100;

                orderNodes.forEach((id, index) => {{
                    this.editor.drawflow.drawflow.Home.data[id].pos_x = 100;
                    this.editor.drawflow.drawflow.Home.data[id].pos_y = startY + index * spacing;
                }});

                filterNodes.forEach((id, index) => {{
                    this.editor.drawflow.drawflow.Home.data[id].pos_x = 350;
                    this.editor.drawflow.drawflow.Home.data[id].pos_y = startY + index * spacing;
                }});

                dbNodes.forEach((id, index) => {{
                    this.editor.drawflow.drawflow.Home.data[id].pos_x = 600;
                    this.editor.drawflow.drawflow.Home.data[id].pos_y = startY + index * spacing;
                }});

                // Re-import to apply positions
                const updatedData = this.editor.export();
                this.editor.import(updatedData);
                this.saveBlueprint();
            }},

            clearCanvas: function() {{
                if (!confirm('Clear all nodes and connections? Order nodes will be recreated.')) return;

                this.editor.clear();
                this.createOrderNodes();
                this.saveBlueprint();
            }}
        }};

        // Tab switching
        function switchTab(tabId) {{
            document.querySelectorAll('.tab-btn').forEach(btn => {{
                btn.classList.toggle('active', btn.dataset.tab === tabId);
            }});
            document.querySelectorAll('.tab-panel').forEach(panel => {{
                panel.classList.toggle('active', panel.id === 'tab-' + tabId);
            }});

            if (tabId === 'blueprint' && !BlueprintEditor.editor) {{
                setTimeout(() => BlueprintEditor.init(), 100);
            }}
        }}
    </script>
</body>
</html>
"""

    dashboard_path = base_dir / "dashboard.html"
    with open(dashboard_path, "w", encoding="utf-8") as f:
        f.write(html)

    return dashboard_path


def download_order(order_id: str, base_dir: Path, parallel: int = 4) -> None:
    """Download all files for an order."""
    print(f"Fetching file list for order: {order_id}")

    try:
        files = get_file_list(order_id)
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            print(f"Error: Order '{order_id}' not found or has no delivery ready.")
        else:
            print(f"Error: {e}")
        sys.exit(1)
    except requests.exceptions.RequestException as e:
        print(f"Error connecting to Geotorget: {e}")
        sys.exit(1)

    if not files:
        print("No files found for this order.")
        sys.exit(0)

    # Create order-specific subdirectory
    order_dir = base_dir / order_id
    order_dir.mkdir(parents=True, exist_ok=True)

    total_size = sum(f.get("length", 0) for f in files)

    print(f"Found {len(files)} files ({format_size(total_size)} total)")
    print(f"Output directory: {order_dir}")
    print()

    successful = 0
    failed = 0
    results = []

    if HAS_TQDM:
        # Use tqdm for progress bar (works with parallel downloads)
        with tqdm(total=total_size, unit="B", unit_scale=True, desc="Downloading") as pbar:
            with ThreadPoolExecutor(max_workers=parallel) as executor:
                futures = {
                    executor.submit(download_file, f, order_dir, pbar): f["title"]
                    for f in files
                }

                for future in as_completed(futures):
                    title, success, message = future.result()
                    results.append((title, success, message))
                    if success:
                        successful += 1
                    else:
                        failed += 1
    else:
        # Fallback without tqdm
        print("(Install 'tqdm' for progress bars: pip install tqdm)")
        print()
        with ThreadPoolExecutor(max_workers=parallel) as executor:
            futures = {
                executor.submit(download_file, f, order_dir, None): f["title"]
                for f in files
            }

            for i, future in enumerate(as_completed(futures), 1):
                title, success, message = future.result()
                status = "OK" if success else "FAILED"
                print(f"[{i}/{len(files)}] {title}: {status} - {message}")
                results.append((title, success, message))
                if success:
                    successful += 1
                else:
                    failed += 1

    # Print summary of failures
    print()
    if failed > 0:
        print("Failed downloads:")
        for title, success, message in results:
            if not success:
                print(f"  - {title}: {message}")
        print()

    print(f"Done. {successful} files downloaded, {failed} failed.")
    print(f"Files saved to: {order_dir}")

    # Save order metadata for dashboard
    save_order_metadata(order_id, order_dir, files)

    # Generate combined dashboard
    dashboard_path = generate_dashboard(base_dir)
    print(f"Dashboard: file://{dashboard_path}")


# ==================== PostGIS Functions ====================

def init_postgis_database(db_connection: str) -> None:
    """Initialize PostGIS database with required schema."""
    try:
        # Import here to avoid requiring psycopg2 for basic operations
        from src.lm_geotorget.tiling.postgis_loader import PostGISLoader

        print("Initializing PostGIS database...")
        loader = PostGISLoader(db_connection)
        loader.init_database()
        loader.close()
        print("Database initialized successfully!")
        print("  - Created PostGIS extension")
        print("  - Created 'geotorget' schema")
        print("  - Created _metadata table")
    except ImportError:
        print("Error: psycopg2 is required. Install with: pip install psycopg2-binary")
        sys.exit(1)
    except Exception as e:
        print(f"Error initializing database: {e}")
        sys.exit(1)


def show_db_status(db_connection: str, downloads_dir: Path) -> None:
    """Show PostGIS database status and tables."""
    try:
        from src.lm_geotorget.tiling.postgis_loader import PostGISLoader

        loader = PostGISLoader(db_connection)

        print("PostGIS Database Status")
        print("=" * 50)

        # List tables
        tables = loader.list_tables()
        if not tables:
            print("No tables found in 'geotorget' schema.")
            print("\nTo publish data, run:")
            print("  python download_order.py --publish <order-id> --db <connection>")
            loader.close()
            return

        print(f"\nTables in 'geotorget' schema: {len(tables)}")
        print("-" * 50)

        total_features = 0
        for table in tables:
            print(f"  {table.name}")
            print(f"    Type: {table.geometry_type}")
            print(f"    Features: {table.feature_count:,}")
            print(f"    Columns: {', '.join(table.columns[:5])}" + ("..." if len(table.columns) > 5 else ""))
            total_features += table.feature_count

        print("-" * 50)
        print(f"Total features: {total_features:,}")

        # Get metadata
        metadata = loader.get_metadata()
        if metadata:
            order_ids = set(m["order_id"] for m in metadata)
            print(f"Published orders: {len(order_ids)}")

        loader.close()

    except ImportError:
        print("Error: psycopg2 is required. Install with: pip install psycopg2-binary")
        sys.exit(1)
    except Exception as e:
        print(f"Error connecting to database: {e}")
        sys.exit(1)


def publish_order_to_postgis(
    db_connection: str,
    downloads_dir: Path,
    order_id: str,
    layers: list[str] = None
) -> None:
    """Publish an order to PostGIS."""
    try:
        from src.lm_geotorget.tiling.processor import DataProcessor
        from src.lm_geotorget.tiling.detector import detect_order_type, is_publishable

        # Check order exists
        order_dir = downloads_dir / order_id
        if not order_dir.exists():
            print(f"Error: Order not found: {order_dir}")
            print("Available orders:")
            for d in downloads_dir.iterdir():
                if d.is_dir() and not d.name.startswith("."):
                    print(f"  {d.name}")
            sys.exit(1)

        # Detect data type
        detected = detect_order_type(order_dir)
        if not is_publishable(detected.data_type):
            print(f"Error: Data type '{detected.data_type.value}' is not supported for publishing.")
            print("Only GeoPackage (vector) data can be published to PostGIS.")
            sys.exit(1)

        print(f"Publishing order: {order_id}")
        print(f"  Data type: {detected.data_type.value}")
        print(f"  Layers: {', '.join(detected.layers) if detected.layers else 'all'}")
        print()

        # Create processor
        processor = DataProcessor(
            downloads_dir=downloads_dir,
            db_connection=db_connection
        )

        # Process with progress callback
        def progress(layer_name, current, total):
            print(f"  [{current}/{total}] Processing {layer_name}...")

        result = processor.process_order(
            order_id=order_id,
            layers=layers,
            progress_callback=progress
        )

        processor.close()

        # Print results
        print()
        if result.success:
            print("Publishing completed successfully!")
            print(f"  Total features: {result.total_features:,}")
            print(f"  Duration: {result.duration_seconds:.1f}s")
            print(f"  Layers processed: {len(result.layers_processed)}")
            for lr in result.layers_processed:
                status = "OK" if lr.success else f"FAILED: {lr.error}"
                print(f"    - {lr.layer_name}: {lr.feature_count:,} features [{status}]")
        else:
            print(f"Publishing failed: {result.error}")
            for lr in result.layers_processed:
                if not lr.success:
                    print(f"  - {lr.layer_name}: {lr.error}")
            sys.exit(1)

    except ImportError as e:
        print(f"Error: Missing dependency - {e}")
        print("Install with: pip install psycopg2-binary")
        sys.exit(1)
    except Exception as e:
        print(f"Error publishing: {e}")
        sys.exit(1)


def publish_all_orders(db_connection: str, downloads_dir: Path) -> None:
    """Publish all downloaded orders to PostGIS."""
    try:
        from src.lm_geotorget.tiling.processor import DataProcessor
        from src.lm_geotorget.tiling.detector import detect_order_type, is_publishable

        # Find all publishable orders
        publishable = []
        for order_dir in downloads_dir.iterdir():
            if order_dir.is_dir() and not order_dir.name.startswith("."):
                detected = detect_order_type(order_dir)
                if is_publishable(detected.data_type):
                    publishable.append(order_dir.name)

        if not publishable:
            print("No publishable orders found.")
            return

        print(f"Found {len(publishable)} publishable orders")
        print()

        # Create processor
        processor = DataProcessor(
            downloads_dir=downloads_dir,
            db_connection=db_connection
        )

        total_features = 0
        success_count = 0

        for i, order_id in enumerate(publishable, 1):
            print(f"[{i}/{len(publishable)}] Publishing {order_id}...")

            result = processor.process_order(order_id=order_id)

            if result.success:
                success_count += 1
                total_features += result.total_features
                print(f"  OK - {result.total_features:,} features in {result.duration_seconds:.1f}s")
            else:
                print(f"  FAILED - {result.error}")

        processor.close()

        print()
        print(f"Completed: {success_count}/{len(publishable)} orders")
        print(f"Total features: {total_features:,}")

    except ImportError as e:
        print(f"Error: Missing dependency - {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Download Lantmateriet Geotorget orders"
    )
    parser.add_argument(
        "order_id",
        nargs="?",
        help="Order ID (UUID format)"
    )
    parser.add_argument(
        "-o", "--output",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory (default: {DEFAULT_OUTPUT_DIR})"
    )
    parser.add_argument(
        "-p", "--parallel",
        type=int,
        default=4,
        help="Number of parallel downloads (default: 4)"
    )
    parser.add_argument(
        "-r", "--regenerate",
        action="store_true",
        help="Regenerate dashboard without downloading"
    )
    parser.add_argument(
        "-c", "--check",
        action="store_true",
        help="Check for updates on all downloaded orders"
    )

    # PostGIS and API commands
    parser.add_argument(
        "--init-db",
        action="store_true",
        help="Initialize PostGIS database with required schema"
    )
    parser.add_argument(
        "--publish",
        nargs="?",
        const="__current__",
        metavar="ORDER_ID",
        help="Publish order to PostGIS (use with order_id or --publish ORDER_ID)"
    )
    parser.add_argument(
        "--publish-all",
        action="store_true",
        help="Publish all downloaded orders to PostGIS"
    )
    parser.add_argument(
        "--layers",
        type=str,
        help="Comma-separated list of layers to publish (default: all)"
    )
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="PostgreSQL connection string (or set GEOTORGET_DB env var)"
    )
    parser.add_argument(
        "--db-status",
        action="store_true",
        help="Show PostGIS database status and tables"
    )

    args = parser.parse_args()

    if args.check:
        check_all_updates(args.output)
        return

    if args.regenerate:
        dashboard_path = generate_dashboard(args.output)
        print(f"Dashboard regenerated: file://{dashboard_path}")
        return

    # Get database connection string
    db_connection = args.db or os.environ.get("GEOTORGET_DB")

    # Handle PostGIS commands
    if args.init_db:
        if not db_connection:
            print("Error: Database connection required. Use --db or set GEOTORGET_DB")
            sys.exit(1)
        init_postgis_database(db_connection)
        return

    if args.db_status:
        if not db_connection:
            print("Error: Database connection required. Use --db or set GEOTORGET_DB")
            sys.exit(1)
        show_db_status(db_connection, args.output)
        return

    if args.publish_all:
        if not db_connection:
            print("Error: Database connection required. Use --db or set GEOTORGET_DB")
            sys.exit(1)
        publish_all_orders(db_connection, args.output)
        return

    if args.publish:
        if not db_connection:
            print("Error: Database connection required. Use --db or set GEOTORGET_DB")
            sys.exit(1)

        # Determine order ID to publish
        publish_order_id = args.publish
        if publish_order_id == "__current__":
            publish_order_id = args.order_id
        if not publish_order_id:
            publish_order_id = input("Enter order ID to publish: ").strip()

        if not publish_order_id:
            print("Error: Order ID is required for --publish")
            sys.exit(1)

        layers = args.layers.split(",") if args.layers else None
        publish_order_to_postgis(db_connection, args.output, publish_order_id, layers)
        return

    order_id = args.order_id
    if not order_id:
        order_id = input("Enter order ID: ").strip()

    if not order_id:
        print("Error: Order ID is required")
        sys.exit(1)

    download_order(order_id, args.output, args.parallel)


if __name__ == "__main__":
    main()
