# MapLibre GL JS Feature Viewer Design

## Overview

Add an interactive MapLibre GL JS map to the dashboard for viewing downloaded geodata features stored in PostGIS.

## Decisions

| Aspect | Decision |
|--------|----------|
| Map library | MapLibre GL JS |
| Data source | PostGIS via existing `/api/layers/{layer}/features` endpoint |
| Loading strategy | On-demand, bbox-based fetching on pan/zoom |
| Layout | Split view: Datasets list (40%) + Map (60%) |
| Layer controls | Checkboxes in existing Datasets tab file rows |
| Styling | Auto-color by layer name, style by geometry type |
| Interaction | Click for popup with feature properties |
| Basemap | OpenStreetMap raster tiles |

## Layout

The Datasets tab changes from full-width table to split view:

```
+------------------------------------------+
|  Datasets | Blueprint                    |
+------------------------------------------+
|                    |                     |
|   Order List       |    MapLibre Map     |
|   (left panel)     |    (right panel)    |
|   ~40% width       |    ~60% width       |
|                    |                     |
|   - Order 1        |    [map canvas]     |
|     [x] Layer A    |                     |
|     [x] Layer B    |                     |
|   - Order 2        |                     |
|     [ ] Layer C    |                     |
|                    |                     |
+------------------------------------------+
```

Responsive: stack vertically on narrow screens, map minimum height 400px.

## Data Loading

1. On dashboard load, fetch `GET /api/layers` to discover available PostGIS layers
2. Match layers to orders using `_source_order` metadata field
3. When layer toggled on, fetch features for current viewport:
   - `GET /api/layers/{layer}/features?bbox={minx},{miny},{maxx},{maxy}&limit=5000`
4. On map `moveend` event (debounced 300ms), refetch for visible layers
5. Clear layer cache when toggled off

## Map Rendering

### Basemap
- OpenStreetMap raster tiles (no API key required)

### Layer Styling
- Auto-generate color from layer name hash: `hsl(hash % 360, 70%, 50%)`
- Style by geometry type:
  - Polygon: 0.3 opacity fill, solid stroke
  - LineString: 2-3px solid line
  - Point: 6-8px circle markers

### Layer Ordering
- Polygons (bottom) -> Lines -> Points (top)

### Initial View
- Center on Sweden (lat 62, lng 17, zoom 4)
- Fit to first enabled layer's bbox

## Feature Interaction

### Click
- Open popup anchored to click location
- Content: layer name, feature ID, property table
- Exclude internal fields (`_source_order`, `_loaded_at`, `fid`)
- Limit to 10 properties with "Show all" expansion

### Hover
- Subtle highlight (increased opacity or outline)
- Pointer cursor

## Layer Toggle UI

Integrate into existing Datasets tab file rows:

```
  [x] [color] byggnad.gpkg    2,451 features    12.3 MB
```

- Checkbox for published layers only
- Color swatch matches map layer color
- Spinner while loading, error icon on failure
- Unpublished files show disabled checkbox

### State Persistence
- Store enabled layers in localStorage
- Restore on page reload

## Dependencies

- MapLibre GL JS (load from CDN)
- API server must be running for map functionality

## Out of Scope

- Vector tiles / pg_tileserv
- Custom color configuration
- Layer opacity controls
- Feature search/filter by attribute
- Export selected features
