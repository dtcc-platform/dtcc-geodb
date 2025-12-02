# Role-Based Access Control Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add database-backed user management with Admin and User roles, where Admins see the full dashboard and Users see a consumer portal with layers, map, API docs, and scoped chat.

**Architecture:** Extend existing Flask session auth with database-backed users in a `_users` table. Add `@admin_required` decorator for admin-only routes. Modify `generate_dashboard_html()` to conditionally render panels based on role.

**Tech Stack:** Flask, PostgreSQL/psycopg2, werkzeug.security for password hashing

---

## Task 1: Add Password Hashing Import

**Files:**
- Modify: `src/lm_geotorget/management/server.py:24-29`

**Step 1: Add werkzeug.security import**

At line 24-29, there's a try/except block for Flask imports. Add the password hashing import inside this block:

```python
try:
    from flask import Flask, request, jsonify, Response, stream_with_context, session, redirect, url_for
    from functools import wraps
    from werkzeug.security import generate_password_hash, check_password_hash
    HAS_FLASK = True
except ImportError:
    HAS_FLASK = False
```

**Step 2: Verify import works**

Run: `python -c "from werkzeug.security import generate_password_hash, check_password_hash; print('OK')"`

Expected: `OK`

---

## Task 2: Create @admin_required Decorator

**Files:**
- Modify: `src/lm_geotorget/management/server.py:32-42`

**Step 1: Add admin_required decorator after login_required**

After the existing `login_required` decorator (ends at line 42), add:

```python
def admin_required(f):
    """Decorator to require admin role for routes."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({'error': 'Authentication required'}), 401
            return redirect('login')
        if session.get('role') != 'admin':
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({'error': 'Admin access required'}), 403
            return redirect('./')
        return f(*args, **kwargs)
    return decorated_function
```

**Step 2: Verify syntax**

Run: `python -m py_compile src/lm_geotorget/management/server.py`

Expected: No output (success)

---

## Task 3: Add User Table Helper Functions

**Files:**
- Modify: `src/lm_geotorget/management/server.py` (add after line ~1700, before `generate_login_html`)

**Step 1: Add helper functions for user management**

Add these functions before `generate_login_html`:

```python
def ensure_users_table(db_connection: str, schema: str) -> bool:
    """Create _users table if it doesn't exist. Returns True if table exists/created."""
    if not db_connection:
        return False
    try:
        import psycopg2
        conn = psycopg2.connect(db_connection)
        cur = conn.cursor()
        cur.execute(f'''
            CREATE TABLE IF NOT EXISTS "{schema}"._users (
                id SERIAL PRIMARY KEY,
                username VARCHAR(255) UNIQUE NOT NULL,
                password_hash VARCHAR(255) NOT NULL,
                role VARCHAR(50) NOT NULL DEFAULT 'user',
                created_at TIMESTAMP DEFAULT NOW(),
                created_by INTEGER REFERENCES "{schema}"._users(id)
            )
        ''')
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        print(f"Failed to create _users table: {e}")
        return False


def get_db_user(db_connection: str, schema: str, username: str) -> Optional[dict]:
    """Get user from database by username. Returns dict with id, username, password_hash, role or None."""
    if not db_connection:
        return None
    try:
        import psycopg2
        conn = psycopg2.connect(db_connection)
        cur = conn.cursor()
        cur.execute(f'''
            SELECT id, username, password_hash, role
            FROM "{schema}"._users
            WHERE username = %s
        ''', (username,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row:
            return {'id': row[0], 'username': row[1], 'password_hash': row[2], 'role': row[3]}
        return None
    except Exception:
        return None


def has_any_admin(db_connection: str, schema: str) -> bool:
    """Check if any admin user exists in database."""
    if not db_connection:
        return False
    try:
        import psycopg2
        conn = psycopg2.connect(db_connection)
        cur = conn.cursor()
        cur.execute(f'''
            SELECT COUNT(*) FROM "{schema}"._users WHERE role = 'admin'
        ''')
        count = cur.fetchone()[0]
        cur.close()
        conn.close()
        return count > 0
    except Exception:
        return False


def list_all_users(db_connection: str, schema: str) -> list:
    """List all users (without password hashes)."""
    if not db_connection:
        return []
    try:
        import psycopg2
        conn = psycopg2.connect(db_connection)
        cur = conn.cursor()
        cur.execute(f'''
            SELECT u.id, u.username, u.role, u.created_at, c.username as created_by
            FROM "{schema}"._users u
            LEFT JOIN "{schema}"._users c ON u.created_by = c.id
            ORDER BY u.created_at
        ''')
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [
            {'id': r[0], 'username': r[1], 'role': r[2], 'created_at': r[3].isoformat() if r[3] else None, 'created_by': r[4]}
            for r in rows
        ]
    except Exception:
        return []


def create_db_user(db_connection: str, schema: str, username: str, password: str, role: str, created_by: Optional[int] = None) -> dict:
    """Create a new user. Returns {'success': True, 'id': ...} or {'success': False, 'error': ...}."""
    if not db_connection:
        return {'success': False, 'error': 'Database not configured'}
    if role not in ('admin', 'user'):
        return {'success': False, 'error': 'Invalid role'}
    if len(password) < 8:
        return {'success': False, 'error': 'Password must be at least 8 characters'}
    try:
        import psycopg2
        password_hash = generate_password_hash(password)
        conn = psycopg2.connect(db_connection)
        cur = conn.cursor()
        cur.execute(f'''
            INSERT INTO "{schema}"._users (username, password_hash, role, created_by)
            VALUES (%s, %s, %s, %s)
            RETURNING id
        ''', (username, password_hash, role, created_by))
        user_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        return {'success': True, 'id': user_id}
    except psycopg2.IntegrityError:
        return {'success': False, 'error': 'Username already exists'}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def update_db_user(db_connection: str, schema: str, user_id: int, role: Optional[str] = None, password: Optional[str] = None) -> dict:
    """Update user role and/or password. Returns {'success': True} or {'success': False, 'error': ...}."""
    if not db_connection:
        return {'success': False, 'error': 'Database not configured'}
    if role and role not in ('admin', 'user'):
        return {'success': False, 'error': 'Invalid role'}
    if password and len(password) < 8:
        return {'success': False, 'error': 'Password must be at least 8 characters'}
    try:
        import psycopg2
        conn = psycopg2.connect(db_connection)
        cur = conn.cursor()
        if role and password:
            password_hash = generate_password_hash(password)
            cur.execute(f'''
                UPDATE "{schema}"._users SET role = %s, password_hash = %s WHERE id = %s
            ''', (role, password_hash, user_id))
        elif role:
            cur.execute(f'''
                UPDATE "{schema}"._users SET role = %s WHERE id = %s
            ''', (role, user_id))
        elif password:
            password_hash = generate_password_hash(password)
            cur.execute(f'''
                UPDATE "{schema}"._users SET password_hash = %s WHERE id = %s
            ''', (password_hash, user_id))
        conn.commit()
        cur.close()
        conn.close()
        return {'success': True}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def delete_db_user(db_connection: str, schema: str, user_id: int) -> dict:
    """Delete a user. Returns {'success': True} or {'success': False, 'error': ...}."""
    if not db_connection:
        return {'success': False, 'error': 'Database not configured'}
    try:
        import psycopg2
        conn = psycopg2.connect(db_connection)
        cur = conn.cursor()
        # Check if this is the last admin
        cur.execute(f'''
            SELECT role FROM "{schema}"._users WHERE id = %s
        ''', (user_id,))
        row = cur.fetchone()
        if not row:
            cur.close()
            conn.close()
            return {'success': False, 'error': 'User not found'}
        if row[0] == 'admin':
            cur.execute(f'''
                SELECT COUNT(*) FROM "{schema}"._users WHERE role = 'admin'
            ''')
            admin_count = cur.fetchone()[0]
            if admin_count <= 1:
                cur.close()
                conn.close()
                return {'success': False, 'error': 'Cannot delete last admin user'}
        cur.execute(f'''
            DELETE FROM "{schema}"._users WHERE id = %s
        ''', (user_id,))
        conn.commit()
        cur.close()
        conn.close()
        return {'success': True}
    except Exception as e:
        return {'success': False, 'error': str(e)}
```

**Step 2: Verify syntax**

Run: `python -m py_compile src/lm_geotorget/management/server.py`

Expected: No output (success)

---

## Task 4: Modify Login to Use Database Users

**Files:**
- Modify: `src/lm_geotorget/management/server.py:210-225`

**Step 1: Replace login route logic**

Replace the login route (lines 210-225) with:

```python
    @app.route('/login', methods=['GET', 'POST'])
    def login():
        """Login page."""
        if request.method == 'POST':
            username = request.form.get('username', '')
            password = request.form.get('password', '')

            # Ensure users table exists
            if app.config['db_connection']:
                ensure_users_table(app.config['db_connection'], app.config['schema'])

            # Try database user first
            db_user = None
            if app.config['db_connection']:
                db_user = get_db_user(app.config['db_connection'], app.config['schema'], username)

            if db_user and check_password_hash(db_user['password_hash'], password):
                # Database user login
                session['logged_in'] = True
                session['user_id'] = db_user['id']
                session['username'] = db_user['username']
                session['role'] = db_user['role']
                return redirect('./')

            # Fallback to env var credentials only if no admin exists in DB
            if not has_any_admin(app.config['db_connection'], app.config['schema']):
                if username == app.config['AUTH_USERNAME'] and password == app.config['AUTH_PASSWORD']:
                    session['logged_in'] = True
                    session['user_id'] = None  # No DB user
                    session['username'] = username
                    session['role'] = 'admin'  # Env var user is always admin
                    return redirect('./')

            return generate_login_html(error='Invalid username or password')

        return generate_login_html()
```

**Step 2: Verify syntax**

Run: `python -m py_compile src/lm_geotorget/management/server.py`

Expected: No output (success)

---

## Task 5: Add User Management API Endpoints

**Files:**
- Modify: `src/lm_geotorget/management/server.py` (add after the `/logout` route, around line 232)

**Step 1: Add user management endpoints**

Add these routes after the logout route:

```python
    # ==================== User Management ====================

    @app.route('/api/users')
    @admin_required
    def list_users():
        """List all users."""
        users = list_all_users(app.config['db_connection'], app.config['schema'])
        return jsonify(users)

    @app.route('/api/users', methods=['POST'])
    @admin_required
    def create_user():
        """Create a new user."""
        data = request.json or {}
        username = data.get('username', '').strip()
        password = data.get('password', '')
        role = data.get('role', 'user')

        if not username:
            return jsonify({'error': 'Username is required'}), 400
        if not password:
            return jsonify({'error': 'Password is required'}), 400

        result = create_db_user(
            app.config['db_connection'],
            app.config['schema'],
            username,
            password,
            role,
            session.get('user_id')
        )

        if result['success']:
            return jsonify({'status': 'ok', 'id': result['id']})
        return jsonify({'error': result['error']}), 400

    @app.route('/api/users/<int:user_id>', methods=['PUT'])
    @admin_required
    def update_user(user_id: int):
        """Update a user's role or password."""
        # Prevent self-demotion
        if user_id == session.get('user_id'):
            data = request.json or {}
            if data.get('role') and data['role'] != 'admin':
                return jsonify({'error': 'Cannot demote yourself'}), 400

        data = request.json or {}
        result = update_db_user(
            app.config['db_connection'],
            app.config['schema'],
            user_id,
            role=data.get('role'),
            password=data.get('password')
        )

        if result['success']:
            return jsonify({'status': 'ok'})
        return jsonify({'error': result['error']}), 400

    @app.route('/api/users/<int:user_id>', methods=['DELETE'])
    @admin_required
    def delete_user(user_id: int):
        """Delete a user."""
        # Prevent self-deletion
        if user_id == session.get('user_id'):
            return jsonify({'error': 'Cannot delete yourself'}), 400

        result = delete_db_user(
            app.config['db_connection'],
            app.config['schema'],
            user_id
        )

        if result['success']:
            return jsonify({'status': 'ok'})
        return jsonify({'error': result['error']}), 400
```

**Step 2: Verify syntax**

Run: `python -m py_compile src/lm_geotorget/management/server.py`

Expected: No output (success)

---

## Task 6: Protect Admin-Only Routes

**Files:**
- Modify: `src/lm_geotorget/management/server.py`

**Step 1: Change decorators on admin-only routes**

Find and replace `@login_required` with `@admin_required` on these routes:

1. **`/api/config` POST** (around line 264):
   ```python
   @app.route('/api/config', methods=['POST'])
   @admin_required  # Changed from @login_required
   def set_config():
   ```

2. **`/api/orders` GET** (around line 282) - keep as `@login_required` (users can see orders list for context, but actual data is filtered in dashboard)

3. **`/api/download/<order_id>` POST** (around line 400):
   ```python
   @app.route('/api/download/<order_id>', methods=['POST'])
   @admin_required  # Changed from @login_required
   def start_download(order_id: str):
   ```

4. **`/api/orders/<order_id>/publish` POST** (around line 467):
   ```python
   @app.route('/api/orders/<order_id>/publish', methods=['POST'])
   @admin_required  # Changed from @login_required
   def publish_order(order_id: str):
   ```

5. **`/api/orders/<order_id>` DELETE** - search for this route and add `@admin_required`

6. **`/api/orders/<order_id>/package-name` POST** (around line 359):
   ```python
   @app.route('/api/orders/<order_id>/package-name', methods=['POST'])
   @admin_required  # Changed from @login_required
   def set_package_name(order_id: str):
   ```

**Step 2: Verify syntax**

Run: `python -m py_compile src/lm_geotorget/management/server.py`

Expected: No output (success)

---

## Task 7: Modify Dashboard Route to Pass Role

**Files:**
- Modify: `src/lm_geotorget/management/server.py:235-242`

**Step 1: Update dashboard route to pass role**

Replace the dashboard route:

```python
    @app.route('/')
    @login_required
    def dashboard():
        """Serve the dashboard HTML."""
        dashboard_path = app.config['downloads_dir'].parent / 'dashboard.html'
        if dashboard_path.exists():
            return dashboard_path.read_text()
        role = session.get('role', 'user')
        username = session.get('username', '')
        return generate_dashboard_html(app.config['downloads_dir'], role=role, username=username)
```

**Step 2: Verify syntax**

Run: `python -m py_compile src/lm_geotorget/management/server.py`

Expected: No output (success)

---

## Task 8: Update generate_dashboard_html Signature

**Files:**
- Modify: `src/lm_geotorget/management/server.py:1863`

**Step 1: Update function signature**

Change line 1863 from:
```python
def generate_dashboard_html(downloads_dir: Path) -> str:
```

To:
```python
def generate_dashboard_html(downloads_dir: Path, role: str = 'admin', username: str = '') -> str:
```

**Step 2: Add role variable to JavaScript**

In the generated HTML, after the opening `<script>` tag (search for the first `<script>` after styles), add at the very beginning:

```javascript
const USER_ROLE = '${role}';
const USERNAME = '${username}';
```

Note: You'll need to change the string from `'''...'''` to an f-string: `f'''...'''` and use `{role}` and `{username}`.

Actually, since this is a large HTML string, a cleaner approach is to add these as a separate script tag right after `<body>`:

Find:
```html
<body>
```

Replace with:
```html
<body>
    <script>
        const USER_ROLE = '{role}';
        const USERNAME = '{username}';
    </script>
```

And change the function to use f-string:
```python
def generate_dashboard_html(downloads_dir: Path, role: str = 'admin', username: str = '') -> str:
    """Generate dashboard HTML with management API integration."""
    return f'''<!DOCTYPE html>
```

**Step 3: Verify syntax**

Run: `python -m py_compile src/lm_geotorget/management/server.py`

Expected: No output (success)

---

## Task 9: Add User Management Panel HTML (Admin Only)

**Files:**
- Modify: `src/lm_geotorget/management/server.py` (in `generate_dashboard_html`)

**Step 1: Add CSS for user management panel**

Find the CSS section in `generate_dashboard_html` and add these styles (after existing panel styles):

```css
/* User Management Panel */
.user-table {{
    width: 100%;
    border-collapse: collapse;
    margin-top: 1rem;
}}
.user-table th, .user-table td {{
    padding: 0.75rem;
    text-align: left;
    border-bottom: 1px solid var(--border-subtle);
}}
.user-table th {{
    color: var(--text-secondary);
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
}}
.user-actions {{
    display: flex;
    gap: 0.5rem;
}}
.user-form {{
    display: grid;
    grid-template-columns: 1fr 1fr auto auto;
    gap: 0.75rem;
    margin-top: 1rem;
    padding: 1rem;
    background: var(--dark-secondary);
    border-radius: 4px;
}}
.user-form input, .user-form select {{
    padding: 0.5rem;
    background: var(--dark-bg);
    border: 1px solid var(--border-subtle);
    border-radius: 4px;
    color: var(--text-primary);
    font-family: inherit;
}}
.role-badge {{
    padding: 0.25rem 0.5rem;
    border-radius: 4px;
    font-size: 0.75rem;
    text-transform: uppercase;
}}
.role-badge.admin {{
    background: var(--gold-dim);
    color: var(--gold);
}}
.role-badge.user {{
    background: rgba(72, 187, 120, 0.2);
    color: var(--green);
}}
```

**Step 2: Add User Management panel HTML**

Find where the Orders panel ends (look for the closing `</section>` of the orders section) and add this panel right after, wrapped in a role check:

```html
{'<!-- User Management Panel (Admin Only) -->' if role == 'admin' else ''}
{'''
<section class="panel" id="users-panel">
    <div class="panel-header" onclick="togglePanel('users-panel')">
        <h2>User Management</h2>
        <span class="toggle-icon">-</span>
    </div>
    <div class="panel-content">
        <div class="user-form" id="add-user-form">
            <input type="text" id="new-username" placeholder="Username" />
            <input type="password" id="new-password" placeholder="Password (min 8 chars)" />
            <select id="new-role">
                <option value="user">User</option>
                <option value="admin">Admin</option>
            </select>
            <button class="btn btn-primary" onclick="createUser()">Add User</button>
        </div>
        <table class="user-table">
            <thead>
                <tr>
                    <th>Username</th>
                    <th>Role</th>
                    <th>Created</th>
                    <th>Created By</th>
                    <th>Actions</th>
                </tr>
            </thead>
            <tbody id="users-list">
                <tr><td colspan="5" style="color: var(--text-secondary)">Loading...</td></tr>
            </tbody>
        </table>
    </div>
</section>
''' if role == 'admin' else ''}
```

**Step 3: Verify syntax**

Run: `python -m py_compile src/lm_geotorget/management/server.py`

Expected: No output (success)

---

## Task 10: Add User Management JavaScript

**Files:**
- Modify: `src/lm_geotorget/management/server.py` (in `generate_dashboard_html` JavaScript section)

**Step 1: Add user management functions**

Find the JavaScript section and add these functions (wrapped in admin check):

```javascript
// User Management (Admin Only)
async function loadUsers() {
    if (USER_ROLE !== 'admin') return;
    try {
        const resp = await fetch('./api/users');
        if (!resp.ok) return;
        const users = await resp.json();
        renderUsers(users);
    } catch (e) {
        console.error('Failed to load users:', e);
    }
}

function renderUsers(users) {
    const tbody = document.getElementById('users-list');
    if (!tbody) return;
    tbody.innerHTML = '';

    users.forEach(user => {
        const tr = document.createElement('tr');
        const isSelf = user.username === USERNAME;

        tr.innerHTML = `
            <td>${escapeHtml(user.username)}</td>
            <td><span class="role-badge ${user.role}">${user.role}</span></td>
            <td>${user.created_at ? new Date(user.created_at).toLocaleDateString() : '-'}</td>
            <td>${user.created_by || '-'}</td>
            <td class="user-actions">
                <button class="btn btn-sm" onclick="editUser(${user.id}, '${user.role}')" ${isSelf ? 'disabled' : ''}>Edit</button>
                <button class="btn btn-sm btn-danger" onclick="deleteUser(${user.id}, '${escapeHtml(user.username)}')" ${isSelf ? 'disabled' : ''}>Delete</button>
            </td>
        `;
        tbody.appendChild(tr);
    });

    if (users.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" style="color: var(--text-secondary)">No users found</td></tr>';
    }
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

async function createUser() {
    const username = document.getElementById('new-username').value.trim();
    const password = document.getElementById('new-password').value;
    const role = document.getElementById('new-role').value;

    if (!username || !password) {
        alert('Username and password are required');
        return;
    }

    try {
        const resp = await fetch('./api/users', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({username, password, role})
        });
        const data = await resp.json();
        if (!resp.ok) {
            alert(data.error || 'Failed to create user');
            return;
        }
        document.getElementById('new-username').value = '';
        document.getElementById('new-password').value = '';
        document.getElementById('new-role').value = 'user';
        loadUsers();
    } catch (e) {
        alert('Failed to create user: ' + e.message);
    }
}

async function editUser(userId, currentRole) {
    const newRole = currentRole === 'admin' ? 'user' : 'admin';
    const newPassword = prompt('Enter new password (leave empty to keep current):');

    const updates = {role: newRole};
    if (newPassword) updates.password = newPassword;

    try {
        const resp = await fetch(`./api/users/${userId}`, {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(updates)
        });
        const data = await resp.json();
        if (!resp.ok) {
            alert(data.error || 'Failed to update user');
            return;
        }
        loadUsers();
    } catch (e) {
        alert('Failed to update user: ' + e.message);
    }
}

async function deleteUser(userId, username) {
    if (!confirm(`Delete user "${username}"? This cannot be undone.`)) return;

    try {
        const resp = await fetch(`./api/users/${userId}`, {method: 'DELETE'});
        const data = await resp.json();
        if (!resp.ok) {
            alert(data.error || 'Failed to delete user');
            return;
        }
        loadUsers();
    } catch (e) {
        alert('Failed to delete user: ' + e.message);
    }
}
```

**Step 2: Call loadUsers on page load**

Find the `DOMContentLoaded` event handler or initialization code and add:

```javascript
if (USER_ROLE === 'admin') {
    loadUsers();
}
```

**Step 3: Verify syntax**

Run: `python -m py_compile src/lm_geotorget/management/server.py`

Expected: No output (success)

---

## Task 11: Hide Admin Panels from Users

**Files:**
- Modify: `src/lm_geotorget/management/server.py` (in `generate_dashboard_html`)

**Step 1: Wrap Database Config panel in role check**

Find the database config section (starts with `<section class="db-config"` or similar) and wrap it:

```html
{'''
<!-- Database Config Section -->
<section class="db-config">
    ... existing content ...
</section>
''' if role == 'admin' else ''}
```

**Step 2: Wrap Orders panel in role check**

Find the orders section and wrap it similarly:

```html
{'''
<!-- Orders Section -->
<section class="panel" id="orders-panel">
    ... existing content ...
</section>
''' if role == 'admin' else ''}
```

**Step 3: Simplify status bar for users**

Find the status bar section and modify to show simplified version for users:

For admin, show full status bar with DB config details.
For user, show just connection status:

```html
{'''
<div class="status-bar">
    <div class="status-item">
        <span class="status-dot" id="db-status-dot"></span>
        <span id="db-status-text">Database: Checking...</span>
    </div>
    <div class="status-item">
        <span class="status-dot" id="martin-status-dot"></span>
        <span id="martin-status-text">Tile Server: Checking...</span>
    </div>
</div>
''' if role == 'admin' else '''
<div class="status-bar">
    <div class="status-item">
        <span class="status-dot" id="db-status-dot"></span>
        <span>Status: <span id="connection-status">Checking...</span></span>
    </div>
</div>
'''}
```

**Step 4: Verify syntax**

Run: `python -m py_compile src/lm_geotorget/management/server.py`

Expected: No output (success)

---

## Task 12: Add API Documentation Panel (User Dashboard)

**Files:**
- Modify: `src/lm_geotorget/management/server.py` (in `generate_dashboard_html`)

**Step 1: Add CSS for API docs panel**

```css
/* API Documentation Panel */
.api-endpoint {{
    background: var(--dark-secondary);
    padding: 1rem;
    border-radius: 4px;
    margin-bottom: 0.75rem;
    font-family: monospace;
}}
.api-endpoint .method {{
    display: inline-block;
    padding: 0.25rem 0.5rem;
    border-radius: 4px;
    font-size: 0.75rem;
    font-weight: 600;
    margin-right: 0.5rem;
}}
.api-endpoint .method.get {{
    background: rgba(72, 187, 120, 0.2);
    color: var(--green);
}}
.api-endpoint .url {{
    color: var(--text-primary);
}}
.api-endpoint .description {{
    color: var(--text-secondary);
    font-size: 0.85rem;
    margin-top: 0.5rem;
    font-family: 'Montserrat', sans-serif;
}}
.copy-btn {{
    background: transparent;
    border: 1px solid var(--border-subtle);
    color: var(--text-secondary);
    padding: 0.25rem 0.5rem;
    border-radius: 4px;
    cursor: pointer;
    font-size: 0.75rem;
    margin-left: 0.5rem;
}}
.copy-btn:hover {{
    border-color: var(--gold);
    color: var(--gold);
}}
```

**Step 2: Add API docs panel HTML (for users only)**

Add after layers panel, for users only:

```html
{'''
<!-- API Documentation Panel (User Only) -->
<section class="panel" id="api-docs-panel">
    <div class="panel-header" onclick="togglePanel('api-docs-panel')">
        <h2>API Documentation</h2>
        <span class="toggle-icon">-</span>
    </div>
    <div class="panel-content">
        <p style="color: var(--text-secondary); margin-bottom: 1rem;">
            Use these endpoints to access geodata programmatically.
        </p>
        <div id="api-docs-content">
            <div class="api-endpoint">
                <span class="method get">GET</span>
                <span class="url" id="api-base-url">/api/layers</span>
                <button class="copy-btn" onclick="copyToClipboard(document.getElementById('api-base-url').textContent)">Copy</button>
                <div class="description">List all available layers with metadata</div>
            </div>
            <div id="layer-endpoints"></div>
        </div>
    </div>
</section>
''' if role == 'user' else ''}
```

**Step 3: Add JavaScript to generate per-layer API docs**

```javascript
// API Documentation (User Only)
function renderApiDocs(layers) {
    if (USER_ROLE !== 'user') return;

    const container = document.getElementById('layer-endpoints');
    if (!container) return;

    const baseUrl = window.location.origin + window.location.pathname.replace(/\\/$/, '');

    // Update base URL display
    const baseUrlEl = document.getElementById('api-base-url');
    if (baseUrlEl) baseUrlEl.textContent = baseUrl + '/api/layers';

    container.innerHTML = '';

    layers.forEach(layer => {
        const div = document.createElement('div');
        div.innerHTML = `
            <h4 style="color: var(--gold); margin: 1.5rem 0 0.75rem; font-size: 0.9rem;">${escapeHtml(layer.name || layer)}</h4>
            <div class="api-endpoint">
                <span class="method get">GET</span>
                <span class="url">${baseUrl}/api/layers/${encodeURIComponent(layer.name || layer)}</span>
                <button class="copy-btn" onclick="copyToClipboard('${baseUrl}/api/layers/${encodeURIComponent(layer.name || layer)}')">Copy</button>
                <div class="description">Get layer metadata and schema</div>
            </div>
            <div class="api-endpoint">
                <span class="method get">GET</span>
                <span class="url">${baseUrl}/api/layers/${encodeURIComponent(layer.name || layer)}/features?bbox=x1,y1,x2,y2</span>
                <button class="copy-btn" onclick="copyToClipboard('${baseUrl}/api/layers/${encodeURIComponent(layer.name || layer)}/features')">Copy</button>
                <div class="description">Query features with optional bounding box filter</div>
            </div>
        `;
        container.appendChild(div);
    });
}

function copyToClipboard(text) {
    navigator.clipboard.writeText(text).then(() => {
        // Brief visual feedback could be added here
    });
}
```

**Step 4: Call renderApiDocs when layers load**

Find where layers are loaded/rendered and add a call to `renderApiDocs(layers)` after rendering.

**Step 5: Verify syntax**

Run: `python -m py_compile src/lm_geotorget/management/server.py`

Expected: No output (success)

---

## Task 13: Scope Chat for Users

**Files:**
- Modify: `src/lm_geotorget/management/server.py:1049-1137` (chat context endpoint)

**Step 1: Modify get_chat_context to filter tables for users**

In the `get_chat_context` function, add role-based filtering. After getting all tables, filter to only published layers for users:

Find this code (around line 1069):
```python
tables = [row[0] for row in cur.fetchall()]
```

Add after it:
```python
            # For non-admin users, filter to only published layer tables
            if session.get('role') != 'admin':
                # Get published layers from _metadata
                cur.execute(f"""
                    SELECT DISTINCT layer_name FROM "{schema_name}"._metadata
                """)
                published = set(row[0] for row in cur.fetchall())
                tables = [t for t in tables if t in published or t == '_metadata']
```

**Step 2: Modify system prompt for users**

Find where the system prompt is built in the chat context (search for "system" in the response). Add role-based context:

```python
            # Add role-based instruction
            role_context = ""
            if session.get('role') != 'admin':
                role_context = "You are helping a user explore published geodata layers. Only query the tables shown in the schema - do not attempt to access any other tables."
```

Include `role_context` in the returned context.

**Step 3: Verify syntax**

Run: `python -m py_compile src/lm_geotorget/management/server.py`

Expected: No output (success)

---

## Task 14: Add Username Display in Header

**Files:**
- Modify: `src/lm_geotorget/management/server.py` (in `generate_dashboard_html` header section)

**Step 1: Add username and logout to header**

Find the header nav section (around line with `.dtcc-header nav`) and modify to show username:

```html
<div class="dtcc-header">
    <div class="logo">
        <img src="https://www.dtcc.chalmers.se/dtcc-logo.png" alt="DTCC">
        <div class="logo-text">Digital Twin<br>Cities Centre</div>
    </div>
    <nav>
        <span style="color: var(--text-secondary); margin-right: 1rem;">{username} ({role})</span>
        <a href="./logout">Logout</a>
    </nav>
</div>
```

**Step 2: Verify syntax**

Run: `python -m py_compile src/lm_geotorget/management/server.py`

Expected: No output (success)

---

## Task 15: Final Integration Test

**Step 1: Start the server**

Run: `cd /Users/vasnas/scratch/dtcc-geodb && python -c "from src.lm_geotorget.management.server import create_management_app; app = create_management_app('./downloads'); print('App created successfully')"`

Expected: `App created successfully`

**Step 2: Manual testing checklist**

1. Start server and login with env var credentials (bootstrap mode)
2. Verify admin sees all panels including User Management
3. Create a new user with 'user' role
4. Logout and login as the new user
5. Verify user sees only: Layers, API Docs, Chat (scoped)
6. Verify user cannot access admin routes (returns 403)
7. Login as admin again, verify env var login no longer works (DB admin exists)

---

## Summary

**Total Tasks:** 15

**Files Modified:**
- `src/lm_geotorget/management/server.py` (all changes)

**New Capabilities:**
- Database-backed user management
- Admin and User roles
- Bootstrap via env vars until DB admin exists
- Role-based dashboard rendering
- User management panel for admins
- API documentation panel for users
- Chat scoped to published layers for users
