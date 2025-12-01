# Geotorget Download Client

A standalone Python client for downloading Lantmateriet Geotorget orders with an interactive dashboard and visual blueprint editor.

## Features

- **Download Orders**: Download geodata files from Lantmateriet Geotorget using order IDs
- **Parallel Downloads**: Configurable parallel downloads with progress bars
- **Update Checking**: Check subscribed orders for new data releases
- **Interactive Dashboard**: Auto-generated HTML dashboard with DTCC styling
- **Blueprint Editor**: Visual node-based editor for mapping data flows to SQL databases

## Requirements

```bash
pip install requests tqdm
```

Optional (for accurate coordinate transformation):
```bash
pip install pyproj
```

### Martin Tile Server (Optional)

Martin is an optional tile server that improves map performance by serving PostGIS data as vector tiles. The dashboard will auto-start Martin if installed.

**macOS (Homebrew):**
```bash
brew install martin
```

**Linux (pre-built binary):**
```bash
curl -LO https://github.com/maplibre/martin/releases/latest/download/martin-x86_64-unknown-linux-gnu.tar.gz
tar -xzf martin-x86_64-unknown-linux-gnu.tar.gz
sudo mv martin /usr/local/bin/
```

**Docker:**
```bash
docker pull ghcr.io/maplibre/martin
```

Verify installation:
```bash
martin --version
```

When Martin is installed and a database is configured, the dashboard will automatically start Martin on port 3000. The status bar will show "Tiles: Martin running" when active. If Martin is not installed, the dashboard falls back to serving GeoJSON directly (slower for large datasets).

## Usage

### Download an Order

```bash
python download_order.py <order-id>
```

Example:
```bash
python download_order.py fe76535a-a4fd-45e3-9a16-1d7464f0bd32
```

### Options

```
python download_order.py [order-id] [options]

Arguments:
  order-id              Order ID (UUID format)

Options:
  -o, --output PATH     Output directory (default: ~/Downloads/geotorget)
  -p, --parallel N      Number of parallel downloads (default: 4)
  -r, --regenerate      Regenerate dashboard without downloading
  -c, --check           Check all orders for updates
```

### Check for Updates

```bash
python download_order.py --check
```

### Regenerate Dashboard

```bash
python download_order.py --regenerate
```

## Dashboard

The dashboard is automatically generated after downloads at `<output-dir>/dashboard.html`.

### Datasets Tab
- View all downloaded orders
- Expand orders to see individual files
- Show data extent on interactive map
- View raw `uttag.json` metadata

### Blueprint Tab
- Visual node-based editor for data pipelines
- **Order Nodes** (gold): Auto-generated from downloads
- **Database Nodes** (green): Configure SQL database connections
- **Filter Nodes** (purple): Add data filtering conditions
- Drag connections between nodes
- Auto-saves to browser localStorage
- Export blueprints as JSON

#### Blueprint Controls
- **Right-click**: Add Database or Filter nodes
- **Drag**: Move nodes or create connections
- **Auto Layout**: Arrange nodes in columns
- **Clear**: Reset canvas (preserves order nodes)
- **Export JSON**: Download blueprint configuration

## Output Structure

```
<output-dir>/
├── dashboard.html           # Interactive dashboard
├── <order-id-1>/
│   ├── order_metadata.json  # Download metadata
│   ├── uttag.json           # Geotorget metadata
│   ├── *.zip                # Downloaded data files
│   └── ...
└── <order-id-2>/
    └── ...
```

## License

MIT
