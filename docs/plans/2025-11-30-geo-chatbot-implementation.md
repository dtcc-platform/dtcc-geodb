# Geo Chatbot Implementation Plan

Based on: `docs/plans/2025-11-30-geo-chatbot-design.md`

## Task 1: Add `/api/chat/context` Endpoint

**File:** `src/lm_geotorget/management/server.py`

**Location:** After the existing `/api/layers/<name>/features` route (around line 550)

**Add this route:**

```python
@app.route('/api/chat/context')
def get_chat_context():
    """Return database context for Claude's system prompt."""
    if not db_connection:
        return jsonify({'error': 'Database not configured'}), 400

    try:
        conn = psycopg2.connect(db_connection)
        cur = conn.cursor()

        # Get all tables in schema
        cur.execute("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = %s AND table_type = 'BASE TABLE'
        """, (schema,))
        tables = [row[0] for row in cur.fetchall()]

        schema_info = {}
        samples = {}

        for table in tables:
            full_name = f"{schema}.{table}"

            # Get column info
            cur.execute("""
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                ORDER BY ordinal_position
            """, (schema, table))
            columns = [{'name': row[0], 'type': row[1]} for row in cur.fetchall()]

            # Get geometry info if exists
            cur.execute("""
                SELECT type, srid
                FROM geometry_columns
                WHERE f_table_schema = %s AND f_table_name = %s
                LIMIT 1
            """, (schema, table))
            geom_row = cur.fetchone()

            # Get row count
            cur.execute(f'SELECT COUNT(*) FROM {schema}."{table}"')
            row_count = cur.fetchone()[0]

            schema_info[full_name] = {
                'columns': columns,
                'geometry_type': geom_row[0] if geom_row else None,
                'srid': geom_row[1] if geom_row else None,
                'row_count': row_count
            }

            # Get sample data (3 rows, excluding geometry)
            non_geom_cols = [c['name'] for c in columns if c['type'] != 'USER-DEFINED']
            if non_geom_cols:
                cols_sql = ', '.join([f'"{c}"' for c in non_geom_cols[:10]])  # Limit columns
                cur.execute(f'SELECT {cols_sql} FROM {schema}."{table}" LIMIT 3')
                sample_rows = []
                for row in cur.fetchall():
                    sample_rows.append(dict(zip(non_geom_cols[:10], row)))
                samples[full_name] = sample_rows

        # Get metadata
        cur.execute(f"""
            SELECT DISTINCT objektidentitet
            FROM {schema}.byggnad
            LIMIT 5
        """)

        cur.close()
        conn.close()

        return jsonify({
            'schema': schema_info,
            'samples': samples,
            'metadata': {
                'schema_name': schema,
                'coordinate_system': 'SWEREF99 TM (EPSG:3006)',
                'total_tables': len(tables)
            }
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500
```

**Verification:** `curl http://localhost:5050/api/chat/context | jq .`

---

## Task 2: Add `/api/chat/query` Endpoint

**File:** `src/lm_geotorget/management/server.py`

**Location:** After `/api/chat/context`

**Add this route:**

```python
import re
import time

@app.route('/api/chat/query', methods=['POST'])
def execute_chat_query():
    """Execute validated read-only SQL query."""
    if not db_connection:
        return jsonify({'error': 'Database not configured'}), 400

    data = request.get_json()
    if not data or 'sql' not in data:
        return jsonify({'error': 'Missing sql parameter'}), 400

    sql = data['sql'].strip()

    # Validate read-only (basic check)
    sql_upper = sql.upper()
    forbidden = ['INSERT', 'UPDATE', 'DELETE', 'DROP', 'CREATE', 'ALTER', 'TRUNCATE', 'GRANT', 'REVOKE']
    for word in forbidden:
        if re.search(r'\b' + word + r'\b', sql_upper):
            return jsonify({'error': f'Query contains forbidden keyword: {word}'}), 400

    if not sql_upper.startswith('SELECT'):
        return jsonify({'error': 'Only SELECT queries are allowed'}), 400

    try:
        conn = psycopg2.connect(db_connection)
        cur = conn.cursor()

        # Set statement timeout (30 seconds)
        cur.execute("SET statement_timeout = '30s'")

        start_time = time.time()
        cur.execute(sql)

        # Limit results
        rows = cur.fetchmany(1000)
        columns = [desc[0] for desc in cur.description] if cur.description else []

        # Convert to list of dicts
        results = []
        for row in rows:
            row_dict = {}
            for i, val in enumerate(row):
                # Handle special types
                if hasattr(val, 'isoformat'):
                    row_dict[columns[i]] = val.isoformat()
                elif isinstance(val, (bytes, memoryview)):
                    row_dict[columns[i]] = '<binary>'
                else:
                    row_dict[columns[i]] = val
            results.append(row_dict)

        execution_time = (time.time() - start_time) * 1000

        cur.close()
        conn.close()

        return jsonify({
            'success': True,
            'results': results,
            'row_count': len(results),
            'columns': columns,
            'execution_time_ms': round(execution_time, 2),
            'truncated': len(rows) == 1000
        })

    except psycopg2.Error as e:
        return jsonify({'error': f'Database error: {str(e)}'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500
```

**Verification:**
```bash
curl -X POST http://localhost:5050/api/chat/query \
  -H "Content-Type: application/json" \
  -d '{"sql": "SELECT COUNT(*) FROM geotorget.byggnad"}'
```

---

## Task 3: Add Chat Widget CSS

**File:** `src/lm_geotorget/management/server.py`

**Location:** Inside `generate_dashboard_html()`, add to the `<style>` section (before `</style>`):

```css
/* Chat Widget Styles */
.chat-toggle {
    position: fixed;
    bottom: 24px;
    right: 24px;
    width: 56px;
    height: 56px;
    border-radius: 50%;
    background: linear-gradient(135deg, #d4af37, #b8942e);
    border: none;
    cursor: pointer;
    box-shadow: 0 4px 20px rgba(212, 175, 55, 0.4);
    z-index: 1000;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: transform 0.2s, box-shadow 0.2s;
}
.chat-toggle:hover {
    transform: scale(1.05);
    box-shadow: 0 6px 24px rgba(212, 175, 55, 0.5);
}
.chat-toggle svg {
    width: 28px;
    height: 28px;
    fill: #1a1a2e;
}
.chat-window {
    position: fixed;
    bottom: 90px;
    right: 24px;
    width: 400px;
    height: 500px;
    background: #1a1a2e;
    border: 1px solid #333;
    border-radius: 12px;
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.5);
    z-index: 1001;
    display: none;
    flex-direction: column;
    overflow: hidden;
}
.chat-window.open { display: flex; }
.chat-header {
    padding: 16px;
    background: #252540;
    border-bottom: 1px solid #333;
    display: flex;
    justify-content: space-between;
    align-items: center;
}
.chat-header h3 {
    margin: 0;
    color: #d4af37;
    font-size: 16px;
}
.chat-close {
    background: none;
    border: none;
    color: #888;
    font-size: 24px;
    cursor: pointer;
    padding: 0;
    line-height: 1;
}
.chat-close:hover { color: #fff; }
.chat-api-key {
    padding: 12px 16px;
    background: #252540;
    border-bottom: 1px solid #333;
}
.chat-api-key input {
    width: 100%;
    padding: 8px 12px;
    background: #1a1a2e;
    border: 1px solid #444;
    border-radius: 6px;
    color: #fff;
    font-size: 13px;
}
.chat-api-key input::placeholder { color: #666; }
.chat-api-key.hidden { display: none; }
.chat-messages {
    flex: 1;
    overflow-y: auto;
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 12px;
}
.chat-message {
    max-width: 85%;
    padding: 10px 14px;
    border-radius: 12px;
    font-size: 14px;
    line-height: 1.4;
}
.chat-message.user {
    align-self: flex-end;
    background: linear-gradient(135deg, #d4af37, #b8942e);
    color: #1a1a2e;
}
.chat-message.assistant {
    align-self: flex-start;
    background: #252540;
    color: #e0e0e0;
}
.chat-message.error {
    background: #4a1a1a;
    color: #ff6b6b;
}
.chat-message pre {
    background: #1a1a2e;
    padding: 8px;
    border-radius: 6px;
    overflow-x: auto;
    margin: 8px 0;
    font-size: 12px;
}
.chat-message code {
    font-family: 'Monaco', 'Consolas', monospace;
}
.chat-message table {
    width: 100%;
    border-collapse: collapse;
    margin: 8px 0;
    font-size: 12px;
}
.chat-message th, .chat-message td {
    padding: 4px 8px;
    border: 1px solid #444;
    text-align: left;
}
.chat-message th { background: #1a1a2e; }
.chat-input-area {
    padding: 12px 16px;
    background: #252540;
    border-top: 1px solid #333;
    display: flex;
    gap: 8px;
}
.chat-input-area input {
    flex: 1;
    padding: 10px 14px;
    background: #1a1a2e;
    border: 1px solid #444;
    border-radius: 20px;
    color: #fff;
    font-size: 14px;
}
.chat-input-area input:focus {
    outline: none;
    border-color: #d4af37;
}
.chat-input-area button {
    padding: 10px 16px;
    background: linear-gradient(135deg, #d4af37, #b8942e);
    border: none;
    border-radius: 20px;
    color: #1a1a2e;
    font-weight: 600;
    cursor: pointer;
}
.chat-input-area button:hover { opacity: 0.9; }
.chat-input-area button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
}
.chat-typing {
    display: flex;
    gap: 4px;
    padding: 10px 14px;
}
.chat-typing span {
    width: 8px;
    height: 8px;
    background: #666;
    border-radius: 50%;
    animation: typing 1.4s infinite;
}
.chat-typing span:nth-child(2) { animation-delay: 0.2s; }
.chat-typing span:nth-child(3) { animation-delay: 0.4s; }
@keyframes typing {
    0%, 60%, 100% { transform: translateY(0); }
    30% { transform: translateY(-4px); }
}
```

---

## Task 4: Add Chat Widget HTML

**File:** `src/lm_geotorget/management/server.py`

**Location:** Inside `generate_dashboard_html()`, add before the closing `</body>` tag:

```html
<!-- Chat Widget -->
<button class="chat-toggle" id="chatToggle" title="Geo Assistant">
    <svg viewBox="0 0 24 24"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm0 14H6l-2 2V4h16v12z"/></svg>
</button>
<div class="chat-window" id="chatWindow">
    <div class="chat-header">
        <h3>Geo Assistant</h3>
        <button class="chat-close" id="chatClose">&times;</button>
    </div>
    <div class="chat-api-key" id="chatApiKey">
        <input type="password" id="apiKeyInput" placeholder="Enter your Claude API key...">
    </div>
    <div class="chat-messages" id="chatMessages"></div>
    <div class="chat-input-area">
        <input type="text" id="chatInput" placeholder="Ask about your geodata...">
        <button id="chatSend">Send</button>
    </div>
</div>
```

---

## Task 5: Add GeoChat JavaScript Object

**File:** `src/lm_geotorget/management/server.py`

**Location:** Inside `generate_dashboard_html()`, add to the `<script>` section (after the existing JavaScript, before `</script>`):

```javascript
// Geo Chat Assistant
var GeoChat = {
    apiKey: null,
    context: null,
    messages: [],

    init: function() {
        var self = this;

        // Load saved API key
        this.apiKey = localStorage.getItem('claude_api_key');
        if (this.apiKey) {
            document.getElementById('chatApiKey').classList.add('hidden');
        }

        // Event listeners
        document.getElementById('chatToggle').addEventListener('click', function() {
            document.getElementById('chatWindow').classList.toggle('open');
            if (!self.context) self.loadContext();
        });

        document.getElementById('chatClose').addEventListener('click', function() {
            document.getElementById('chatWindow').classList.remove('open');
        });

        document.getElementById('apiKeyInput').addEventListener('change', function(e) {
            self.apiKey = e.target.value;
            localStorage.setItem('claude_api_key', self.apiKey);
            document.getElementById('chatApiKey').classList.add('hidden');
        });

        document.getElementById('chatSend').addEventListener('click', function() {
            self.sendMessage();
        });

        document.getElementById('chatInput').addEventListener('keypress', function(e) {
            if (e.key === 'Enter') self.sendMessage();
        });
    },

    loadContext: function() {
        var self = this;
        fetch('/api/chat/context')
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.error) {
                    self.addMessage('error', 'Failed to load database context: ' + data.error);
                } else {
                    self.context = data;
                }
            })
            .catch(function(err) {
                self.addMessage('error', 'Failed to connect to server');
            });
    },

    buildSystemPrompt: function() {
        if (!this.context) return '';

        var prompt = 'You are a geodata assistant for Swedish Lantmateriet data stored in PostGIS.\\n\\n';
        prompt += 'DATABASE CONTEXT:\\n';
        prompt += '- Schema: ' + this.context.metadata.schema_name + '\\n';
        prompt += '- Coordinate system: ' + this.context.metadata.coordinate_system + '\\n';
        prompt += '- Total tables: ' + this.context.metadata.total_tables + '\\n\\n';

        prompt += 'TABLE SCHEMAS:\\n';
        for (var table in this.context.schema) {
            var info = this.context.schema[table];
            prompt += '\\n' + table + ' (' + info.row_count + ' rows):\\n';
            if (info.geometry_type) {
                prompt += '  Geometry: ' + info.geometry_type + ' (SRID: ' + info.srid + ')\\n';
            }
            prompt += '  Columns: ';
            var cols = info.columns.map(function(c) { return c.name + ' (' + c.type + ')'; });
            prompt += cols.join(', ') + '\\n';
        }

        prompt += '\\nSAMPLE DATA:\\n';
        for (var table in this.context.samples) {
            prompt += '\\n' + table + ':\\n';
            prompt += JSON.stringify(this.context.samples[table], null, 2) + '\\n';
        }

        prompt += '\\nINSTRUCTIONS:\\n';
        prompt += '- Generate PostgreSQL/PostGIS SQL to answer user questions\\n';
        prompt += '- Return SQL wrapped in ```sql code blocks\\n';
        prompt += '- For spatial queries, use ST_* PostGIS functions\\n';
        prompt += '- Keep queries efficient (use LIMIT when appropriate)\\n';
        prompt += '- After I execute your SQL, I will give you the results and you should summarize them\\n';

        return prompt;
    },

    addMessage: function(role, content) {
        var container = document.getElementById('chatMessages');
        var div = document.createElement('div');
        div.className = 'chat-message ' + role;

        // Parse markdown-like content
        var html = this.formatMessage(content);
        div.innerHTML = html;

        container.appendChild(div);
        container.scrollTop = container.scrollHeight;

        if (role !== 'error') {
            this.messages.push({ role: role, content: content });
        }
    },

    formatMessage: function(text) {
        // Escape HTML first
        var escaped = text.replace(/&/g, '&amp;')
                         .replace(/</g, '&lt;')
                         .replace(/>/g, '&gt;');

        // Format code blocks
        escaped = escaped.replace(/```(\\w*)\\n([\\s\\S]*?)```/g, function(match, lang, code) {
            return '<pre><code>' + code.trim() + '</code></pre>';
        });

        // Format inline code
        escaped = escaped.replace(/`([^`]+)`/g, '<code>$1</code>');

        // Format newlines
        escaped = escaped.replace(/\\n/g, '<br>');

        return escaped;
    },

    showTyping: function() {
        var container = document.getElementById('chatMessages');
        var div = document.createElement('div');
        div.className = 'chat-message assistant chat-typing';
        div.id = 'typingIndicator';

        // Create typing dots using safe DOM methods
        for (var i = 0; i < 3; i++) {
            var span = document.createElement('span');
            div.appendChild(span);
        }

        container.appendChild(div);
        container.scrollTop = container.scrollHeight;
    },

    hideTyping: function() {
        var el = document.getElementById('typingIndicator');
        if (el) el.remove();
    },

    sendMessage: async function() {
        var input = document.getElementById('chatInput');
        var text = input.value.trim();
        if (!text) return;

        if (!this.apiKey) {
            this.addMessage('error', 'Please enter your Claude API key first');
            return;
        }

        if (!this.context) {
            this.addMessage('error', 'Database context not loaded. Please wait...');
            this.loadContext();
            return;
        }

        // Add user message
        this.addMessage('user', text);
        input.value = '';

        // Disable input
        var sendBtn = document.getElementById('chatSend');
        sendBtn.disabled = true;

        this.showTyping();

        try {
            // Call Claude API
            var response = await this.callClaude(text);
            this.hideTyping();

            // Check for SQL in response
            var sqlMatch = response.match(/```sql\\n([\\s\\S]*?)```/);
            if (sqlMatch) {
                // Execute SQL
                this.addMessage('assistant', response);

                var sql = sqlMatch[1].trim();
                var results = await this.executeSQL(sql);

                if (results.error) {
                    this.addMessage('error', 'SQL Error: ' + results.error);
                } else {
                    // Format results as table
                    var resultText = this.formatResults(results);
                    this.addMessage('assistant', resultText);

                    // Ask Claude to summarize
                    this.showTyping();
                    var summary = await this.callClaude('Here are the SQL results:\\n' + resultText + '\\n\\nPlease summarize these results for the user.');
                    this.hideTyping();
                    this.addMessage('assistant', summary);
                }
            } else {
                this.addMessage('assistant', response);
            }
        } catch (err) {
            this.hideTyping();
            this.addMessage('error', 'Error: ' + err.message);
        }

        sendBtn.disabled = false;
    },

    callClaude: async function(userMessage) {
        var messages = [
            { role: 'user', content: userMessage }
        ];

        // Include conversation history (last 10 messages)
        var history = this.messages.slice(-10);
        if (history.length > 0) {
            messages = history.map(function(m) {
                return { role: m.role === 'user' ? 'user' : 'assistant', content: m.content };
            });
            messages.push({ role: 'user', content: userMessage });
        }

        var response = await fetch('https://api.anthropic.com/v1/messages', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'x-api-key': this.apiKey,
                'anthropic-version': '2023-06-01',
                'anthropic-dangerous-direct-browser-access': 'true'
            },
            body: JSON.stringify({
                model: 'claude-sonnet-4-20250514',
                max_tokens: 1024,
                system: this.buildSystemPrompt(),
                messages: messages
            })
        });

        if (!response.ok) {
            var error = await response.json();
            throw new Error(error.error?.message || 'API request failed');
        }

        var data = await response.json();
        return data.content[0].text;
    },

    executeSQL: async function(sql) {
        var response = await fetch('/api/chat/query', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ sql: sql })
        });
        return response.json();
    },

    formatResults: function(results) {
        if (!results.results || results.results.length === 0) {
            return 'Query returned no results.';
        }

        var text = 'Results (' + results.row_count + ' rows, ' + results.execution_time_ms + 'ms):\\n\\n';

        // Simple table format
        var cols = results.columns;
        text += '| ' + cols.join(' | ') + ' |\\n';
        text += '| ' + cols.map(function() { return '---'; }).join(' | ') + ' |\\n';

        results.results.slice(0, 20).forEach(function(row) {
            var vals = cols.map(function(c) {
                var v = row[c];
                if (v === null) return 'NULL';
                if (typeof v === 'object') return JSON.stringify(v);
                return String(v).substring(0, 50);
            });
            text += '| ' + vals.join(' | ') + ' |\\n';
        });

        if (results.truncated) {
            text += '\\n(Results truncated to 1000 rows)';
        }

        return text;
    }
};

// Initialize chat on page load
document.addEventListener('DOMContentLoaded', function() {
    GeoChat.init();
});
```

---

## Task 6: Integration Testing

**After implementing all tasks:**

1. Start the server: `python manage_server.py`
2. Open dashboard at `http://localhost:5050`
3. Click the gold chat bubble in bottom-right
4. Enter your Claude API key
5. Test queries:
   - "How many buildings are there?"
   - "What tables are available?"
   - "Show me the first 5 buildings"
   - "What is the total area of all buildings?"

**Expected behavior:**
- Chat opens with API key prompt (first time)
- After entering key, prompt hides
- User questions appear on right (gold)
- Claude responses appear on left (dark)
- SQL queries shown in code blocks
- Results formatted as tables
- Natural language summaries provided
