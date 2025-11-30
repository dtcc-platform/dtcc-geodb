# Geo Chatbot Design

## Overview

Add an LLM-powered chatbot to the dashboard that answers questions about geodata by querying PostGIS directly.

## Decisions

| Aspect | Decision |
|--------|----------|
| Query method | LLM generates SQL directly |
| UI | Floating chat bubble (bottom-right) |
| API key | User provides their own (localStorage) |
| Map integration | Read-only (no map control) |
| Context | Schema + samples + metadata |
| Model | claude-sonnet-4-20250514 |
| Safeguards | Read-only SQL, 30s timeout, 1000 row limit |

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Browser                               │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────┐  │
│  │  Dashboard  │  │  MapViewer  │  │  ChatWidget     │  │
│  │             │  │             │  │  (floating)     │  │
│  └─────────────┘  └─────────────┘  └────────┬────────┘  │
│                                              │           │
│                     Claude API  ◄────────────┘           │
│                     (direct from browser)                │
└─────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────┐
│                Management Server (Flask)                 │
│  ┌─────────────────────────────────────────────────┐    │
│  │  POST /api/chat/query                            │    │
│  │  - Receives SQL from browser                     │    │
│  │  - Validates (read-only, timeout)               │    │
│  │  - Executes against PostGIS                     │    │
│  │  - Returns results as JSON                      │    │
│  └─────────────────────────────────────────────────┘    │
│  ┌─────────────────────────────────────────────────┐    │
│  │  GET /api/chat/context                          │    │
│  │  - Returns schema, sample data, metadata        │    │
│  └─────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────┘
```

**Flow:**
1. User types question in chat widget
2. Browser fetches database context from `/api/chat/context`
3. Browser calls Claude API directly with question + context
4. Claude responds with SQL query
5. Browser sends SQL to `/api/chat/query` for execution
6. Results returned to browser, Claude formats the answer

## Chat Widget UI

**Collapsed state:**
- Floating circular button (48px) in bottom-right corner
- Gold color matching dashboard theme
- Chat icon (speech bubble)
- Subtle pulse animation when first loaded

**Expanded state:**
- Chat window ~400px wide x 500px tall
- Dark theme matching dashboard
- Header bar with "Geo Assistant" title and close button
- API key input field (shown only if no key saved)
- Message history area (scrollable)
- Input field at bottom with send button

**Message styling:**
- User messages: right-aligned, gold background
- Assistant messages: left-aligned, dark background
- SQL queries shown in collapsible code blocks
- Results shown as formatted tables (truncated if large)
- Error messages in red

**State persistence:**
- API key saved to localStorage
- Chat history cleared on page refresh

## Backend API Endpoints

### GET /api/chat/context

Returns database context for Claude's system prompt:

```json
{
  "schema": {
    "geotorget.byggnad": {
      "columns": ["id", "objektidentitet", "geometry", ...],
      "geometry_type": "MultiPolygon",
      "srid": 3006,
      "row_count": 45000
    }
  },
  "samples": {
    "geotorget.byggnad": [
      {"id": 1, "objektidentitet": "abc123", ...}
    ]
  },
  "metadata": {
    "orders": ["fe76535a-a4fd-45e3-9a16-1d7464f0bd32"],
    "coordinate_system": "SWEREF99 TM (EPSG:3006)",
    "total_layers": 33
  }
}
```

### POST /api/chat/query

Executes validated SQL:

```json
// Request
{
  "sql": "SELECT COUNT(*) FROM geotorget.byggnad"
}

// Response
{
  "success": true,
  "results": [{"count": 45000}],
  "row_count": 1,
  "execution_time_ms": 42
}
```

**Safeguards:**
- SQL parsed to ensure read-only (SELECT only)
- Query timeout: 30 seconds
- Result limit: 1000 rows max
- Schema restricted to configured schema

## Claude Integration

**System prompt structure:**

```
You are a geodata assistant for Swedish Lantmäteriet data stored in PostGIS.

DATABASE CONTEXT:
- Schema: geotorget
- Coordinate system: SWEREF99 TM (EPSG:3006)
- Available tables: [list from /api/chat/context]

TABLE SCHEMAS:
[formatted schema info with column names, types, geometry types]

SAMPLE DATA:
[2-3 rows per table to show data format]

INSTRUCTIONS:
- Generate PostgreSQL/PostGIS SQL to answer user questions
- Return SQL wrapped in ```sql code blocks
- For spatial queries, use ST_* PostGIS functions
- Keep queries efficient (use LIMIT, indexes)
- After receiving results, provide a natural language summary
```

**Conversation flow:**

1. User: "How many buildings are there?"
2. Claude: "I'll query that for you: ```sql SELECT COUNT(*) FROM geotorget.byggnad ```"
3. System executes SQL, returns results
4. Claude: "There are 45,000 buildings in the database."

**API configuration:**
- Model: claude-sonnet-4-20250514
- Direct browser-to-Anthropic API call
- Streaming responses for better UX

## Out of Scope

- Map highlighting of query results
- Chat history persistence
- Voice input
- Export results to file
