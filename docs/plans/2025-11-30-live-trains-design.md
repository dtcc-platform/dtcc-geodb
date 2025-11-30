# Live Trains Viewer Design

## Overview

Add real-time train visualization to the dashboard showing trains at Swedish stations using Trafikverket's public API.

## Decisions

| Aspect | Decision |
|--------|----------|
| Data source | Trafikverket Open API (demokey - no registration) |
| API key | None needed (public demokey) |
| Update method | Manual refresh button |
| UI placement | Integrated with existing layer toggles |
| Train positions | At stations (not GPS between stations) |
| Popup info | Train ID, route (from/to), current station, delay status |

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                         Browser                              │
│  ┌──────────────────┐  ┌──────────────────────────────────┐ │
│  │  Layer Toggles   │  │         MapLibre Map             │ │
│  │  ─────────────── │  │                                  │ │
│  │  ☑ byggnadspunkt │  │    ○ ← station markers          │ │
│  │  ☑ Live Trains   │  │    🚂 ← trains at stations      │ │
│  │                  │  │         (click for popup)       │ │
│  │  [↻ Refresh]     │  │                                  │ │
│  └──────────────────┘  └──────────────────────────────────┘ │
└───────────────┬─────────────────────────────────────────────┘
                │
                ▼
   Trafikverket API (demokey - no user key needed)
   POST https://api.trafikinfo.trafikverket.se/v2/data.json
```

**Data flow:**
1. On toggle ON: Fetch `TrainStation` (all stations with coordinates) - cache this
2. Fetch `TrainAnnouncement` filtered to recent arrivals/departures
3. Join by `LocationSignature` to place trains at station coordinates
4. Render as MapLibre layer with click popups

## API Queries

**TrainStation (fetched once, cached):**
```xml
<REQUEST>
  <LOGIN authenticationkey="demokey"/>
  <QUERY objecttype="TrainStation" schemaversion="1.4">
    <FILTER><EQ name="Advertised" value="true"/></FILTER>
    <INCLUDE>LocationSignature</INCLUDE>
    <INCLUDE>AdvertisedLocationName</INCLUDE>
    <INCLUDE>Geometry.WGS84</INCLUDE>
  </QUERY>
</REQUEST>
```

**TrainAnnouncement (on each refresh):**
```xml
<REQUEST>
  <LOGIN authenticationkey="demokey"/>
  <QUERY objecttype="TrainAnnouncement" schemaversion="1.9" limit="500">
    <FILTER>
      <GT name="TimeAtLocation" value="$now-00:10:00"/>
      <LT name="TimeAtLocation" value="$now+00:05:00"/>
    </FILTER>
    <INCLUDE>AdvertisedTrainIdent</INCLUDE>
    <INCLUDE>LocationSignature</INCLUDE>
    <INCLUDE>FromLocation</INCLUDE>
    <INCLUDE>ToLocation</INCLUDE>
    <INCLUDE>TimeAtLocation</INCLUDE>
    <INCLUDE>EstimatedTimeAtLocation</INCLUDE>
    <INCLUDE>AdvertisedTimeAtLocation</INCLUDE>
    <INCLUDE>Operator</INCLUDE>
  </QUERY>
</REQUEST>
```

**Joined data structure:**
```javascript
{
  trainId: "60183",
  location: { name: "Alingsås", lng: 12.53, lat: 57.92 },
  from: "Stockholm",
  to: "Göteborg",
  delay: 5,  // minutes
  operator: "SJ"
}
```

## UI Components

**Layer toggle:**
- "Live Trains" checkbox in existing layer list
- Styled same as other layers
- Refresh button appears when toggled ON

**Map markers:**
- Train icon at station coordinates
- Color: green (on time), orange (1-10 min delay), red (10+ min delay)
- Multiple trains at same station: offset slightly

**Click popup:**
```
┌─────────────────────────┐
│ Train 60183             │
│ ─────────────────────── │
│ Stockholm → Göteborg    │
│ At: Alingsås            │
│ Status: 5 min delay     │
│ Operator: SJ            │
└─────────────────────────┘
```

## Implementation

**Files to modify:**
- `src/lm_geotorget/management/server.py`

**Components:**
1. CSS (~30 lines) - train marker styles, popup, refresh button
2. HTML (~10 lines) - toggle and refresh button in layer list
3. JavaScript TrainViewer object (~150 lines):
   - `fetchStations()` - one-time load, cache
   - `fetchTrains()` - get recent announcements
   - `renderTrains()` - MapLibre layer
   - `showPopup(train)` - display details

**No backend changes** - browser calls Trafikverket directly.
