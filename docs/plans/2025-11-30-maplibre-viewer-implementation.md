# MapLibre GL JS Feature Viewer Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add an interactive MapLibre GL JS map to the dashboard that displays PostGIS features with layer toggle controls and click popups.

**Architecture:** Modify the `generate_dashboard()` function in `download_order.py` to produce a split-view layout. The left panel contains the existing order/file list with layer toggle checkboxes. The right panel contains a MapLibre GL JS map that loads features on-demand from the existing FastAPI endpoints. Layer visibility state is persisted in localStorage.

**Tech Stack:** MapLibre GL JS (CDN), existing FastAPI `/api/layers` endpoints, vanilla JavaScript

---

## Task 1: Add MapLibre GL JS Dependencies

**Files:**
- Modify: `download_order.py:504-507` (CDN script tags)

**Step 1: Add MapLibre CSS and JS CDN links**

Replace the existing Leaflet CDN imports with MapLibre (keep Leaflet for now as fallback, we'll remove later):

In the `<head>` section around line 504, add after the existing scripts:

```python
    <link href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css" rel="stylesheet" />
    <script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>
```

**Step 2: Verify the dashboard still generates**

Run: `python download_order.py --regenerate`
Expected: Dashboard regenerates without errors, opens in browser

**Step 3: Commit**

```
feat: add MapLibre GL JS CDN dependencies
```

---

## Task 2: Create Split-View Layout CSS

**Files:**
- Modify: `download_order.py` (CSS section around lines 508-830)

**Step 1: Add split-view CSS**

Add the following CSS rules in the `<style>` section:

```css
.split-view {{
    display: flex;
    gap: 1.5rem;
    min-height: 600px;
}}
.split-left {{
    flex: 0 0 40%;
    max-width: 40%;
    overflow-y: auto;
    max-height: 80vh;
}}
.split-right {{
    flex: 1;
    min-width: 0;
    position: relative;
}}
#maplibre-map {{
    width: 100%;
    height: 100%;
    min-height: 500px;
    border-radius: 8px;
    border: 1px solid var(--border-subtle);
}}
.layer-toggle {{
    display: inline-flex;
    align-items: center;
    gap: 0.5rem;
    margin-right: 0.5rem;
}}
.layer-toggle input[type="checkbox"] {{
    width: 16px;
    height: 16px;
    cursor: pointer;
}}
.layer-color-swatch {{
    width: 12px;
    height: 12px;
    border-radius: 2px;
    display: inline-block;
}}
.layer-loading {{
    display: inline-block;
    width: 16px;
    height: 16px;
    border: 2px solid var(--border-subtle);
    border-top-color: var(--gold);
    border-radius: 50%;
    animation: spin 1s linear infinite;
}}
@keyframes spin {{
    to {{ transform: rotate(360deg); }}
}}
@media (max-width: 1000px) {{
    .split-view {{
        flex-direction: column;
    }}
    .split-left {{
        flex: none;
        max-width: 100%;
        max-height: 50vh;
    }}
    .split-right {{
        min-height: 400px;
    }}
}}
```

**Step 2: Verify CSS compiles into dashboard**

Run: `python download_order.py --regenerate`
Expected: Dashboard regenerates, CSS is present in output

**Step 3: Commit**

```
feat: add split-view CSS for MapLibre layout
```

---

## Task 3: Restructure Datasets Tab to Split-View HTML

**Files:**
- Modify: `download_order.py:1536-1564` (Datasets tab panel HTML)

**Step 1: Wrap existing content in split-view layout**

Change the `tab-datasets` panel structure from:

```html
<div class="tab-panel active" id="tab-datasets">
    <div id="map-container">...</div>
    <div class="card">...</div>
</div>
```

To:

```html
<div class="tab-panel active" id="tab-datasets">
    <div class="split-view">
        <div class="split-left">
            <div class="card">
                <!-- existing table content -->
            </div>
        </div>
        <div class="split-right">
            <div id="maplibre-map"></div>
            <div id="map-status" style="position: absolute; top: 10px; left: 10px; background: var(--dark-card); padding: 0.5rem 1rem; border-radius: 4px; font-size: 0.8rem; display: none;"></div>
        </div>
    </div>
</div>
```

Note: Remove the old `#map-container` div (Leaflet map) - we'll replace it entirely.

**Step 2: Verify layout renders**

Run: `python download_order.py --regenerate`
Expected: Dashboard shows split layout, table on left, empty map area on right

**Step 3: Commit**

```
feat: restructure Datasets tab to split-view layout
```

---

## Task 4: Initialize MapLibre Map

**Files:**
- Modify: `download_order.py` (JavaScript section, around line 1250+)

**Step 1: Add MapLibre initialization code**

Add a new JavaScript section for the map. Find the existing `<script>` section and add:

```javascript
// MapLibre Feature Viewer
const MapViewer = {
    map: null,
    layers: {},          // layer name -> { source, visible, color }
    apiBase: '',         // Set from server or default
    loadingLayers: new Set(),

    init: function() {
        // Detect API base URL (same origin or configured)
        this.apiBase = window.GEOTORGET_API_BASE || '';

        this.map = new maplibregl.Map({
            container: 'maplibre-map',
            style: {
                version: 8,
                sources: {
                    'osm': {
                        type: 'raster',
                        tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
                        tileSize: 256,
                        attribution: '&copy; OpenStreetMap contributors'
                    }
                },
                layers: [{
                    id: 'osm-tiles',
                    type: 'raster',
                    source: 'osm',
                    minzoom: 0,
                    maxzoom: 19
                }]
            },
            center: [17, 62],  // Sweden
            zoom: 4
        });

        this.map.addControl(new maplibregl.NavigationControl());

        this.map.on('load', () => {
            this.discoverLayers();
        });

        this.map.on('moveend', () => {
            this.reloadVisibleLayers();
        });
    },

    hashColor: function(str) {
        let hash = 0;
        for (let i = 0; i < str.length; i++) {
            hash = str.charCodeAt(i) + ((hash << 5) - hash);
        }
        const h = Math.abs(hash) % 360;
        return 'hsl(' + h + ', 70%, 50%)';
    },

    discoverLayers: async function() {
        try {
            const response = await fetch(this.apiBase + '/api/layers');
            if (!response.ok) {
                this.showStatus('API not available - start server with: python serve_api.py --db ...');
                return;
            }
            const layers = await response.json();
            this.updateLayerToggles(layers);
        } catch (e) {
            this.showStatus('API not available - start server with: python serve_api.py --db ...');
        }
    },

    updateLayerToggles: function(apiLayers) {
        // Map API layers by source_order to match with order rows
        const layersByOrder = {};
        apiLayers.forEach(layer => {
            // We'll need to fetch layer detail to get source_order
            // For now, just store all layers
            this.layers[layer.name] = {
                info: layer,
                visible: false,
                color: this.hashColor(layer.name)
            };
        });

        // Update UI checkboxes
        this.renderLayerToggles();
    },

    renderLayerToggles: function() {
        // Add toggle checkboxes to file rows that match published layers
        document.querySelectorAll('.file-row').forEach(row => {
            const firstCell = row.querySelector('td:first-child');
            const filename = firstCell ? firstCell.textContent.trim() : '';
            if (!filename || filename === 'uttag.json') return;

            // Check if this file has a corresponding layer
            const layerName = filename.replace('.gpkg', '').toLowerCase().replace(/[^a-z0-9]/g, '_');
            const layerInfo = this.layers[layerName];

            if (layerInfo && firstCell && !firstCell.querySelector('.layer-toggle')) {
                const toggle = document.createElement('label');
                toggle.className = 'layer-toggle';

                const checkbox = document.createElement('input');
                checkbox.type = 'checkbox';
                checkbox.dataset.layer = layerName;
                checkbox.addEventListener('change', () => {
                    MapViewer.toggleLayer(layerName, checkbox.checked);
                });

                const swatch = document.createElement('span');
                swatch.className = 'layer-color-swatch';
                swatch.style.background = layerInfo.color;

                toggle.appendChild(checkbox);
                toggle.appendChild(swatch);
                firstCell.insertBefore(toggle, firstCell.firstChild);
            }
        });
    },

    showStatus: function(msg) {
        const el = document.getElementById('map-status');
        if (el) {
            el.textContent = msg;
            el.style.display = msg ? 'block' : 'none';
        }
    },

    toggleLayer: async function(layerName, visible) {
        // Placeholder - implemented in Task 6
    },

    reloadVisibleLayers: function() {
        // Placeholder - implemented in Task 7
    }
};

// Initialize when DOM ready
document.addEventListener('DOMContentLoaded', function() {
    if (document.getElementById('maplibre-map')) {
        MapViewer.init();
    }
});
```

**Step 2: Verify map initializes**

Run: `python download_order.py --regenerate`
Open dashboard in browser
Expected: Map appears with OSM tiles, centered on Sweden

**Step 3: Commit**

```
feat: initialize MapLibre map with OSM basemap
```

---

## Task 5: Layer Discovery and Toggle UI

**Files:**
- Modify: `download_order.py` (JavaScript MapViewer object)

**Step 1: Enhance layer discovery to fetch source_order**

Update the `discoverLayers` and `updateLayerToggles` methods:

```javascript
discoverLayers: async function() {
    try {
        const response = await fetch(this.apiBase + '/api/layers');
        if (!response.ok) {
            this.showStatus('API not available - start server with: python serve_api.py --db ...');
            return;
        }
        const layers = await response.json();

        // Fetch detail for each layer to get source_order
        for (const layer of layers) {
            try {
                const detailRes = await fetch(this.apiBase + '/api/layers/' + layer.name);
                if (detailRes.ok) {
                    const detail = await detailRes.json();
                    layer.source_order = detail.source_order;
                    layer.bbox = detail.bbox;
                }
            } catch (e) {
                // Continue without detail
            }
        }

        this.updateLayerToggles(layers);
        this.showStatus('');
    } catch (e) {
        this.showStatus('API not available - start server with: python serve_api.py --db ...');
    }
},

updateLayerToggles: function(apiLayers) {
    apiLayers.forEach(layer => {
        this.layers[layer.name] = {
            info: layer,
            visible: false,
            color: this.hashColor(layer.name),
            sourceOrder: layer.source_order
        };
    });

    // Restore visibility from localStorage
    const savedState = localStorage.getItem('mapviewer_layers');
    if (savedState) {
        try {
            const visible = JSON.parse(savedState);
            visible.forEach(name => {
                if (this.layers[name]) {
                    this.layers[name].visible = true;
                }
            });
        } catch (e) {}
    }

    this.renderLayerToggles();

    // Load any previously visible layers
    Object.entries(this.layers).forEach(([name, layer]) => {
        if (layer.visible) {
            this.toggleLayer(name, true, true); // skipSave=true
        }
    });
},

saveLayerState: function() {
    const visible = Object.entries(this.layers)
        .filter(([_, l]) => l.visible)
        .map(([name, _]) => name);
    localStorage.setItem('mapviewer_layers', JSON.stringify(visible));
},
```

**Step 2: Update renderLayerToggles to match by source_order**

```javascript
renderLayerToggles: function() {
    // Group layers by source order
    const layersByOrder = {};
    Object.entries(this.layers).forEach(([name, layer]) => {
        const orderId = layer.sourceOrder || 'unknown';
        if (!layersByOrder[orderId]) layersByOrder[orderId] = [];
        layersByOrder[orderId].push({ name, ...layer });
    });

    // Add toggle checkboxes to matching order rows
    document.querySelectorAll('.order-row').forEach(row => {
        const orderIdCell = row.querySelector('td:first-child code');
        if (!orderIdCell) return;

        const shortId = orderIdCell.textContent.replace('...', '');

        // Find matching layers
        Object.entries(layersByOrder).forEach(([orderId, layers]) => {
            if (orderId.startsWith(shortId)) {
                // Add indicator that layers are available
                const cell = row.querySelector('td:first-child');
                if (cell && !cell.querySelector('.layers-available')) {
                    const badge = document.createElement('span');
                    badge.className = 'layers-available';
                    badge.style.cssText = 'margin-left: 0.5rem; font-size: 0.7rem; color: var(--gold);';
                    badge.textContent = '[' + layers.length + ' layer' + (layers.length > 1 ? 's' : '') + ']';
                    cell.appendChild(badge);
                }
            }
        });
    });

    // Add toggles to file rows
    document.querySelectorAll('.file-row').forEach(row => {
        const orderId = row.dataset.order;
        const firstCell = row.querySelector('td:first-child');
        const filename = firstCell ? firstCell.textContent.trim() : '';
        if (!filename || filename === 'uttag.json') return;

        // Find layer matching this file
        const matchingLayer = Object.entries(this.layers).find(([name, layer]) => {
            return layer.sourceOrder === orderId &&
                   (filename.toLowerCase().includes(name) || name.includes(filename.replace('.gpkg', '').toLowerCase()));
        });

        if (matchingLayer && firstCell && !firstCell.querySelector('.layer-toggle')) {
            const [layerName, layerInfo] = matchingLayer;

            const toggle = document.createElement('label');
            toggle.className = 'layer-toggle';

            const checkbox = document.createElement('input');
            checkbox.type = 'checkbox';
            checkbox.dataset.layer = layerName;
            checkbox.checked = layerInfo.visible;
            checkbox.addEventListener('change', () => {
                MapViewer.toggleLayer(layerName, checkbox.checked);
            });

            const swatch = document.createElement('span');
            swatch.className = 'layer-color-swatch';
            swatch.style.background = layerInfo.color;

            toggle.appendChild(checkbox);
            toggle.appendChild(swatch);
            firstCell.insertBefore(toggle, firstCell.firstChild);
        }
    });
},
```

**Step 3: Verify toggles appear for published layers**

Run API server: `python serve_api.py --db "postgresql://..." `
Run: `python download_order.py --regenerate`
Open dashboard
Expected: File rows for published layers show checkbox toggles with color swatches

**Step 4: Commit**

```
feat: add layer toggle checkboxes to file rows
```

---

## Task 6: Implement Layer Loading on Toggle

**Files:**
- Modify: `download_order.py` (JavaScript MapViewer.toggleLayer)

**Step 1: Implement toggleLayer method**

```javascript
toggleLayer: async function(layerName, visible, skipSave) {
    const layer = this.layers[layerName];
    if (!layer) return;

    layer.visible = visible;
    if (!skipSave) this.saveLayerState();

    if (visible) {
        await this.loadLayerFeatures(layerName);
    } else {
        this.removeLayerFromMap(layerName);
    }
},

loadLayerFeatures: async function(layerName) {
    const layer = this.layers[layerName];
    if (!layer) return;

    // Show loading state
    this.setLayerLoading(layerName, true);

    try {
        const bounds = this.map.getBounds();
        const bbox = [
            bounds.getWest(),
            bounds.getSouth(),
            bounds.getEast(),
            bounds.getNorth()
        ].join(',');

        const url = this.apiBase + '/api/layers/' + layerName + '/features?bbox=' + bbox + '&limit=5000';
        const response = await fetch(url);

        if (!response.ok) {
            throw new Error('Failed to load layer: ' + response.status);
        }

        const geojson = await response.json();
        this.addLayerToMap(layerName, geojson, layer.color);

    } catch (e) {
        console.error('Error loading layer:', e);
        this.showStatus('Error loading ' + layerName + ': ' + e.message);
        setTimeout(() => this.showStatus(''), 3000);
    } finally {
        this.setLayerLoading(layerName, false);
    }
},

setLayerLoading: function(layerName, loading) {
    const checkbox = document.querySelector('input[data-layer="' + layerName + '"]');
    if (!checkbox) return;

    const toggle = checkbox.closest('.layer-toggle');
    if (!toggle) return;

    if (loading) {
        this.loadingLayers.add(layerName);
        checkbox.style.display = 'none';
        if (!toggle.querySelector('.layer-loading')) {
            const spinner = document.createElement('span');
            spinner.className = 'layer-loading';
            toggle.insertBefore(spinner, checkbox);
        }
    } else {
        this.loadingLayers.delete(layerName);
        checkbox.style.display = '';
        const spinner = toggle.querySelector('.layer-loading');
        if (spinner) spinner.remove();
    }
},

addLayerToMap: function(layerName, geojson, color) {
    const sourceId = 'source-' + layerName;

    // Remove existing if present
    this.removeLayerFromMap(layerName);

    // Add source
    this.map.addSource(sourceId, {
        type: 'geojson',
        data: geojson
    });

    // Detect geometry type from first feature
    const firstFeature = geojson.features && geojson.features[0];
    const geomType = firstFeature && firstFeature.geometry ? firstFeature.geometry.type : 'Point';

    // Add appropriate layer based on geometry type
    if (geomType.includes('Polygon')) {
        this.map.addLayer({
            id: layerName + '-fill',
            type: 'fill',
            source: sourceId,
            paint: {
                'fill-color': color,
                'fill-opacity': 0.3
            }
        });
        this.map.addLayer({
            id: layerName + '-outline',
            type: 'line',
            source: sourceId,
            paint: {
                'line-color': color,
                'line-width': 1.5
            }
        });
    } else if (geomType.includes('Line')) {
        this.map.addLayer({
            id: layerName + '-line',
            type: 'line',
            source: sourceId,
            paint: {
                'line-color': color,
                'line-width': 2.5
            }
        });
    } else {
        // Point
        this.map.addLayer({
            id: layerName + '-circle',
            type: 'circle',
            source: sourceId,
            paint: {
                'circle-color': color,
                'circle-radius': 6,
                'circle-stroke-color': '#ffffff',
                'circle-stroke-width': 1.5
            }
        });
    }

    // Store layer type for later reference
    this.layers[layerName].geomType = geomType;
},

removeLayerFromMap: function(layerName) {
    const sourceId = 'source-' + layerName;
    const layerIds = [
        layerName + '-fill',
        layerName + '-outline',
        layerName + '-line',
        layerName + '-circle'
    ];

    layerIds.forEach(id => {
        if (this.map.getLayer(id)) {
            this.map.removeLayer(id);
        }
    });

    if (this.map.getSource(sourceId)) {
        this.map.removeSource(sourceId);
    }
},
```

**Step 2: Verify layer loading works**

Start API server
Regenerate dashboard
Toggle a layer checkbox on
Expected: Features appear on map with correct color, loading spinner shows during fetch

**Step 3: Commit**

```
feat: implement layer loading on toggle
```

---

## Task 7: Implement Bbox-Based Reloading on Pan/Zoom

**Files:**
- Modify: `download_order.py` (JavaScript MapViewer)

**Step 1: Implement reloadVisibleLayers with debounce**

```javascript
// Add at top of MapViewer object
reloadTimeout: null,

// Update the reloadVisibleLayers method
reloadVisibleLayers: function() {
    // Debounce
    if (this.reloadTimeout) {
        clearTimeout(this.reloadTimeout);
    }

    this.reloadTimeout = setTimeout(() => {
        Object.entries(this.layers).forEach(([name, layer]) => {
            if (layer.visible && !this.loadingLayers.has(name)) {
                this.loadLayerFeatures(name);
            }
        });
    }, 300);
},
```

**Step 2: Verify reloading works**

Toggle a layer on
Pan or zoom the map
Expected: Features reload for the new viewport after a brief delay

**Step 3: Commit**

```
feat: reload visible layers on map pan/zoom
```

---

## Task 8: Add Click Popup for Feature Properties

**Files:**
- Modify: `download_order.py` (JavaScript MapViewer)

**Step 1: Add click handler in init method**

Add after the `moveend` handler in `init()`:

```javascript
this.map.on('click', (e) => {
    this.handleMapClick(e);
});
```

**Step 2: Implement handleMapClick method using safe DOM construction**

```javascript
handleMapClick: function(e) {
    // Get all layer IDs that are currently on the map
    const layerIds = [];
    Object.entries(this.layers).forEach(([name, layer]) => {
        if (layer.visible) {
            const geomType = layer.geomType || '';
            if (geomType.includes('Polygon')) {
                layerIds.push(name + '-fill');
            } else if (geomType.includes('Line')) {
                layerIds.push(name + '-line');
            } else {
                layerIds.push(name + '-circle');
            }
        }
    });

    if (layerIds.length === 0) return;

    // Query features at click point
    const features = this.map.queryRenderedFeatures(e.point, { layers: layerIds });

    if (features.length === 0) return;

    const feature = features[0];
    const layerName = feature.layer.id.replace(/-fill$|-outline$|-line$|-circle$/, '');
    const props = feature.properties;

    // Build popup content using safe DOM methods
    const container = document.createElement('div');
    container.style.cssText = 'font-family: Montserrat, sans-serif; font-size: 12px;';

    // Header
    const header = document.createElement('div');
    header.style.cssText = 'font-weight: 600; color: #FADA36; margin-bottom: 8px; border-bottom: 1px solid #333; padding-bottom: 4px;';
    header.textContent = layerName;
    container.appendChild(header);

    // Properties table
    const table = document.createElement('table');
    table.style.cssText = 'border-collapse: collapse; width: 100%;';

    // Filter out internal fields and limit display
    const excludeFields = ['_source_order', '_loaded_at', 'fid'];
    const entries = Object.entries(props)
        .filter(([k]) => !excludeFields.includes(k) && !k.startsWith('_'));

    const displayEntries = entries.slice(0, 10);
    const hasMore = entries.length > 10;

    displayEntries.forEach(([key, value]) => {
        const tr = document.createElement('tr');

        const keyCell = document.createElement('td');
        keyCell.style.cssText = 'padding: 2px 8px 2px 0; color: #888; white-space: nowrap;';
        keyCell.textContent = key;

        const valueCell = document.createElement('td');
        valueCell.style.cssText = 'padding: 2px 0; color: #fff;';
        const displayValue = value === null ? '-' : String(value).substring(0, 50);
        valueCell.textContent = displayValue;

        tr.appendChild(keyCell);
        tr.appendChild(valueCell);
        table.appendChild(tr);
    });

    container.appendChild(table);

    if (hasMore) {
        const moreDiv = document.createElement('div');
        moreDiv.style.cssText = 'margin-top: 8px; color: #888; font-size: 11px;';
        moreDiv.textContent = '+ ' + (entries.length - 10) + ' more properties';
        container.appendChild(moreDiv);
    }

    new maplibregl.Popup({
        closeButton: true,
        closeOnClick: true,
        maxWidth: '300px'
    })
        .setLngLat(e.lngLat)
        .setDOMContent(container)
        .addTo(this.map);
},
```

**Step 3: Add popup CSS**

Add to the CSS section:

```css
.maplibregl-popup-content {{
    background: var(--dark-card) !important;
    border: 1px solid var(--border-subtle) !important;
    border-radius: 6px !important;
    padding: 12px !important;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.5) !important;
}}
.maplibregl-popup-close-button {{
    color: var(--text-secondary) !important;
    font-size: 18px !important;
    padding: 4px 8px !important;
}}
.maplibregl-popup-close-button:hover {{
    color: var(--gold) !important;
    background: transparent !important;
}}
.maplibregl-popup-tip {{
    border-top-color: var(--dark-card) !important;
}}
```

**Step 4: Verify popup works**

Toggle a layer on
Click on a feature
Expected: Popup appears with feature properties in styled table

**Step 5: Commit**

```
feat: add click popup for feature properties
```

---

## Task 9: Add Hover Highlighting

**Files:**
- Modify: `download_order.py` (JavaScript MapViewer)

**Step 1: Add hover handlers in init method**

Add after the click handler:

```javascript
this.map.on('mousemove', (e) => {
    this.handleMouseMove(e);
});

this.map.on('mouseleave', () => {
    this.clearHover();
});
```

**Step 2: Implement hover methods**

```javascript
hoveredFeature: null,

handleMouseMove: function(e) {
    // Get all clickable layer IDs
    const layerIds = [];
    Object.entries(this.layers).forEach(([name, layer]) => {
        if (layer.visible) {
            const geomType = layer.geomType || '';
            if (geomType.includes('Polygon')) {
                layerIds.push(name + '-fill');
            } else if (geomType.includes('Line')) {
                layerIds.push(name + '-line');
            } else {
                layerIds.push(name + '-circle');
            }
        }
    });

    if (layerIds.length === 0) {
        this.map.getCanvas().style.cursor = '';
        return;
    }

    const features = this.map.queryRenderedFeatures(e.point, { layers: layerIds });

    if (features.length > 0) {
        this.map.getCanvas().style.cursor = 'pointer';
    } else {
        this.map.getCanvas().style.cursor = '';
    }
},

clearHover: function() {
    this.map.getCanvas().style.cursor = '';
},
```

**Step 3: Verify hover cursor works**

Hover over features
Expected: Cursor changes to pointer over features

**Step 4: Commit**

```
feat: add hover cursor feedback
```

---

## Task 10: Remove Old Leaflet Map Code

**Files:**
- Modify: `download_order.py`

**Step 1: Remove Leaflet CDN imports**

Remove these lines from the `<head>`:
```html
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
```

**Step 2: Remove old map-container HTML and map-related JS**

Remove the `#map-container` div and related CSS.
Remove the `showMapForOrder()`, `closeMap()`, and Leaflet map initialization code.
Remove the "Map" button from order rows (or repurpose it to fit bounds).

**Step 3: Remove map-btn from order rows**

In the Python code around line 402-404, remove the `map_button` generation or change it to:

```python
map_button = ""
if bounds:
    bounds_str = ','.join(map(str, [bounds[0][1], bounds[0][0], bounds[1][1], bounds[1][0]]))
    map_button = f'<button class="map-btn" onclick="event.stopPropagation(); MapViewer.fitToBounds([{bounds_str}])">Zoom</button>'
```

**Step 4: Add fitToBounds method to MapViewer**

```javascript
fitToBounds: function(bbox) {
    // bbox is [west, south, east, north]
    this.map.fitBounds([[bbox[0], bbox[1]], [bbox[2], bbox[3]]], {
        padding: 50,
        maxZoom: 12
    });
},
```

**Step 5: Verify old Leaflet code is removed**

Regenerate dashboard
Open in browser, check console for no Leaflet-related errors
Expected: Dashboard works without Leaflet

**Step 6: Commit**

```
refactor: remove Leaflet map, use MapLibre only
```

---

## Task 11: Final Testing and Polish

**Files:**
- Modify: `download_order.py` (as needed)

**Step 1: Test full workflow**

1. Start API server: `python serve_api.py --db "postgresql://..."`
2. Regenerate dashboard: `python download_order.py --regenerate`
3. Open dashboard
4. Expand an order with published layers
5. Toggle layer checkboxes
6. Pan/zoom map, verify features reload
7. Click features, verify popup
8. Refresh page, verify layer state persists

**Step 2: Test error states**

1. Stop API server
2. Refresh dashboard
3. Verify graceful error message appears

**Step 3: Test responsive layout**

1. Resize browser window to narrow width
2. Verify layout stacks vertically

**Step 4: Final commit**

```
feat: complete MapLibre feature viewer implementation
```

---

## Summary

| Task | Description | Files |
|------|-------------|-------|
| 1 | Add MapLibre CDN | download_order.py |
| 2 | Split-view CSS | download_order.py |
| 3 | Restructure HTML to split-view | download_order.py |
| 4 | Initialize MapLibre map | download_order.py |
| 5 | Layer discovery and toggle UI | download_order.py |
| 6 | Layer loading on toggle | download_order.py |
| 7 | Bbox reload on pan/zoom | download_order.py |
| 8 | Click popup (safe DOM construction) | download_order.py |
| 9 | Hover highlighting | download_order.py |
| 10 | Remove old Leaflet code | download_order.py |
| 11 | Final testing | - |

All changes are in `download_order.py` since the dashboard HTML/CSS/JS is generated inline.
