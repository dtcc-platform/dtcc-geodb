# Role-Based Access Control Design

## Overview

Implement role-based access control to differentiate between Admin and User roles. Admins see the full dashboard (current behavior). Users see a consumer portal with published layers, map explorer, API documentation, and scoped chat access.

## Data Model

### Users Table

Stored in the `geotorget` schema alongside existing metadata:

```sql
CREATE TABLE geotorget._users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role VARCHAR(50) NOT NULL DEFAULT 'user',  -- 'admin' or 'user'
    created_at TIMESTAMP DEFAULT NOW(),
    created_by INTEGER REFERENCES geotorget._users(id)
);
```

### Password Handling

- Use `werkzeug.security.generate_password_hash` / `check_password_hash`
- Never store plaintext passwords

### Bootstrap Logic

1. On login, check if `_users` table exists and has any admin users
2. If no admin users exist, accept `AUTH_USERNAME`/`AUTH_PASSWORD` from env vars as admin
3. Once an admin user exists in the database, env var login is disabled
4. Session stores: `logged_in`, `user_id`, `role`, `username`

## Roles

| Role | Description |
|------|-------------|
| `admin` | Full dashboard access, user management, database config, orders |
| `user` | Consumer portal: layers, map, API docs, scoped chat |

## Authentication Flow

### Login Changes

- Login form unchanged (username/password)
- On success, session stores: `session['logged_in'] = True`, `session['user_id'] = id`, `session['role'] = role`, `session['username'] = username`
- Redirect to `./` on success

### Route Protection

**Existing decorator (keep):**
- `@login_required` - requires any authenticated user

**New decorator:**
- `@admin_required` - requires authenticated user with `role='admin'`

## Route Access Matrix

### Admin-Only Routes

| Method | Route | Purpose |
|--------|-------|---------|
| GET/POST | `/api/config` | Database configuration |
| POST | `/api/orders/<id>/download` | Download orders |
| POST | `/api/orders/<id>/publish` | Publish to PostGIS |
| DELETE | `/api/orders/<id>` | Delete orders |
| GET | `/api/users` | List users |
| POST | `/api/users` | Create user |
| PUT | `/api/users/<id>` | Update user |
| DELETE | `/api/users/<id>` | Delete user |

### All Authenticated Users

| Method | Route | Purpose |
|--------|-------|---------|
| GET | `/` | Dashboard (content differs by role) |
| GET | `/api/layers` | List published layers |
| GET | `/api/layers/<name>/features` | Query features |
| POST | `/api/chat/query` | AI chat (scoped for Users) |
| GET | `/logout` | Logout |

## Dashboard UI by Role

### Admin Dashboard

Current behavior plus new User Management panel:

1. Status bar - database connection, Martin tile server status
2. Database configuration panel
3. Orders management panel (download, publish, delete)
4. **User management panel (new)** - create/edit/delete users
5. Layers panel with full details
6. Chat interface (full database access, read-only SQL)

### User Dashboard

Consumer portal view:

1. Status bar - simplified, just "Connected" or "Disconnected"
2. Layers panel - published layers with map explorer
3. **API documentation panel (new)** - endpoint URLs and examples
4. Chat interface - scoped to published layers only

**Hidden from Users:**
- Database configuration panel
- Orders management panel
- User management panel

## User Management Panel

### Location

New collapsible panel in admin dashboard, between Orders and Layers panels.

### UI Components

- Table listing all users: username, role, created date, created by
- "Add User" button - opens form with username, password, role dropdown
- Edit button per row - change role, reset password
- Delete button per row - removes user with confirmation

### API Endpoints

```
GET    /api/users          - List all users (id, username, role, created_at)
POST   /api/users          - Create user {username, password, role}
PUT    /api/users/<id>     - Update user {role?, password?}
DELETE /api/users/<id>     - Delete user
```

### Validation Rules

- Username must be unique
- Password minimum 8 characters
- Cannot delete yourself (prevents lockout)
- Cannot delete last admin user
- Cannot demote yourself from admin

## API Documentation Panel

### Purpose

Show Users how to consume published layers programmatically.

### Content Per Layer

- Layer name and description
- Geometry type, SRID, feature count
- Available columns/attributes
- Endpoint examples with copy-to-clipboard:
  ```
  GET /api/layers/{layer_name}
  GET /api/layers/{layer_name}/features?bbox=x1,y1,x2,y2
  GET /api/layers/{layer_name}/features/{fid}
  ```
- Example response (truncated JSON)
- Martin tile URLs if available:
  ```
  Vector tiles: /tiles/{layer_name}/{z}/{x}/{y}.pbf
  ```

### Implementation

- New collapsible panel in User dashboard
- Dynamically generated from `/api/layers` response
- Base URL detected from `window.location`
- Matches existing dashboard panel styling

## Chat Scoping for Users

When role is `user`, the chat system prompt is modified to:

1. Only reference published layer tables (not raw order tables)
2. Explicitly instruct Claude to refuse queries on non-published tables
3. Maintain read-only SQL restriction (same as admin)

## Implementation Notes

### Files to Modify

- `src/lm_geotorget/management/server.py` - all changes in this file:
  - Add `_users` table creation logic
  - Add `@admin_required` decorator
  - Modify login to check database users first, then env var fallback
  - Add user management API endpoints
  - Modify `generate_dashboard_html()` to accept role parameter
  - Add User Management panel HTML (admin only)
  - Add API Documentation panel HTML (user only)
  - Modify chat endpoint to scope queries for Users

### Migration Path

1. Deploy updated code
2. First login uses env var credentials (bootstrap mode)
3. Admin creates database users via new panel
4. Env var login automatically disabled once DB admin exists

### Rollback

If issues occur, env var login can be re-enabled by:
1. Deleting all rows from `_users` table, OR
2. Adding a `FORCE_ENV_AUTH=true` env var override (optional safeguard)
