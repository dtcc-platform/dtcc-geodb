# Martin Vector Tile Server Integration

## Overview

Integrate Martin tile server to serve PostGIS vector data as MVT tiles for improved MapLibre performance.

## Decisions

| Aspect | Decision |
|--------|----------|
| Martin installation | Provide instructions in README |
| Startup | Dashboard auto-starts Martin as subprocess |
| Port | 3000 (Martin default) |
| Scope | All tables in `geotorget` schema |
| Fallback | Keep GeoJSON endpoints for feature queries |

## Architecture

```
                    +------------------+
                    |   Dashboard      |
                    |   (Flask :5050)  |
                    +--------+---------+
                             |
              +--------------+--------------+
              |                             |
              v                             v
    +------------------+          +------------------+
    |  Martin (:3000)  |          |  Flask API       |
    |  Vector Tiles    |          |  GeoJSON/Meta    |
    +--------+---------+          +--------+---------+
              |                             |
              +-------------+---------------+
                            |
                            v
                   +------------------+
                   |    PostGIS       |
                   | (geotorget schema)|
                   +------------------+
```

## Implementation Tasks

### 1. Documentation - Martin Installation

Add to README.md:

```markdown
## Prerequisites

### Martin Tile Server

Martin is required for vector tile serving. Install via:

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
```

### 2. Martin Configuration File

Create `martin.yaml` in project root:

```yaml
# Martin configuration for Geotorget

# PostgreSQL connection
postgres:
  connection_string: ${DATABASE_URL}

  # Auto-discover all tables in geotorget schema
  auto_publish:
    tables:
      from_schemas: geotorget

  # Default tile settings
  default_srid: 4326

# Server settings
listen_addresses: '127.0.0.1:3000'

# CORS for local development
cors:
  allowed_origins:
    - http://localhost:5050
    - http://127.0.0.1:5050
```

### 3. Dashboard - Martin Process Management

**File:** `src/lm_geotorget/management/server.py`

Add Martin subprocess management:

```python
import subprocess
import shutil

class MartinManager:
    """Manages Martin tile server subprocess."""

    def __init__(self, db_connection: str, port: int = 3000):
        self.db_connection = db_connection
        self.port = port
        self.process = None

    def is_installed(self) -> bool:
        """Check if Martin is installed."""
        return shutil.which('martin') is not None

    def start(self) -> bool:
        """Start Martin server."""
        if not self.is_installed():
            return False

        if self.process and self.process.poll() is None:
            return True  # Already running

        # Build connection string for Martin
        # Martin expects: postgresql://user:pass@host/db
        env = os.environ.copy()
        env['DATABASE_URL'] = self.db_connection

        self.process = subprocess.Popen(
            ['martin', '--config', 'martin.yaml'],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

        # Wait briefly and check if started
        time.sleep(1)
        return self.process.poll() is None

    def stop(self):
        """Stop Martin server."""
        if self.process:
            self.process.terminate()
            self.process.wait(timeout=5)
            self.process = None

    def is_running(self) -> bool:
        """Check if Martin is running."""
        if not self.process:
            return False
        return self.process.poll() is None

    def get_catalog_url(self) -> str:
        """Get Martin catalog URL."""
        return f'http://127.0.0.1:{self.port}/catalog'

    def get_tile_url(self, table: str) -> str:
        """Get tile URL template for a table."""
        return f'http://127.0.0.1:{self.port}/{table}/{{z}}/{{x}}/{{y}}'
```

Integration in `create_management_app()`:

```python
def create_management_app(...):
    # ... existing code ...

    martin_manager = None

    @app.route('/api/martin/status')
    def martin_status():
        """Get Martin server status."""
        if not martin_manager:
            return jsonify({
                'installed': MartinManager('').is_installed(),
                'running': False,
                'error': 'Database not configured'
            })

        return jsonify({
            'installed': martin_manager.is_installed(),
            'running': martin_manager.is_running(),
            'catalog_url': martin_manager.get_catalog_url() if martin_manager.is_running() else None
        })

    @app.route('/api/martin/start', methods=['POST'])
    def start_martin():
        """Start Martin server."""
        nonlocal martin_manager

        if not app.config['db_connection']:
            return jsonify({'error': 'Database not configured'}), 400

        if not martin_manager:
            martin_manager = MartinManager(app.config['db_connection'])

        if not martin_manager.is_installed():
            return jsonify({
                'error': 'Martin not installed. See README for installation instructions.'
            }), 400

        if martin_manager.start():
            return jsonify({'status': 'started', 'catalog_url': martin_manager.get_catalog_url()})
        else:
            return jsonify({'error': 'Failed to start Martin'}), 500

    @app.route('/api/martin/stop', methods=['POST'])
    def stop_martin():
        """Stop Martin server."""
        if martin_manager:
            martin_manager.stop()
        return jsonify({'status': 'stopped'})

    # Auto-start Martin when DB is configured
    def try_start_martin():
        nonlocal martin_manager
        if app.config['db_connection'] and not martin_manager:
            martin_manager = MartinManager(app.config['db_connection'])
            if martin_manager.is_installed():
                martin_manager.start()

    # ... rest of app setup ...
```

### 4. Frontend - Vector Tile Sources

**File:** `src/lm_geotorget/management/server.py` (JavaScript section)

Update MapViewer to use vector tiles:

```javascript
var MapViewer = {
    // ... existing properties ...
    martinUrl: 'http://127.0.0.1:3000',
    useMartinTiles: false,

    init: function() {
        // ... existing init ...

        // Check Martin status
        this.checkMartinStatus();
    },

    checkMartinStatus: function() {
        var self = this;
        fetch('/api/martin/status')
            .then(function(r) { return r.json(); })
            .then(function(data) {
                self.useMartinTiles = data.running;
                if (data.running) {
                    console.log('Martin available, using vector tiles');
                } else {
                    console.log('Martin not available, using GeoJSON');
                }
            });
    },

    addLayerToMap: function(layerName, geojson, color) {
        var self = this;

        if (this.useMartinTiles) {
            this.addVectorTileLayer(layerName, color);
        } else {
            this.addGeoJSONLayer(layerName, geojson, color);
        }
    },

    addVectorTileLayer: function(layerName, color) {
        var map = this.map;
        var sourceId = 'src-' + layerName;
        var layerId = 'layer-' + layerName;

        // Add vector tile source
        if (!map.getSource(sourceId)) {
            map.addSource(sourceId, {
                type: 'vector',
                tiles: [this.martinUrl + '/' + layerName + '/{z}/{x}/{y}'],
                minzoom: 0,
                maxzoom: 14
            });
        }

        // Get layer info for geometry type
        var layerInfo = this.layers[layerName];
        var geomType = layerInfo ? layerInfo.info.geometry_type : 'Polygon';

        // Add appropriate layer based on geometry type
        if (geomType.includes('Polygon')) {
            map.addLayer({
                id: layerId + '-fill',
                type: 'fill',
                source: sourceId,
                'source-layer': layerName,
                paint: {
                    'fill-color': color,
                    'fill-opacity': 0.3
                }
            });
            map.addLayer({
                id: layerId + '-outline',
                type: 'line',
                source: sourceId,
                'source-layer': layerName,
                paint: {
                    'line-color': color,
                    'line-width': 1.5
                }
            });
        } else if (geomType.includes('Line')) {
            map.addLayer({
                id: layerId + '-line',
                type: 'line',
                source: sourceId,
                'source-layer': layerName,
                paint: {
                    'line-color': color,
                    'line-width': 2
                }
            });
        } else if (geomType.includes('Point')) {
            map.addLayer({
                id: layerId + '-circle',
                type: 'circle',
                source: sourceId,
                'source-layer': layerName,
                paint: {
                    'circle-color': color,
                    'circle-radius': 6,
                    'circle-stroke-color': '#fff',
                    'circle-stroke-width': 1
                }
            });
        }
    },

    // Keep existing addGeoJSONLayer for fallback
    addGeoJSONLayer: function(layerName, geojson, color) {
        // ... existing GeoJSON layer code ...
    }
};
```

### 5. UI - Martin Status Indicator

Add to status bar:

```html
<div class="status-item">
    <span class="status-dot" id="martinStatus"></span>
    <span id="martinStatusText">Tiles: checking...</span>
</div>
```

JavaScript to update status:

```javascript
function checkMartinStatus() {
    fetch('/api/martin/status')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            var dot = document.getElementById('martinStatus');
            var text = document.getElementById('martinStatusText');

            if (!data.installed) {
                dot.className = 'status-dot';
                text.textContent = 'Tiles: Martin not installed';
            } else if (data.running) {
                dot.className = 'status-dot connected';
                text.textContent = 'Tiles: Martin running';
            } else {
                dot.className = 'status-dot disconnected';
                text.textContent = 'Tiles: Martin stopped';
            }
        });
}
```

### 6. Auto-start on Database Connect

When database connection is established, attempt to start Martin:

```javascript
function saveDbConfig() {
    // ... existing code ...

    fetch('/api/config', { method: 'POST', ... })
        .then(function() {
            checkDbStatus();
            loadOrders();

            // Try to start Martin
            fetch('/api/martin/start', { method: 'POST' })
                .then(function() { checkMartinStatus(); });
        });
}
```

## File Changes Summary

| File | Changes |
|------|---------|
| `README.md` | Add Martin installation instructions |
| `martin.yaml` | New - Martin configuration |
| `server.py` | Add MartinManager class, API endpoints, auto-start |
| `server.py` (JS) | Update MapViewer for vector tiles, add status indicator |

## Testing

1. Install Martin: `brew install martin` (macOS)
2. Start dashboard with database configured
3. Verify Martin auto-starts (check `/api/martin/status`)
4. Toggle a layer - should load vector tiles instead of GeoJSON
5. Check network tab - requests to `:3000/{table}/{z}/{x}/{y}`

## Rollback

If Martin not available:
- Dashboard continues working with GeoJSON fallback
- No vector tiles, same behavior as before
- Status shows "Martin not installed"
