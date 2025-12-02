"""
Management server for Geotorget dashboard operations.

Provides:
- Dashboard UI serving
- Download triggering
- Publish to PostGIS
- Status monitoring

This is separate from the serving API which handles geodata queries.
"""

import json
import os
import shutil
import subprocess
import threading
import time
import queue
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, asdict

try:
    from flask import Flask, request, jsonify, Response, stream_with_context, session, redirect, url_for
    from functools import wraps
    HAS_FLASK = True
except ImportError:
    HAS_FLASK = False


def login_required(f):
    """Decorator to require login for routes."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({'error': 'Authentication required'}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


@dataclass
class PublishProgress:
    """Progress update for publish operations."""
    order_id: str
    status: str  # 'starting', 'processing', 'completed', 'error'
    current_layer: Optional[str] = None
    layers_done: int = 0
    layers_total: int = 0
    message: Optional[str] = None
    error: Optional[str] = None


@dataclass
class DownloadProgress:
    """Progress update for download operations."""
    order_id: str
    status: str  # 'starting', 'fetching_list', 'downloading', 'completed', 'error'
    current_file: Optional[str] = None
    files_done: int = 0
    files_total: int = 0
    bytes_downloaded: int = 0
    bytes_total: int = 0
    message: Optional[str] = None
    error: Optional[str] = None


class MartinManager:
    """Manages Martin tile server subprocess."""

    def __init__(self, db_connection: str, port: int = 3000):
        self.db_connection = db_connection
        self.port = port
        self.process: Optional[subprocess.Popen] = None
        self._config_path = Path(__file__).parent.parent.parent.parent / 'martin.yaml'

    @staticmethod
    def is_installed() -> bool:
        """Check if Martin is installed."""
        return shutil.which('martin') is not None

    def start(self) -> bool:
        """Start Martin server."""
        if not self.is_installed():
            return False

        if self.process and self.process.poll() is None:
            return True  # Already running

        # Check if config exists
        if not self._config_path.exists():
            print(f"Martin config not found at {self._config_path}")
            return False

        # Build environment with database URL
        env = os.environ.copy()
        env['DATABASE_URL'] = self.db_connection

        try:
            self.process = subprocess.Popen(
                ['martin', '--config', str(self._config_path)],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self._config_path.parent)
            )

            # Wait briefly and check if started
            time.sleep(2)
            if self.process.poll() is not None:
                # Process exited, read error
                _, stderr = self.process.communicate()
                print(f"Martin failed to start: {stderr.decode()}")
                return False

            return True
        except Exception as e:
            print(f"Failed to start Martin: {e}")
            return False

    def stop(self):
        """Stop Martin server."""
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None

    def is_running(self) -> bool:
        """Check if Martin is running by making an HTTP request."""
        import urllib.request
        import urllib.error
        try:
            req = urllib.request.Request(
                f'http://127.0.0.1:{self.port}/health',
                method='GET'
            )
            with urllib.request.urlopen(req, timeout=2) as response:
                return response.status == 200
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ConnectionRefusedError):
            # Try the catalog endpoint as fallback (older Martin versions)
            try:
                req = urllib.request.Request(
                    f'http://127.0.0.1:{self.port}/catalog',
                    method='GET'
                )
                with urllib.request.urlopen(req, timeout=2) as response:
                    return response.status == 200
            except Exception:
                return False
        except Exception:
            return False

    def get_catalog_url(self) -> str:
        """Get Martin catalog URL."""
        return f'http://127.0.0.1:{self.port}/catalog'

    def get_tile_url(self, table: str) -> str:
        """Get tile URL template for a table."""
        return f'http://127.0.0.1:{self.port}/{table}/{{z}}/{{x}}/{{y}}'


def create_management_app(
    downloads_dir: Path,
    db_connection: Optional[str] = None,
    schema: str = "geotorget"
) -> "Flask":
    """
    Create the Flask management application.

    Args:
        downloads_dir: Directory for downloaded orders
        db_connection: PostgreSQL connection string (optional, can be set later)
        schema: Schema name for PostGIS tables

    Returns:
        Configured Flask app
    """
    if not HAS_FLASK:
        raise ImportError(
            "Flask is required for the management server. "
            "Install with: pip install flask"
        )

    app = Flask(__name__)
    app.config['downloads_dir'] = Path(downloads_dir)
    app.config['db_connection'] = db_connection
    app.config['schema'] = schema

    # Authentication config - can be set via environment variables
    app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
    app.config['AUTH_USERNAME'] = os.environ.get('AUTH_USERNAME', 'admin')
    app.config['AUTH_PASSWORD'] = os.environ.get('AUTH_PASSWORD', 'admin')

    # Store for SSE progress updates
    progress_queues: dict[str, queue.Queue] = {}

    # Martin tile server manager
    martin_manager: Optional[MartinManager] = None
    if db_connection:
        martin_manager = MartinManager(db_connection)

    # ==================== Authentication ====================

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        """Login page."""
        if request.method == 'POST':
            username = request.form.get('username', '')
            password = request.form.get('password', '')

            if username == app.config['AUTH_USERNAME'] and password == app.config['AUTH_PASSWORD']:
                session['logged_in'] = True
                session['username'] = username
                return redirect(url_for('dashboard'))
            else:
                return generate_login_html(error='Invalid username or password')

        return generate_login_html()

    @app.route('/logout')
    def logout():
        """Logout and clear session."""
        session.clear()
        return redirect(url_for('login'))

    # ==================== Dashboard ====================

    @app.route('/')
    @login_required
    def dashboard():
        """Serve the dashboard HTML."""
        dashboard_path = app.config['downloads_dir'].parent / 'dashboard.html'
        if dashboard_path.exists():
            return dashboard_path.read_text()
        return generate_dashboard_html(app.config['downloads_dir'])

    @app.route('/api/config')
    @login_required
    def get_config():
        """Get current configuration."""
        db_conn = app.config['db_connection']
        # Mask password in connection string for display
        db_display = None
        if db_conn:
            import re
            # Mask password: postgresql://user:password@host -> postgresql://user:***@host
            db_display = re.sub(r'://([^:]+):([^@]+)@', r'://\1:***@', db_conn)

        return jsonify({
            'downloads_dir': str(app.config['downloads_dir']),
            'db_configured': db_conn is not None,
            'db_connection': db_conn,  # Full connection string for copying
            'db_display': db_display,  # Masked for display
            'schema': app.config['schema']
        })

    @app.route('/api/config', methods=['POST'])
    @login_required
    def set_config():
        """Update configuration."""
        nonlocal martin_manager

        data = request.json
        if 'db_connection' in data:
            app.config['db_connection'] = data['db_connection']
            # Initialize Martin manager with new connection
            if data['db_connection']:
                martin_manager = MartinManager(data['db_connection'])
        if 'schema' in data:
            app.config['schema'] = data['schema']
        return jsonify({'status': 'ok'})

    # ==================== Orders ====================

    @app.route('/api/orders')
    @login_required
    def list_orders():
        """List all downloaded orders with their status."""
        from ..tiling.processor import get_order_info

        orders = []
        downloads_dir = app.config['downloads_dir']

        if not downloads_dir.exists():
            return jsonify([])

        for order_dir in sorted(downloads_dir.iterdir()):
            if order_dir.is_dir() and not order_dir.name.startswith('.'):
                try:
                    info = get_order_info(order_dir)

                    # Check if published (if db configured)
                    published_layers = []
                    if app.config['db_connection']:
                        published_layers = get_published_layers(
                            app.config['db_connection'],
                            app.config['schema'],
                            order_dir.name
                        )

                    orders.append({
                        'order_id': info['order_id'],
                        'data_type': info['data_type'],
                        'data_type_label': info['data_type_label'],
                        'is_publishable': info['is_publishable'],
                        'layers': info['layers'],
                        'total_size_mb': info['total_size_mb'],
                        'published_layers': published_layers,
                        'is_published': len(published_layers) > 0,
                        'package_name': load_package_name(order_dir)
                    })
                except Exception as e:
                    orders.append({
                        'order_id': order_dir.name,
                        'error': str(e)
                    })

        return jsonify(orders)

    @app.route('/api/orders/<order_id>')
    @login_required
    def get_order(order_id: str):
        """Get detailed info for a single order."""
        from ..tiling.processor import get_order_info

        order_dir = app.config['downloads_dir'] / order_id
        if not order_dir.exists():
            return jsonify({'error': 'Order not found'}), 404

        try:
            info = get_order_info(order_dir)

            # Check published status
            published_layers = []
            if app.config['db_connection']:
                published_layers = get_published_layers(
                    app.config['db_connection'],
                    app.config['schema'],
                    order_id
                )

            info['published_layers'] = published_layers
            info['is_published'] = len(published_layers) > 0

            # Load package name from metadata
            info['package_name'] = load_package_name(order_dir)

            return jsonify(info)
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/orders/<order_id>/package-name', methods=['POST'])
    @login_required
    def set_package_name(order_id: str):
        """Set the LM package name for an order."""
        order_dir = app.config['downloads_dir'] / order_id
        if not order_dir.exists():
            return jsonify({'error': 'Order not found'}), 404

        data = request.json or {}
        package_name = data.get('package_name', '').strip()

        try:
            save_package_name(order_dir, package_name)
            return jsonify({'status': 'ok', 'package_name': package_name})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/orders/<order_id>/check-updates')
    @login_required
    def check_order_updates(order_id: str):
        """
        Check if there are updates available for an order.

        Returns:
            - has_update: bool
            - local_date: str (last download date)
            - remote_date: str (latest available date)
            - new_files: list of files with updates
        """
        order_dir = app.config['downloads_dir'] / order_id
        if not order_dir.exists():
            return jsonify({'error': 'Order not found'}), 404

        try:
            result = check_for_updates(order_id, order_dir)
            return jsonify(result)
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    # ==================== Download ====================

    @app.route('/api/download/<order_id>', methods=['POST'])
    @login_required
    def start_download(order_id: str):
        """
        Start downloading an order.

        No credentials required - uses public Lantmateriet download API.
        Returns immediately, use SSE endpoint for progress.
        """
        # Create progress queue for this download
        queue_key = f"download_{order_id}"
        progress_queues[queue_key] = queue.Queue()

        # Start download in background thread
        thread = threading.Thread(
            target=run_download,
            args=(
                order_id,
                app.config['downloads_dir'],
                progress_queues[queue_key]
            )
        )
        thread.start()

        return jsonify({
            'status': 'started',
            'order_id': order_id,
            'progress_url': f'/api/download/{order_id}/progress'
        })

    @app.route('/api/download/<order_id>/progress')
    @login_required
    def download_progress(order_id: str):
        """SSE endpoint for download progress updates."""
        queue_key = f"download_{order_id}"

        def generate():
            q = progress_queues.get(queue_key)
            if not q:
                yield f"data: {json.dumps({'error': 'No active download for this order'})}\n\n"
                return

            while True:
                try:
                    progress = q.get(timeout=30)
                    yield f"data: {json.dumps(asdict(progress))}\n\n"

                    if progress.status in ('completed', 'error'):
                        # Cleanup
                        if queue_key in progress_queues:
                            del progress_queues[queue_key]
                        break
                except queue.Empty:
                    # Send keepalive
                    yield f"data: {json.dumps({'status': 'keepalive'})}\n\n"

        return Response(
            stream_with_context(generate()),
            mimetype='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'X-Accel-Buffering': 'no'
            }
        )

    # ==================== Publish ====================

    @app.route('/api/orders/<order_id>/publish', methods=['POST'])
    @login_required
    def publish_order(order_id: str):
        """
        Publish an order to PostGIS.

        Expects JSON body with:
        - layers: Optional list of layer names (null = all)

        Returns immediately, use SSE endpoint for progress.
        """
        if not app.config['db_connection']:
            return jsonify({
                'error': 'Database not configured. POST to /api/config first.'
            }), 400

        order_dir = app.config['downloads_dir'] / order_id
        if not order_dir.exists():
            return jsonify({'error': 'Order not found'}), 404

        data = request.json or {}
        layers = data.get('layers')

        # Create progress queue for this order
        progress_queues[order_id] = queue.Queue()

        # Start publish in background thread
        thread = threading.Thread(
            target=run_publish,
            args=(
                order_id,
                app.config['downloads_dir'],
                app.config['db_connection'],
                app.config['schema'],
                layers,
                progress_queues[order_id]
            )
        )
        thread.start()

        return jsonify({
            'status': 'started',
            'order_id': order_id,
            'progress_url': f'/api/orders/{order_id}/publish/progress'
        })

    @app.route('/api/orders/<order_id>/publish/progress')
    @login_required
    def publish_progress(order_id: str):
        """SSE endpoint for publish progress updates."""
        def generate():
            q = progress_queues.get(order_id)
            if not q:
                yield f"data: {json.dumps({'error': 'No active publish for this order'})}\n\n"
                return

            while True:
                try:
                    progress = q.get(timeout=30)
                    yield f"data: {json.dumps(asdict(progress))}\n\n"

                    if progress.status in ('completed', 'error'):
                        # Cleanup
                        del progress_queues[order_id]
                        break
                except queue.Empty:
                    # Send keepalive
                    yield f"data: {json.dumps({'status': 'keepalive'})}\n\n"

        return Response(
            stream_with_context(generate()),
            mimetype='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'X-Accel-Buffering': 'no'
            }
        )

    # ==================== LiDAR Tiles (On-Demand) ====================

    @app.route('/api/orders/<order_id>/lidar-tiles')
    @login_required
    def get_lidar_tiles_api(order_id: str):
        """Get LiDAR tiles for an on-demand order."""
        from ..tiling.processor import get_lidar_tiles

        order_dir = app.config['downloads_dir'] / order_id
        if not order_dir.exists():
            return jsonify({'error': 'Order not found'}), 404

        tiles = get_lidar_tiles(order_dir)
        return jsonify({
            'order_id': order_id,
            'tile_count': len(tiles),
            'tiles': tiles
        })

    @app.route('/api/orders/<order_id>/lidar-tiles.geojson')
    @login_required
    def get_lidar_tiles_geojson_api(order_id: str):
        """Get LiDAR tiles as GeoJSON for map display."""
        from ..tiling.processor import get_lidar_tiles_geojson

        order_dir = app.config['downloads_dir'] / order_id
        if not order_dir.exists():
            return jsonify({'error': 'Order not found'}), 404

        geojson = get_lidar_tiles_geojson(order_dir)
        return jsonify(geojson)

    @app.route('/api/orders/<order_id>/lidar-tiles/downloaded')
    @login_required
    def get_downloaded_tiles(order_id: str):
        """Get list of already downloaded LiDAR tiles."""
        order_dir = app.config['downloads_dir'] / order_id
        if not order_dir.exists():
            return jsonify({'error': 'Order not found'}), 404

        # Check for .laz files in the tiles subdirectory
        tiles_dir = order_dir / 'tiles'
        downloaded = []
        if tiles_dir.exists():
            downloaded = [f.name for f in tiles_dir.glob('*.laz')]

        return jsonify({
            'order_id': order_id,
            'downloaded': downloaded,
            'count': len(downloaded)
        })

    @app.route('/api/orders/<order_id>/lidar-tiles/<tile_name>/download', methods=['GET', 'POST'])
    @login_required
    def download_lidar_tile(order_id: str, tile_name: str):
        """
        GET: Get info about a tile
        POST: Start downloading a specific LiDAR tile to the server (with SSE progress)
        """
        from ..tiling.processor import get_lidar_tiles
        import requests as req_lib

        order_dir = app.config['downloads_dir'] / order_id
        if not order_dir.exists():
            return jsonify({'error': 'Order not found'}), 404

        tiles = get_lidar_tiles(order_dir)
        tile = next((t for t in tiles if t['filename'] == tile_name), None)

        if not tile:
            return jsonify({'error': 'Tile not found'}), 404

        # GET request - just return tile info
        if request.method == 'GET':
            tiles_dir = order_dir / 'tiles'
            is_downloaded = (tiles_dir / tile_name).exists() if tiles_dir.exists() else False
            return jsonify({
                'filename': tile['filename'],
                'href': tile['href'],
                'size_mb': tile['size_mb'],
                'downloaded': is_downloaded
            })

        # POST request - download the tile with progress streaming
        tiles_dir = order_dir / 'tiles'
        tiles_dir.mkdir(exist_ok=True)
        tile_path = tiles_dir / tile_name

        # Check if already downloaded
        if tile_path.exists():
            return jsonify({
                'status': 'already_downloaded',
                'filename': tile_name,
                'path': str(tile_path)
            })

        def generate_progress():
            try:
                response = req_lib.get(tile['href'], stream=True, timeout=300)
                response.raise_for_status()

                total_size = int(response.headers.get('content-length', 0))
                downloaded = 0
                chunk_size = 65536  # 64KB chunks

                with open(tile_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=chunk_size):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total_size > 0:
                            percent = int((downloaded / total_size) * 100)
                            yield f"data: {json.dumps({'status': 'downloading', 'percent': percent, 'downloaded': downloaded, 'total': total_size})}\n\n"

                yield f"data: {json.dumps({'status': 'downloaded', 'filename': tile_name, 'size_mb': tile['size_mb']})}\n\n"

            except req_lib.RequestException as e:
                # Clean up partial file
                if tile_path.exists():
                    tile_path.unlink()
                yield f"data: {json.dumps({'status': 'error', 'error': str(e)})}\n\n"

        return Response(
            stream_with_context(generate_progress()),
            mimetype='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'X-Accel-Buffering': 'no'
            }
        )

    @app.route('/api/orders/<order_id>/lidar-tiles/<tile_name>/file')
    @login_required
    def serve_lidar_tile(order_id: str, tile_name: str):
        """Serve a downloaded LiDAR tile file."""
        from flask import send_file

        order_dir = app.config['downloads_dir'] / order_id
        if not order_dir.exists():
            return jsonify({'error': 'Order not found'}), 404

        tiles_dir = order_dir / 'tiles'
        tile_path = tiles_dir / tile_name

        if not tile_path.exists():
            return jsonify({'error': 'Tile not downloaded yet'}), 404

        return send_file(
            tile_path,
            mimetype='application/octet-stream',
            as_attachment=True,
            download_name=tile_name
        )

    # ==================== Martin Tile Server ====================

    @app.route('/api/martin/status')
    @login_required
    def martin_status():
        """Get Martin server status."""
        nonlocal martin_manager

        installed = MartinManager.is_installed()

        # Create a temporary manager to check status even if not initialized
        if not martin_manager and app.config['db_connection']:
            martin_manager = MartinManager(app.config['db_connection'])

        if not martin_manager:
            # Still check if Martin is running on default port
            temp_manager = MartinManager('')
            is_running = temp_manager.is_running()
            return jsonify({
                'installed': installed,
                'running': is_running,
                'configured': False,
                'port': 3000,
                'catalog_url': 'http://127.0.0.1:3000/catalog' if is_running else None,
                'message': 'Database not configured' if not app.config['db_connection'] else 'Martin not initialized'
            })

        return jsonify({
            'installed': installed,
            'running': martin_manager.is_running(),
            'configured': True,
            'port': martin_manager.port,
            'catalog_url': martin_manager.get_catalog_url() if martin_manager.is_running() else None
        })

    @app.route('/api/martin/start', methods=['POST'])
    @login_required
    def start_martin():
        """Start Martin server."""
        nonlocal martin_manager

        if not app.config['db_connection']:
            return jsonify({'error': 'Database not configured'}), 400

        if not MartinManager.is_installed():
            return jsonify({
                'error': 'Martin not installed. Install with: brew install martin (macOS) or download from https://github.com/maplibre/martin'
            }), 400

        if not martin_manager:
            martin_manager = MartinManager(app.config['db_connection'])

        if martin_manager.is_running():
            return jsonify({
                'status': 'already_running',
                'catalog_url': martin_manager.get_catalog_url()
            })

        if martin_manager.start():
            return jsonify({
                'status': 'started',
                'catalog_url': martin_manager.get_catalog_url()
            })
        else:
            return jsonify({'error': 'Failed to start Martin. Check logs for details.'}), 500

    @app.route('/api/martin/stop', methods=['POST'])
    @login_required
    def stop_martin():
        """Stop Martin server."""
        nonlocal martin_manager

        if martin_manager:
            martin_manager.stop()
            return jsonify({'status': 'stopped'})
        return jsonify({'status': 'not_running'})

    @app.route('/api/martin/restart', methods=['POST'])
    @login_required
    def restart_martin():
        """Restart Martin server to pick up new tables."""
        nonlocal martin_manager

        if not martin_manager:
            if not app.config['db_connection']:
                return jsonify({'error': 'Database not configured'}), 400
            martin_manager = MartinManager(app.config['db_connection'])

        if not martin_manager.is_installed():
            return jsonify({'error': 'Martin not installed'}), 400

        # Stop if running
        if martin_manager.is_running():
            martin_manager.stop()

        # Start again
        if martin_manager.start():
            return jsonify({
                'status': 'restarted',
                'port': martin_manager.port,
                'catalog_url': f'http://127.0.0.1:{martin_manager.port}/catalog'
            })
        return jsonify({'error': 'Failed to restart Martin'}), 500

    # ==================== Database Status ====================

    @app.route('/api/db/status')
    @login_required
    def db_status():
        """Get PostGIS database status."""
        if not app.config['db_connection']:
            return jsonify({
                'configured': False,
                'error': 'Database not configured'
            })

        try:
            from ..tiling.postgis_loader import PostGISLoader

            loader = PostGISLoader(
                app.config['db_connection'],
                app.config['schema']
            )

            tables = loader.list_tables()
            metadata = loader.get_metadata()

            # Group by order
            orders = {}
            for m in metadata:
                oid = m['order_id']
                if oid not in orders:
                    orders[oid] = {
                        'order_id': oid,
                        'layers': [],
                        'total_features': 0
                    }
                orders[oid]['layers'].append(m['layer_name'])
                orders[oid]['total_features'] += m['feature_count'] or 0

            loader.close()

            return jsonify({
                'configured': True,
                'connected': True,
                'schema': app.config['schema'],
                'table_count': len(tables),
                'tables': tables,
                'orders': list(orders.values()),
                'total_features': sum(o['total_features'] for o in orders.values())
            })
        except Exception as e:
            return jsonify({
                'configured': True,
                'connected': False,
                'error': str(e)
            })

    @app.route('/api/db/init', methods=['POST'])
    @login_required
    def init_db():
        """Initialize the PostGIS database."""
        if not app.config['db_connection']:
            return jsonify({
                'error': 'Database not configured. POST to /api/config first.'
            }), 400

        try:
            from ..tiling.postgis_loader import PostGISLoader

            loader = PostGISLoader(
                app.config['db_connection'],
                app.config['schema']
            )
            loader.init_database()
            loader.close()

            return jsonify({
                'status': 'ok',
                'message': f'Database initialized with schema "{app.config["schema"]}"'
            })
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    # ==================== Layers API ====================

    @app.route('/api/layers')
    @login_required
    def list_layers_api():
        """List all available layers."""
        if not app.config['db_connection']:
            return jsonify([])

        try:
            import psycopg2

            with psycopg2.connect(app.config['db_connection']) as conn:
                with conn.cursor() as cur:
                    schema = app.config['schema']
                    # Get all geometry tables
                    cur.execute("""
                        SELECT f_table_name
                        FROM geometry_columns
                        WHERE f_table_schema = %s
                        AND f_table_name NOT LIKE '\\_%%'
                        ORDER BY f_table_name
                    """, (schema,))
                    return jsonify([row[0] for row in cur.fetchall()])
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/layers/<layer_name>')
    @login_required
    def get_layer_info_api(layer_name: str):
        """Get detailed information about a layer."""
        if not app.config['db_connection']:
            return jsonify({'error': 'Database not configured'}), 400

        try:
            import psycopg2

            with psycopg2.connect(app.config['db_connection']) as conn:
                with conn.cursor() as cur:
                    schema = app.config['schema']

                    # Get geometry info
                    cur.execute("""
                        SELECT type, srid
                        FROM geometry_columns
                        WHERE f_table_schema = %s AND f_table_name = %s
                    """, (schema, layer_name))
                    row = cur.fetchone()
                    if not row:
                        return jsonify({'error': 'Layer not found'}), 404

                    geom_type = row[0]
                    srid = row[1] if len(row) > 1 else 0

                    # Get count
                    cur.execute(f'SELECT COUNT(*) FROM "{schema}"."{layer_name}"')
                    count_row = cur.fetchone()
                    count = count_row[0] if count_row else 0

                    # Get source order
                    cur.execute(f'SELECT _source_order FROM "{schema}"."{layer_name}" LIMIT 1')
                    source_row = cur.fetchone()
                    source_order = source_row[0] if source_row else None

                    return jsonify({
                        'name': layer_name,
                        'geometry_type': geom_type,
                        'srid': srid,
                        'feature_count': count,
                        'source_order': source_order
                    })
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/layers/<layer_name>/features')
    @login_required
    def get_layer_features_api(layer_name: str):
        """Query features from a layer.

        Query params:
            bbox: Bounding box format: minX,minY,maxX,maxY
            bbox_srid: SRID of bbox coordinates (default 3006 for SWEREF99 TM)
            limit: Max features to return (default 1000, max 10000)
            srid: Output SRID (default 3006 for SWEREF99 TM, use 4326 for WGS84)
        """
        if not app.config['db_connection']:
            return jsonify({'error': 'Database not configured'}), 400

        bbox = request.args.get('bbox')
        bbox_srid = int(request.args.get('bbox_srid', 3006))  # Default bbox in SWEREF99 TM
        limit = min(int(request.args.get('limit', 1000)), 10000)
        output_srid = int(request.args.get('srid', 3006))  # Default output in SWEREF99 TM

        try:
            import psycopg2
            import psycopg2.extras

            with psycopg2.connect(app.config['db_connection']) as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    schema = app.config['schema']

                    # Build query
                    where_clause = ""
                    params = []

                    if bbox:
                        bbox_parts = [float(x) for x in bbox.split(',')]
                        if len(bbox_parts) == 4:
                            # bbox_srid specifies the coordinate system of the input bbox
                            if bbox_srid == 3006:
                                # bbox already in native SWEREF99 TM
                                where_clause = """
                                    WHERE ST_Intersects(
                                        geom,
                                        ST_MakeEnvelope(%s, %s, %s, %s, 3006)
                                    )
                                """
                            else:
                                # Transform bbox to native SRID
                                where_clause = f"""
                                    WHERE ST_Intersects(
                                        geom,
                                        ST_Transform(ST_MakeEnvelope(%s, %s, %s, %s, {bbox_srid}), 3006)
                                    )
                                """
                            params = bbox_parts

                    # Output in requested SRID (default SWEREF99 TM)
                    if output_srid == 3006:
                        geom_select = "ST_AsGeoJSON(geom)::json as geometry"
                    else:
                        geom_select = f"ST_AsGeoJSON(ST_Transform(geom, {output_srid}))::json as geometry"

                    query = f"""
                        SELECT
                            fid,
                            {geom_select},
                            *
                        FROM "{schema}"."{layer_name}"
                        {where_clause}
                        LIMIT %s
                    """
                    params.append(limit)

                    cur.execute(query, params)
                    rows = cur.fetchall()

                    # Build GeoJSON
                    features = []
                    for row in rows:
                        properties = {k: v for k, v in row.items()
                                    if k not in ('fid', 'geom', 'geometry', '_source_order', '_loaded_at')}
                        features.append({
                            'type': 'Feature',
                            'geometry': row['geometry'],
                            'properties': properties
                        })

                    return jsonify({
                        'type': 'FeatureCollection',
                        'features': features
                    })
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    # ==================== Chat API ====================

    @app.route('/api/chat/context')
    @login_required
    def get_chat_context():
        """Return database context for Claude's system prompt."""
        if not app.config['db_connection']:
            return jsonify({'error': 'Database not configured'}), 400

        try:
            import psycopg2

            conn = psycopg2.connect(app.config['db_connection'])
            cur = conn.cursor()
            schema_name = app.config['schema']

            # Get all tables in schema
            cur.execute("""
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = %s AND table_type = 'BASE TABLE'
            """, (schema_name,))
            tables = [row[0] for row in cur.fetchall()]

            schema_info = {}
            samples = {}

            for table in tables:
                full_name = f"{schema_name}.{table}"

                # Get column info
                cur.execute("""
                    SELECT column_name, data_type
                    FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = %s
                    ORDER BY ordinal_position
                """, (schema_name, table))
                columns = [{'name': row[0], 'type': row[1]} for row in cur.fetchall()]

                # Get geometry info if exists
                cur.execute("""
                    SELECT type, srid
                    FROM geometry_columns
                    WHERE f_table_schema = %s AND f_table_name = %s
                    LIMIT 1
                """, (schema_name, table))
                geom_row = cur.fetchone()

                # Get row count
                cur.execute(f'SELECT COUNT(*) FROM "{schema_name}"."{table}"')
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
                    cols_sql = ', '.join([f'"{c}"' for c in non_geom_cols[:10]])
                    cur.execute(f'SELECT {cols_sql} FROM "{schema_name}"."{table}" LIMIT 3')
                    sample_rows = []
                    for row in cur.fetchall():
                        sample_row = {}
                        for i, val in enumerate(row):
                            col_name = non_geom_cols[i] if i < len(non_geom_cols) else f'col_{i}'
                            if hasattr(val, 'isoformat'):
                                sample_row[col_name] = val.isoformat()
                            else:
                                sample_row[col_name] = val
                        sample_rows.append(sample_row)
                    samples[full_name] = sample_rows

            cur.close()
            conn.close()

            return jsonify({
                'schema': schema_info,
                'samples': samples,
                'metadata': {
                    'schema_name': schema_name,
                    'coordinate_system': 'SWEREF99 TM (EPSG:3006)',
                    'total_tables': len(tables)
                }
            })

        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/chat/query', methods=['POST'])
    @login_required
    def execute_chat_query():
        """Execute validated read-only SQL query."""
        import re
        import time

        if not app.config['db_connection']:
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
            import psycopg2

            conn = psycopg2.connect(app.config['db_connection'])
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

        except Exception as e:
            return jsonify({'error': f'Database error: {str(e)}'}), 400

    return app


def run_download(
    order_id: str,
    downloads_dir: Path,
    progress_queue: queue.Queue
):
    """Run download operation in background thread with differential updates."""
    import requests
    from datetime import datetime

    BASE_URL = "https://download-geotorget.lantmateriet.se/download"

    try:
        progress_queue.put(DownloadProgress(
            order_id=order_id,
            status='fetching_list',
            message='Fetching file list...'
        ))

        # Get file list
        url = f"{BASE_URL}/{order_id}/files"
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        files = response.json()

        if not files:
            progress_queue.put(DownloadProgress(
                order_id=order_id,
                status='error',
                error='No files found for this order'
            ))
            return

        # Create order directory
        order_dir = downloads_dir / order_id
        order_dir.mkdir(parents=True, exist_ok=True)

        # Load existing metadata to check for updates
        local_download_date = None
        existing_metadata = load_order_metadata_full(order_dir)
        if existing_metadata:
            local_download_date = existing_metadata.get('download_date')

        # Parse local download date for comparison
        # Convert to UTC timestamp for consistent comparison
        local_timestamp = None
        if local_download_date:
            try:
                # Handle both naive and aware datetimes
                local_dt = datetime.fromisoformat(local_download_date.replace("Z", "+00:00"))
                # Convert to timestamp (seconds since epoch) for comparison
                if local_dt.tzinfo is None:
                    # Naive datetime - assume local time, convert to timestamp
                    local_timestamp = local_dt.timestamp()
                else:
                    local_timestamp = local_dt.timestamp()
            except (ValueError, TypeError):
                pass

        # Determine which files need to be downloaded
        files_to_download = []
        files_skipped = []

        for file_info in files:
            file_title = file_info.get('title', '')
            file_path = order_dir / file_title
            file_updated = file_info.get('updated')

            needs_download = True

            if file_path.exists() and local_timestamp is not None and file_updated:
                # Check if file was updated after our last download
                try:
                    remote_dt = datetime.fromisoformat(file_updated.replace("Z", "+00:00"))
                    # Convert to timestamp for comparison
                    if remote_dt.tzinfo is None:
                        remote_timestamp = remote_dt.timestamp()
                    else:
                        remote_timestamp = remote_dt.timestamp()

                    if remote_timestamp <= local_timestamp:
                        # File hasn't changed since last download
                        needs_download = False
                        files_skipped.append(file_title)
                except (ValueError, TypeError):
                    pass
            elif file_path.exists() and not file_updated:
                # No update timestamp available, skip if file exists
                needs_download = False
                files_skipped.append(file_title)

            if needs_download:
                files_to_download.append(file_info)

        total_files = len(files)
        files_to_download_count = len(files_to_download)
        skipped_count = len(files_skipped)

        # Calculate size of files to download
        download_size = sum(f.get('length', f.get('size', 0)) for f in files_to_download)

        if files_to_download_count == 0:
            progress_queue.put(DownloadProgress(
                order_id=order_id,
                status='completed',
                files_done=total_files,
                files_total=total_files,
                message=f'All {total_files} files are up to date'
            ))
            return

        progress_queue.put(DownloadProgress(
            order_id=order_id,
            status='downloading',
            files_total=files_to_download_count,
            bytes_total=download_size,
            message=f'Downloading {files_to_download_count} files ({skipped_count} skipped, up to date)'
        ))

        # Download files that need updating
        bytes_downloaded = 0
        files_downloaded = 0

        for i, file_info in enumerate(files_to_download):
            file_title = file_info.get('title', f'file_{i}')
            file_href = file_info.get('href')
            file_size = file_info.get('length', file_info.get('size', 0))

            if not file_href:
                continue

            # Initial progress for this file
            file_bytes_downloaded = 0
            progress_queue.put(DownloadProgress(
                order_id=order_id,
                status='downloading',
                current_file=file_title,
                files_done=i,
                files_total=files_to_download_count,
                bytes_downloaded=0,
                bytes_total=file_size,
                message=f'Downloading {file_title}...'
            ))

            # Download file with progress updates
            file_path = order_dir / file_title
            last_update = 0
            update_interval = 65536  # Update every 64KB
            try:
                with requests.get(file_href, stream=True, timeout=300) as r:
                    r.raise_for_status()
                    # Try to get actual file size from headers
                    actual_size = int(r.headers.get('content-length', file_size) or file_size)
                    if actual_size > 0:
                        file_size = actual_size

                    with open(file_path, 'wb') as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            f.write(chunk)
                            file_bytes_downloaded += len(chunk)
                            bytes_downloaded += len(chunk)

                            # Send progress update periodically
                            if file_bytes_downloaded - last_update >= update_interval:
                                last_update = file_bytes_downloaded
                                progress_queue.put(DownloadProgress(
                                    order_id=order_id,
                                    status='downloading',
                                    current_file=file_title,
                                    files_done=i,
                                    files_total=files_to_download_count,
                                    bytes_downloaded=file_bytes_downloaded,
                                    bytes_total=file_size,
                                    message=f'Downloading {file_title}...'
                                ))

                files_downloaded += 1

            except Exception as e:
                progress_queue.put(DownloadProgress(
                    order_id=order_id,
                    status='error',
                    current_file=file_title,
                    files_done=i,
                    files_total=files_to_download_count,
                    error=f'Failed to download {file_title}: {str(e)}'
                ))
                return

        # Save/update order_metadata.json (used for update checking)
        order_metadata = {
            'order_id': order_id,
            'files': files,
            'download_date': datetime.now().isoformat(),
        }
        order_metadata_path = order_dir / 'order_metadata.json'
        with open(order_metadata_path, 'w') as f:
            json.dump(order_metadata, f, indent=2)

        # Also save/update metadata.json for compatibility
        total_size = sum(f.get('length', f.get('size', 0)) for f in files)
        metadata = {
            'order_id': order_id,
            'files': files,
            'download_date': datetime.now().isoformat(),
            'total_size': total_size
        }
        metadata_path = order_dir / 'metadata.json'
        with open(metadata_path, 'w') as f:
            json.dump(metadata, f, indent=2)

        msg = f'Downloaded {files_downloaded} files'
        if skipped_count > 0:
            msg += f' ({skipped_count} already up to date)'

        progress_queue.put(DownloadProgress(
            order_id=order_id,
            status='completed',
            files_done=files_to_download_count,
            files_total=files_to_download_count,
            bytes_downloaded=bytes_downloaded,
            bytes_total=download_size,
            message=msg
        ))

    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            progress_queue.put(DownloadProgress(
                order_id=order_id,
                status='error',
                error=f'Order not found: {order_id}'
            ))
        else:
            progress_queue.put(DownloadProgress(
                order_id=order_id,
                status='error',
                error=f'HTTP error: {str(e)}'
            ))
    except Exception as e:
        progress_queue.put(DownloadProgress(
            order_id=order_id,
            status='error',
            error=str(e)
        ))


def run_publish(
    order_id: str,
    downloads_dir: Path,
    db_connection: str,
    schema: str,
    layers: Optional[list[str]],
    progress_queue: queue.Queue
):
    """Run publish operation in background thread."""
    from ..tiling.processor import DataProcessor

    try:
        progress_queue.put(PublishProgress(
            order_id=order_id,
            status='starting',
            message='Initializing processor...'
        ))

        processor = DataProcessor(
            downloads_dir=downloads_dir,
            db_connection=db_connection,
            schema=schema
        )

        def on_progress(layer_name: str, current: int, total: int):
            progress_queue.put(PublishProgress(
                order_id=order_id,
                status='processing',
                current_layer=layer_name,
                layers_done=current - 1,
                layers_total=total,
                message=f'Processing {layer_name}...'
            ))

        result = processor.process_order(
            order_id=order_id,
            layers=layers,
            progress_callback=on_progress
        )

        processor.close()

        if result.success:
            progress_queue.put(PublishProgress(
                order_id=order_id,
                status='completed',
                layers_done=len(result.layers_processed),
                layers_total=len(result.layers_processed),
                message=f'Published {result.total_features} features from {len(result.layers_processed)} layers'
            ))
        else:
            # Collect detailed error information from layer results
            failed_layers = []
            succeeded_layers = []
            for layer_result in result.layers_processed:
                if layer_result.success:
                    succeeded_layers.append(layer_result.layer_name)
                else:
                    error_msg = layer_result.error or 'Unknown error'
                    failed_layers.append(f"{layer_result.layer_name}: {error_msg}")

            # Build detailed error message
            error_details = []
            if failed_layers:
                error_details.append(f"Failed layers ({len(failed_layers)}):")
                for fail in failed_layers:
                    error_details.append(f"  - {fail}")
            if succeeded_layers:
                error_details.append(f"Succeeded layers ({len(succeeded_layers)}): {', '.join(succeeded_layers)}")

            detailed_error = '\n'.join(error_details) if error_details else (result.error or 'Unknown error')

            progress_queue.put(PublishProgress(
                order_id=order_id,
                status='error',
                layers_done=len(succeeded_layers),
                layers_total=len(result.layers_processed),
                error=detailed_error
            ))

    except Exception as e:
        import traceback
        error_detail = f"{str(e)}\n{traceback.format_exc()}"
        progress_queue.put(PublishProgress(
            order_id=order_id,
            status='error',
            error=error_detail
        ))


def get_published_layers(db_connection: str, schema: str, order_id: str) -> list[str]:
    """Get list of published layers for an order."""
    try:
        import psycopg2

        with psycopg2.connect(db_connection) as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT layer_name FROM "{schema}"._metadata
                    WHERE order_id = %s
                """, (order_id,))
                return [row[0] for row in cur.fetchall()]
    except Exception:
        return []


def load_package_name(order_dir: Path) -> str:
    """Load the LM package name from order metadata."""
    metadata_path = order_dir / 'metadata.json'
    if metadata_path.exists():
        try:
            with open(metadata_path) as f:
                metadata = json.load(f)
                return metadata.get('package_name', '')
        except Exception:
            pass
    return ''


def save_package_name(order_dir: Path, package_name: str):
    """Save the LM package name to order metadata."""
    metadata_path = order_dir / 'metadata.json'

    # Load existing metadata or create new
    metadata = {}
    if metadata_path.exists():
        try:
            with open(metadata_path) as f:
                metadata = json.load(f)
        except Exception:
            pass

    # Update package name
    metadata['package_name'] = package_name

    # Save back
    with open(metadata_path, 'w') as f:
        json.dump(metadata, f, indent=2)


def load_order_metadata_full(order_dir: Path) -> dict | None:
    """Load full order metadata from order_metadata.json or metadata.json."""
    # Try order_metadata.json first (created by download_order.py)
    meta_path = order_dir / 'order_metadata.json'
    if meta_path.exists():
        try:
            with open(meta_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass

    # Fall back to metadata.json (created by dashboard downloads)
    meta_path = order_dir / 'metadata.json'
    if meta_path.exists():
        try:
            with open(meta_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass

    return None


def check_for_updates(order_id: str, order_dir: Path) -> dict:
    """
    Check if there are updates available for an order.

    Returns dict with:
        - has_update: bool
        - local_date: str (last download date)
        - remote_date: str (latest available date)
        - new_files: list of files with updates
    """
    import requests
    from datetime import datetime

    BASE_URL = "https://download-geotorget.lantmateriet.se/download"

    result = {
        "has_update": False,
        "local_date": None,
        "remote_date": None,
        "new_files": [],
    }

    # Get local metadata
    metadata = load_order_metadata_full(order_dir)
    if not metadata:
        result["has_update"] = True
        result["new_files"] = ["All files (no local download)"]
        return result

    local_date = metadata.get("download_date")
    result["local_date"] = local_date

    # Get remote file list
    try:
        url = f"{BASE_URL}/{order_id}/files"
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        remote_files = response.json()
    except Exception as e:
        raise ValueError(f"Failed to check updates: {e}")

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

    # Compare dates using timestamps to handle naive vs aware datetime
    if local_date and result["remote_date"]:
        try:
            local_dt = datetime.fromisoformat(local_date.replace("Z", "+00:00"))
            remote_dt = datetime.fromisoformat(result["remote_date"].replace("Z", "+00:00"))

            # Convert to timestamps for safe comparison
            local_ts = local_dt.timestamp()
            remote_ts = remote_dt.timestamp()

            if remote_ts > local_ts:
                result["has_update"] = True
                # Find which files are newer
                for f in remote_files:
                    updated = f.get("updated")
                    if updated:
                        file_dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                        file_ts = file_dt.timestamp()
                        if file_ts > local_ts:
                            result["new_files"].append(f.get("title", "unknown"))
        except (ValueError, TypeError):
            pass

    return result


def generate_login_html(error: str = None) -> str:
    """Generate login page HTML with DTCC styling."""
    error_html = ''
    if error:
        error_html = f'<div class="error-message">{error}</div>'

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Login - DTCC GeoDB</title>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Montserrat:wght@300;500;600;700&display=swap');
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
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
            --red: #f56565;
        }}
        body {{
            font-family: 'Montserrat', -apple-system, BlinkMacSystemFont, sans-serif;
            background: linear-gradient(180deg, var(--dark-bg) 0%, var(--dark-secondary) 100%);
            min-height: 100vh;
            color: var(--text-primary);
            display: flex;
            align-items: center;
            justify-content: center;
        }}
        .login-container {{
            width: 100%;
            max-width: 400px;
            padding: 2rem;
        }}
        .logo-section {{
            text-align: center;
            margin-bottom: 2rem;
        }}
        .logo-section img {{
            height: 48px;
            width: auto;
            margin-bottom: 1rem;
        }}
        .logo-text {{
            font-size: 0.8rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            color: var(--text-primary);
            margin-bottom: 0.5rem;
        }}
        .app-title {{
            color: var(--gold);
            font-size: 1.5rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.06em;
        }}
        .login-card {{
            background: var(--dark-card);
            border: 1px solid var(--border-subtle);
            border-radius: 8px;
            padding: 2rem;
            backdrop-filter: blur(12px);
        }}
        .form-group {{
            margin-bottom: 1.25rem;
        }}
        .form-group label {{
            display: block;
            font-size: 0.75rem;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.06em;
            font-weight: 600;
            margin-bottom: 0.5rem;
        }}
        .form-group input {{
            width: 100%;
            padding: 0.875rem 1rem;
            border: 1px solid var(--border-subtle);
            border-radius: 4px;
            font-family: inherit;
            font-size: 0.9rem;
            background: var(--dark-bg);
            color: var(--text-primary);
            transition: border-color 0.2s;
        }}
        .form-group input::placeholder {{
            color: var(--text-dim);
        }}
        .form-group input:focus {{
            outline: none;
            border-color: var(--gold);
        }}
        .login-btn {{
            width: 100%;
            padding: 0.875rem 1.5rem;
            background: var(--gold);
            color: var(--dark-bg);
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-family: inherit;
            font-size: 0.8rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            transition: all 0.2s;
            margin-top: 0.5rem;
        }}
        .login-btn:hover {{
            opacity: 0.9;
            transform: translateY(-1px);
        }}
        .error-message {{
            background: rgba(245, 101, 101, 0.1);
            border: 1px solid var(--red);
            color: var(--red);
            padding: 0.75rem 1rem;
            border-radius: 4px;
            font-size: 0.85rem;
            margin-bottom: 1.25rem;
            text-align: center;
        }}
    </style>
</head>
<body>
    <div class="login-container">
        <div class="logo-section">
            <img src="https://dtcc.chalmers.se/wp-content/uploads/2023/10/DTCC_white-e1697442498498.png" alt="DTCC Logo">
            <div class="logo-text">Digital Twin Cities Centre</div>
            <div class="app-title">GeoDB Dashboard</div>
        </div>
        <div class="login-card">
            {error_html}
            <form method="POST" action="/login">
                <div class="form-group">
                    <label for="username">Username</label>
                    <input type="text" id="username" name="username" placeholder="Enter username" required autofocus>
                </div>
                <div class="form-group">
                    <label for="password">Password</label>
                    <input type="password" id="password" name="password" placeholder="Enter password" required>
                </div>
                <button type="submit" class="login-btn">Sign In</button>
            </form>
        </div>
    </div>
</body>
</html>'''


def generate_dashboard_html(downloads_dir: Path) -> str:
    """Generate dashboard HTML with management API integration."""
    # Note: The JavaScript in this HTML uses safe DOM methods (createElement, textContent)
    # instead of innerHTML to prevent XSS vulnerabilities
    return '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Geotorget Management</title>
    <link href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css" rel="stylesheet" />
    <script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Montserrat:wght@300;500;600;700&display=swap');
        * { box-sizing: border-box; margin: 0; padding: 0; }
        :root {
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
            --green: #48bb78;
            --red: #f56565;
        }
        body {
            font-family: 'Montserrat', -apple-system, BlinkMacSystemFont, sans-serif;
            background: linear-gradient(180deg, var(--dark-bg) 0%, var(--dark-secondary) 100%);
            min-height: 100vh;
            color: var(--text-primary);
            line-height: 1.6;
            padding: 2rem;
            padding-top: calc(56px + 2rem);
        }

        /* DTCC Header */
        .dtcc-header {
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
        }
        .dtcc-header .logo {
            display: flex;
            align-items: center;
            gap: 0.75rem;
        }
        .dtcc-header .logo img {
            height: 28px;
            width: auto;
        }
        .dtcc-header .logo-text {
            font-size: 0.7rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            line-height: 1.2;
            color: var(--text-primary);
        }
        .dtcc-header nav {
            display: flex;
            gap: 2rem;
        }
        .dtcc-header nav a {
            color: var(--gold);
            text-decoration: none;
            font-size: 0.75rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            transition: opacity 0.2s ease;
        }
        .dtcc-header nav a:hover {
            opacity: 0.7;
        }
        .container { max-width: 1400px; margin: 0 auto; }
        h1 {
            color: var(--gold);
            font-size: 1.8rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            margin-bottom: 0.5rem;
        }
        .subtitle {
            color: var(--text-secondary);
            font-size: 0.9rem;
            font-weight: 300;
            margin-bottom: 2rem;
        }

        .status-bar {
            background: var(--dark-card);
            border: 1px solid var(--border-subtle);
            padding: 1rem 1.5rem;
            border-radius: 8px;
            margin-bottom: 1.5rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
            backdrop-filter: blur(12px);
        }
        .status-item {
            display: flex;
            align-items: center;
            gap: 10px;
            font-size: 0.85rem;
        }
        .status-dot {
            width: 10px;
            height: 10px;
            border-radius: 50%;
            background: var(--text-dim);
        }
        .status-dot.connected { background: var(--green); }
        .status-dot.disconnected { background: var(--red); }

        .db-config {
            background: var(--dark-card);
            border: 1px solid var(--border-subtle);
            padding: 1.5rem;
            border-radius: 8px;
            margin-bottom: 1.5rem;
            backdrop-filter: blur(12px);
        }
        .db-config-title {
            font-size: 0.75rem;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.06em;
            font-weight: 600;
            margin-bottom: 0.75rem;
        }
        .db-config input {
            width: 100%;
            padding: 0.75rem 1rem;
            border: 1px solid var(--border-subtle);
            border-radius: 4px;
            font-family: 'Monaco', 'Consolas', monospace;
            font-size: 0.85rem;
            margin-bottom: 1rem;
            background: var(--dark-bg);
            color: var(--text-primary);
        }
        .db-config input::placeholder {
            color: var(--text-dim);
        }
        .db-config input:focus {
            outline: none;
            border-color: var(--gold);
        }
        .db-config-actions {
            display: flex;
            gap: 0.75rem;
        }
        .db-config button {
            padding: 0.6rem 1.25rem;
            background: var(--gold);
            color: var(--dark-bg);
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-family: inherit;
            font-size: 0.75rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            transition: all 0.2s;
        }
        .db-config button:hover {
            opacity: 0.9;
            transform: translateY(-1px);
        }
        .db-config button.secondary {
            background: var(--dark-card);
            color: var(--text-primary);
            border: 1px solid var(--border-subtle);
        }
        .db-config button.secondary:hover {
            background: var(--gold-subtle);
            border-color: var(--gold);
            color: var(--gold);
        }

        .db-connected {
            background: var(--dark-card);
            border: 1px solid var(--border-subtle);
            padding: 1rem 1.25rem;
            border-radius: 8px;
            margin-bottom: 1.5rem;
        }
        .db-connected-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 0.75rem;
        }
        .db-connected-label {
            font-size: 0.75rem;
            color: var(--success);
            text-transform: uppercase;
            letter-spacing: 0.06em;
            font-weight: 600;
        }
        .db-edit-btn {
            padding: 0.25rem 0.75rem;
            background: transparent;
            color: var(--text-secondary);
            border: 1px solid var(--border-subtle);
            border-radius: 4px;
            cursor: pointer;
            font-size: 0.7rem;
            text-transform: uppercase;
            letter-spacing: 0.04em;
        }
        .db-edit-btn:hover {
            border-color: var(--gold);
            color: var(--gold);
        }
        .db-connection-display {
            display: flex;
            align-items: center;
            gap: 0.75rem;
            background: var(--dark-bg);
            padding: 0.5rem 0.75rem;
            border-radius: 4px;
            margin-bottom: 0.75rem;
        }
        .db-connection-display code {
            flex: 1;
            font-family: 'SF Mono', Monaco, monospace;
            font-size: 0.8rem;
            color: var(--text-primary);
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .copy-btn {
            padding: 0.35rem 0.75rem;
            background: var(--gold);
            color: var(--dark-bg);
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-size: 0.65rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.04em;
            white-space: nowrap;
        }
        .copy-btn:hover {
            opacity: 0.9;
        }
        .copy-btn.copied {
            background: var(--success);
        }
        .db-api-hint {
            font-size: 0.7rem;
            color: var(--text-dim);
            line-height: 1.5;
        }
        .db-api-hint code {
            background: var(--dark-bg);
            padding: 0.15rem 0.4rem;
            border-radius: 3px;
            font-family: 'SF Mono', Monaco, monospace;
            font-size: 0.7rem;
        }

        .section-header {
            font-size: 1rem;
            color: var(--gold);
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            margin-bottom: 1rem;
            padding-bottom: 0.5rem;
            border-bottom: 1px solid var(--border-subtle);
        }

        .orders-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(380px, 1fr));
            gap: 1.25rem;
        }
        .order-card {
            background: var(--dark-card);
            border: 1px solid var(--border-subtle);
            border-radius: 8px;
            padding: 1.25rem;
            backdrop-filter: blur(12px);
            transition: all 0.2s;
        }
        .order-card:hover {
            border-color: var(--gold-dim);
        }
        .order-header {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 1rem;
        }
        .order-id {
            font-weight: 600;
            font-size: 0.8rem;
            color: var(--text-primary);
            word-break: break-all;
            font-family: 'Monaco', 'Consolas', monospace;
        }
        .package-name-row {
            margin-top: 0.5rem;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }
        .package-name-label {
            font-size: 0.7rem;
            color: var(--text-dim);
            text-transform: uppercase;
            letter-spacing: 0.04em;
            white-space: nowrap;
        }
        .package-name-input {
            flex: 1;
            padding: 0.35rem 0.5rem;
            border: 1px solid var(--border-subtle);
            border-radius: 3px;
            font-family: 'Monaco', 'Consolas', monospace;
            font-size: 0.75rem;
            background: var(--dark-bg);
            color: var(--text-primary);
            min-width: 0;
        }
        .package-name-input::placeholder {
            color: var(--text-dim);
        }
        .package-name-input:focus {
            outline: none;
            border-color: var(--gold);
        }
        .package-name-input.saved {
            border-color: var(--green);
        }
        .data-type-badge {
            font-size: 0.65rem;
            padding: 0.25rem 0.5rem;
            border-radius: 4px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.04em;
        }
        .data-type-badge.vector {
            background: rgba(250, 218, 54, 0.2);
            color: var(--gold);
            border: 1px solid rgba(250, 218, 54, 0.3);
        }
        .data-type-badge.lidar {
            background: rgba(159, 122, 234, 0.2);
            color: #9f7aea;
            border: 1px solid rgba(159, 122, 234, 0.3);
        }
        .data-type-badge.raster {
            background: rgba(72, 187, 120, 0.2);
            color: var(--green);
            border: 1px solid rgba(72, 187, 120, 0.3);
        }
        .data-type-badge.unknown {
            background: rgba(255, 255, 255, 0.1);
            color: var(--text-secondary);
            border: 1px solid var(--border-subtle);
        }

        .order-meta {
            font-size: 0.8rem;
            color: var(--text-secondary);
            margin-bottom: 1rem;
        }
        .order-meta div {
            margin-bottom: 0.25rem;
        }
        .order-meta .label {
            color: var(--text-dim);
        }
        .order-meta .value {
            color: var(--text-primary);
        }

        .layers-list {
            font-size: 0.75rem;
            background: var(--dark-bg);
            padding: 0.75rem;
            border-radius: 4px;
            margin-bottom: 1rem;
            max-height: 100px;
            overflow-y: auto;
            border: 1px solid var(--border-subtle);
        }
        .layer-item {
            display: flex;
            align-items: center;
            gap: 6px;
            padding: 0.2rem 0;
            color: var(--text-secondary);
        }
        .layer-item.published {
            color: var(--green);
        }

        .order-actions {
            display: flex;
            gap: 0.75rem;
        }
        .btn {
            flex: 1;
            padding: 0.6rem 1rem;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-family: inherit;
            font-size: 0.75rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            transition: all 0.2s;
        }
        .btn-publish {
            background: var(--gold);
            color: var(--dark-bg);
        }
        .btn-publish:hover {
            opacity: 0.9;
            transform: translateY(-1px);
        }
        .btn-publish:disabled {
            background: var(--dark-card);
            color: var(--text-dim);
            cursor: not-allowed;
            border: 1px solid var(--border-subtle);
            transform: none;
        }
        .btn-published {
            background: var(--dark-card);
            color: var(--text-primary);
            border: 1px solid var(--border-subtle);
        }
        .btn-published:hover {
            background: var(--gold-subtle);
            border-color: var(--gold);
            color: var(--gold);
        }
        .btn-update {
            background: var(--dark-card);
            color: var(--text-primary);
            border: 1px solid var(--border-subtle);
        }
        .btn-update:hover {
            background: var(--gold-subtle);
            border-color: var(--gold);
            color: var(--gold);
        }
        .btn-update.has-update {
            background: rgba(72, 187, 120, 0.2);
            color: var(--green);
            border: 1px solid rgba(72, 187, 120, 0.4);
        }
        .btn-update.has-update:hover {
            background: rgba(72, 187, 120, 0.3);
        }
        .btn-update:disabled {
            background: var(--dark-card);
            color: var(--text-dim);
            cursor: not-allowed;
            border: 1px solid var(--border-subtle);
            transform: none;
        }
        .update-badge {
            display: inline-block;
            width: 8px;
            height: 8px;
            background: var(--green);
            border-radius: 50%;
            margin-left: 6px;
            animation: pulse 2s infinite;
        }
        @keyframes pulse {
            0% { opacity: 1; }
            50% { opacity: 0.5; }
            100% { opacity: 1; }
        }
        .update-info {
            font-size: 0.7rem;
            color: var(--text-dim);
            margin-top: 0.25rem;
        }
        .update-info.available {
            color: var(--green);
        }

        .progress-overlay {
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0, 0, 0, 0.8);
            display: none;
            justify-content: center;
            align-items: center;
            z-index: 1000;
            backdrop-filter: blur(4px);
        }
        .progress-overlay.active { display: flex; }
        .progress-modal {
            background: var(--dark-secondary);
            border: 1px solid var(--border-subtle);
            padding: 2rem;
            border-radius: 8px;
            min-width: 500px;
            text-align: center;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.5);
        }
        .progress-modal h3 {
            color: var(--gold);
            font-size: 1rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            margin-bottom: 1.5rem;
        }
        .progress-close-btn {
            position: absolute;
            top: 1rem;
            right: 1rem;
            width: 28px;
            height: 28px;
            background: var(--dark-card);
            border: 1px solid var(--border-subtle);
            border-radius: 4px;
            color: var(--text-secondary);
            cursor: pointer;
            font-size: 1.2rem;
            line-height: 1;
            display: none;
            align-items: center;
            justify-content: center;
            transition: all 0.2s;
        }
        .progress-close-btn:hover {
            background: var(--gold-subtle);
            border-color: var(--gold);
            color: var(--gold);
        }
        .progress-close-btn.visible {
            display: flex;
        }
        .progress-modal {
            position: relative;
        }
        .progress-section {
            margin-bottom: 1.25rem;
            text-align: left;
        }
        .progress-section:last-of-type {
            margin-bottom: 0.75rem;
        }
        .progress-label {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 0.5rem;
            font-size: 0.75rem;
        }
        .progress-label-text {
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.04em;
        }
        .progress-label-value {
            color: var(--text-primary);
            font-weight: 500;
            font-family: 'Monaco', 'Consolas', monospace;
        }
        .progress-bar {
            height: 6px;
            background: var(--dark-bg);
            border-radius: 3px;
            overflow: hidden;
            border: 1px solid var(--border-subtle);
        }
        .progress-bar.large {
            height: 10px;
            border-radius: 5px;
        }
        .progress-bar-fill {
            height: 100%;
            background: var(--gold);
            transition: width 0.2s;
        }
        .progress-bar-fill.secondary {
            background: var(--green);
        }
        .progress-message {
            color: var(--text-secondary);
            font-size: 0.85rem;
            margin-top: 1rem;
        }
        .progress-filename {
            color: var(--text-primary);
            font-size: 0.8rem;
            font-family: 'Monaco', 'Consolas', monospace;
            margin-top: 0.25rem;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            max-width: 100%;
        }

        .progress-log-toggle {
            margin-top: 1rem;
            padding: 0.5rem 1rem;
            background: var(--dark-card);
            color: var(--text-secondary);
            border: 1px solid var(--border-subtle);
            border-radius: 4px;
            cursor: pointer;
            font-family: inherit;
            font-size: 0.7rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            transition: all 0.2s;
        }
        .progress-log-toggle:hover {
            background: var(--gold-subtle);
            border-color: var(--gold);
            color: var(--gold);
        }
        .progress-log-toggle.active {
            background: var(--gold-subtle);
            border-color: var(--gold);
            color: var(--gold);
        }
        .progress-log {
            display: none;
            margin-top: 1rem;
            background: var(--dark-bg);
            border: 1px solid var(--border-subtle);
            border-radius: 4px;
            max-height: 200px;
            overflow-y: auto;
            text-align: left;
        }
        .progress-log.visible {
            display: block;
        }
        .progress-log-entry {
            padding: 0.4rem 0.75rem;
            font-family: 'Monaco', 'Consolas', monospace;
            font-size: 0.7rem;
            border-bottom: 1px solid var(--border-subtle);
        }
        .progress-log-entry:last-child {
            border-bottom: none;
        }
        .progress-log-entry.info {
            color: var(--text-secondary);
        }
        .progress-log-entry.success {
            color: var(--green);
        }
        .progress-log-entry.error {
            color: var(--red);
        }
        .progress-log-entry .timestamp {
            color: var(--text-dim);
            margin-right: 0.5rem;
        }

        .empty-state {
            text-align: center;
            padding: 4rem 2rem;
            color: var(--text-secondary);
            grid-column: 1 / -1;
        }

        /* Custom Scrollbar */
        ::-webkit-scrollbar {
            width: 8px;
            height: 8px;
        }
        ::-webkit-scrollbar-track {
            background: var(--dark-bg);
        }
        ::-webkit-scrollbar-thumb {
            background: var(--gold-dim);
            border-radius: 4px;
        }
        ::-webkit-scrollbar-thumb:hover {
            background: var(--gold);
        }

        .footer {
            margin-top: 2rem;
            text-align: center;
            color: var(--text-dim);
            font-size: 0.8rem;
            font-weight: 300;
        }
        .footer a {
            color: var(--gold);
            text-decoration: none;
        }
        .footer a:hover {
            text-decoration: underline;
        }

        .download-section {
            background: var(--dark-card);
            border: 1px solid var(--border-subtle);
            padding: 1.5rem;
            border-radius: 8px;
            margin-bottom: 1.5rem;
            backdrop-filter: blur(12px);
        }
        .download-section-title {
            font-size: 0.75rem;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.06em;
            font-weight: 600;
            margin-bottom: 0.75rem;
        }
        .download-form {
            display: flex;
            gap: 0.75rem;
        }
        .download-form input {
            flex: 1;
            padding: 0.75rem 1rem;
            border: 1px solid var(--border-subtle);
            border-radius: 4px;
            font-family: 'Monaco', 'Consolas', monospace;
            font-size: 0.85rem;
            background: var(--dark-bg);
            color: var(--text-primary);
        }
        .download-form input::placeholder {
            color: var(--text-dim);
        }
        .download-form input:focus {
            outline: none;
            border-color: var(--gold);
        }
        .download-form button {
            padding: 0.75rem 1.5rem;
            background: var(--gold);
            color: var(--dark-bg);
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-family: inherit;
            font-size: 0.75rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            transition: all 0.2s;
            white-space: nowrap;
        }
        .download-form button:hover {
            opacity: 0.9;
            transform: translateY(-1px);
        }
        .download-form button:disabled {
            background: var(--dark-card);
            color: var(--text-dim);
            cursor: not-allowed;
            border: 1px solid var(--border-subtle);
            transform: none;
        }

        /* Split View Layout */
        .split-view {
            display: flex;
            gap: 1.5rem;
            margin-top: 1rem;
        }
        .split-left {
            flex: 0 0 40%;
            min-width: 0;
        }
        .split-right {
            flex: 0 0 60%;
            min-width: 0;
            position: sticky;
            top: calc(56px + 2rem);
            height: calc(100vh - 56px - 4rem);
        }

        /* Map Container */
        #maplibre-map {
            width: 100%;
            height: 100%;
            border-radius: 8px;
            border: 1px solid var(--border-subtle);
            background: var(--dark-bg);
        }

        /* Layer Toggles */
        .layer-toggle {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            padding: 0.4rem 0.5rem;
            margin: 0.25rem 0;
            background: var(--dark-bg);
            border-radius: 3px;
            cursor: pointer;
            transition: all 0.2s;
        }
        .layer-toggle:hover {
            background: var(--gold-subtle);
        }
        .layer-toggle input[type="checkbox"] {
            cursor: pointer;
        }
        .layer-toggle-label {
            flex: 1;
            font-size: 0.7rem;
            color: var(--text-secondary);
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }
        .layer-color-swatch {
            width: 14px;
            height: 14px;
            border-radius: 2px;
            border: 1px solid var(--border-subtle);
        }
        .layer-loading {
            font-size: 0.65rem;
            color: var(--text-dim);
            font-style: italic;
        }

        /* MapLibre Popup Styling */
        .maplibregl-popup-content {
            background: var(--dark-secondary);
            color: var(--text-primary);
            border: 1px solid var(--border-subtle);
            border-radius: 4px;
            padding: 0.75rem;
            font-family: 'Montserrat', sans-serif;
            font-size: 0.75rem;
            max-width: 300px;
        }
        .maplibregl-popup-close-button {
            color: var(--text-secondary);
            font-size: 1.2rem;
            padding: 0.25rem 0.5rem;
        }
        .maplibregl-popup-close-button:hover {
            background: var(--gold-subtle);
            color: var(--gold);
        }
        .maplibregl-popup-anchor-top .maplibregl-popup-tip,
        .maplibregl-popup-anchor-bottom .maplibregl-popup-tip,
        .maplibregl-popup-anchor-left .maplibregl-popup-tip,
        .maplibregl-popup-anchor-right .maplibregl-popup-tip {
            border-color: var(--dark-secondary);
        }
        .popup-property {
            margin-bottom: 0.35rem;
        }
        .popup-property:last-child {
            margin-bottom: 0;
        }
        .popup-property-key {
            color: var(--text-dim);
            font-weight: 600;
            text-transform: uppercase;
            font-size: 0.65rem;
            letter-spacing: 0.04em;
        }
        .popup-property-value {
            color: var(--text-primary);
            font-family: 'Monaco', 'Consolas', monospace;
            word-break: break-word;
        }

        /* Responsive: stack on narrow screens */
        @media (max-width: 1024px) {
            .split-view {
                flex-direction: column;
            }
            .split-left, .split-right {
                flex: 1;
                width: 100%;
            }
            .split-right {
                position: relative;
                top: 0;
                height: 500px;
            }
        }

        /* Chat Widget Styles */
        .chat-toggle {
            position: fixed;
            bottom: 24px;
            right: 24px;
            width: 56px;
            height: 56px;
            border-radius: 50%;
            background: linear-gradient(135deg, var(--gold), #c9a82e);
            border: none;
            cursor: pointer;
            box-shadow: 0 4px 20px rgba(250, 218, 54, 0.4);
            z-index: 1000;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: transform 0.2s, box-shadow 0.2s;
        }
        .chat-toggle:hover {
            transform: scale(1.05);
            box-shadow: 0 6px 24px rgba(250, 218, 54, 0.5);
        }
        .chat-toggle svg {
            width: 28px;
            height: 28px;
            fill: var(--dark-bg);
        }
        .chat-window {
            position: fixed;
            bottom: 90px;
            right: 24px;
            width: 400px;
            height: 500px;
            background: var(--dark-bg);
            border: 1px solid var(--border-subtle);
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
            background: var(--dark-secondary);
            border-bottom: 1px solid var(--border-subtle);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .chat-header h3 {
            margin: 0;
            color: var(--gold);
            font-size: 16px;
            font-weight: 600;
        }
        .chat-close {
            background: none;
            border: none;
            color: var(--text-secondary);
            font-size: 24px;
            cursor: pointer;
            padding: 0;
            line-height: 1;
        }
        .chat-close:hover { color: var(--text-primary); }
        .chat-api-key {
            padding: 12px 16px;
            background: var(--dark-secondary);
            border-bottom: 1px solid var(--border-subtle);
        }
        .chat-api-key input {
            width: 100%;
            padding: 8px 12px;
            background: var(--dark-bg);
            border: 1px solid var(--border-subtle);
            border-radius: 6px;
            color: var(--text-primary);
            font-size: 13px;
            font-family: inherit;
        }
        .chat-api-key input::placeholder { color: var(--text-dim); }
        .chat-api-key input:focus {
            outline: none;
            border-color: var(--gold);
        }
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
            line-height: 1.5;
        }
        .chat-message.user {
            align-self: flex-end;
            background: linear-gradient(135deg, var(--gold), #c9a82e);
            color: var(--dark-bg);
        }
        .chat-message.assistant {
            align-self: flex-start;
            background: var(--dark-secondary);
            color: var(--text-primary);
        }
        .chat-message.error {
            align-self: flex-start;
            background: rgba(245, 101, 101, 0.2);
            color: var(--red);
            border: 1px solid rgba(245, 101, 101, 0.3);
        }
        .chat-message pre {
            background: var(--dark-bg);
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
            border: 1px solid var(--border-subtle);
            text-align: left;
        }
        .chat-message th { background: var(--dark-bg); }
        .chat-input-area {
            padding: 12px 16px;
            background: var(--dark-secondary);
            border-top: 1px solid var(--border-subtle);
            display: flex;
            gap: 8px;
        }
        .chat-input-area input {
            flex: 1;
            padding: 10px 14px;
            background: var(--dark-bg);
            border: 1px solid var(--border-subtle);
            border-radius: 20px;
            color: var(--text-primary);
            font-size: 14px;
            font-family: inherit;
        }
        .chat-input-area input:focus {
            outline: none;
            border-color: var(--gold);
        }
        .chat-input-area button {
            padding: 10px 16px;
            background: linear-gradient(135deg, var(--gold), #c9a82e);
            border: none;
            border-radius: 20px;
            color: var(--dark-bg);
            font-weight: 600;
            cursor: pointer;
            font-family: inherit;
            font-size: 13px;
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
            align-self: flex-start;
            background: var(--dark-secondary);
            border-radius: 12px;
        }
        .chat-typing span {
            width: 8px;
            height: 8px;
            background: var(--text-dim);
            border-radius: 50%;
            animation: typing 1.4s infinite;
        }
        .chat-typing span:nth-child(2) { animation-delay: 0.2s; }
        .chat-typing span:nth-child(3) { animation-delay: 0.4s; }
        @keyframes typing {
            0%, 60%, 100% { transform: translateY(0); }
            30% { transform: translateY(-4px); }
        }

        /* Train Viewer Styles */
        .train-controls {
            margin-top: 1rem;
            padding-top: 1rem;
            border-top: 1px solid var(--border-subtle);
        }
        .train-toggle-row {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            margin-bottom: 0.5rem;
        }
        .train-toggle-row input[type="checkbox"] {
            accent-color: var(--gold);
        }
        .train-toggle-row label {
            font-size: 0.85rem;
            color: var(--text-primary);
        }
        .train-refresh-btn {
            display: none;
            padding: 0.4rem 0.8rem;
            background: var(--dark-card);
            color: var(--text-primary);
            border: 1px solid var(--border-subtle);
            border-radius: 4px;
            cursor: pointer;
            font-size: 0.75rem;
            margin-top: 0.5rem;
        }
        .train-refresh-btn:hover {
            border-color: var(--gold);
            color: var(--gold);
        }
        .train-refresh-btn.visible {
            display: inline-block;
        }
        .train-refresh-btn:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }
        .train-status {
            font-size: 0.7rem;
            color: var(--text-dim);
            margin-top: 0.25rem;
        }
        .train-popup {
            font-family: 'Montserrat', sans-serif;
            padding: 0;
        }
        .train-popup h4 {
            margin: 0 0 8px 0;
            font-size: 14px;
            color: var(--gold);
            border-bottom: 1px solid var(--border-subtle);
            padding-bottom: 6px;
        }
        .train-popup p {
            margin: 4px 0;
            font-size: 12px;
            color: var(--text-primary);
        }
        .train-popup .delay-ok { color: #48bb78; }
        .train-popup .delay-warn { color: #ed8936; }
        .train-popup .delay-bad { color: #f56565; }
        .maplibregl-popup-content {
            background: var(--dark-bg);
            border: 1px solid var(--border-subtle);
            border-radius: 8px;
            padding: 12px;
        }
        .maplibregl-popup-tip {
            border-top-color: var(--dark-bg);
        }
        .maplibregl-popup-close-button {
            color: var(--text-secondary);
            font-size: 18px;
        }
        .maplibregl-popup-close-button:hover {
            color: var(--text-primary);
            background: transparent;
        }

        /* Weather Viewer Styles */
        .weather-toggle-row {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            margin-bottom: 0.5rem;
        }
        .weather-toggle-row input[type="checkbox"] {
            accent-color: var(--gold);
        }
        .weather-toggle-row label {
            font-size: 0.85rem;
            color: var(--text-primary);
        }
        .weather-refresh-btn {
            display: none;
            padding: 0.4rem 0.8rem;
            background: var(--dark-card);
            color: var(--text-primary);
            border: 1px solid var(--border-subtle);
            border-radius: 4px;
            cursor: pointer;
            font-size: 0.75rem;
            margin-top: 0.5rem;
            margin-left: 0.5rem;
        }
        .weather-refresh-btn:hover {
            border-color: var(--gold);
            color: var(--gold);
        }
        .weather-refresh-btn.visible {
            display: inline-block;
        }
        .weather-refresh-btn:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }
        .weather-status {
            font-size: 0.7rem;
            color: var(--text-dim);
            margin-top: 0.25rem;
        }
        .weather-popup {
            font-family: 'Montserrat', sans-serif;
            min-width: 200px;
        }
        .weather-popup h4 {
            margin: 0 0 8px 0;
            font-size: 14px;
            color: var(--gold);
            border-bottom: 1px solid var(--border-subtle);
            padding-bottom: 6px;
        }
        .weather-popup .weather-grid {
            display: grid;
            grid-template-columns: auto auto;
            gap: 4px 12px;
            font-size: 12px;
        }
        .weather-popup .weather-label {
            color: var(--text-secondary);
        }
        .weather-popup .weather-value {
            color: var(--text-primary);
            font-weight: 500;
        }
        .weather-popup .temp-cold { color: #63b3ed; }
        .weather-popup .temp-cool { color: #68d391; }
        .weather-popup .temp-warm { color: #f6ad55; }
        .weather-popup .temp-hot { color: #fc8181; }

        /* LiDAR Tiles */
        .lidar-toggle-row {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            margin-bottom: 0.5rem;
        }
        .lidar-toggle-row input[type="checkbox"] {
            accent-color: var(--gold);
        }
        .lidar-toggle-row label {
            font-size: 0.85rem;
            color: var(--text-primary);
        }
        .lidar-status {
            font-size: 0.7rem;
            color: var(--text-dim);
            margin-top: 0.25rem;
        }
        .lidar-popup {
            font-family: 'Montserrat', sans-serif;
            min-width: 200px;
        }
        .lidar-popup h4 {
            margin: 0 0 8px 0;
            font-size: 14px;
            color: #E91E63;
            border-bottom: 1px solid var(--border-subtle);
            padding-bottom: 6px;
        }
        .lidar-popup .lidar-grid {
            display: grid;
            grid-template-columns: auto auto;
            gap: 4px 12px;
            font-size: 12px;
        }
        .lidar-popup .lidar-label {
            color: var(--text-secondary);
        }
        .lidar-popup .lidar-value {
            color: var(--text-primary);
            font-weight: 500;
        }
        .lidar-popup .download-btn {
            display: block;
            margin-top: 10px;
            padding: 8px 16px;
            background: #E91E63;
            color: white;
            text-align: center;
            border-radius: 4px;
            text-decoration: none;
            font-size: 12px;
            font-weight: 500;
        }
        .lidar-popup .download-btn:hover {
            background: #C2185B;
        }
        .lidar-progress {
            margin-top: 10px;
        }
        .lidar-progress-bar {
            width: 100%;
            height: 8px;
            background: var(--dark-card);
            border-radius: 4px;
            overflow: hidden;
        }
        .lidar-progress-fill {
            height: 100%;
            background: linear-gradient(90deg, #E91E63, #FF5722);
            border-radius: 4px;
            transition: width 0.2s ease;
        }
        .lidar-progress-text {
            font-size: 11px;
            color: var(--text-secondary);
            margin-top: 4px;
            text-align: center;
        }
    </style>
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
            <a href="/logout" style="margin-left: 1rem; border-left: 1px solid rgba(255,255,255,0.2); padding-left: 2rem;">Logout</a>
        </nav>
    </header>

    <div class="container">
        <h1>Geotorget Management</h1>
        <p class="subtitle">PostGIS data publishing and management dashboard</p>

        <div class="status-bar">
            <div class="status-item">
                <span class="status-dot" id="dbStatus"></span>
                <span id="dbStatusText">Checking database...</span>
            </div>
            <div class="status-item">
                <span class="status-dot" id="martinStatus"></span>
                <span id="martinStatusText">Tiles: checking...</span>
            </div>
            <div class="status-item">
                <span id="statsText">-</span>
            </div>
        </div>

        <div class="db-config" id="dbConfig">
            <div class="db-config-title">Database Connection</div>
            <input type="text" id="dbConnection" placeholder="postgresql://user:pass@localhost/geotorget">
            <div class="db-config-actions">
                <button onclick="saveDbConfig()">Connect</button>
                <button class="secondary" onclick="initDb()">Initialize DB</button>
            </div>
        </div>

        <div class="db-connected" id="dbConnected" style="display: none;">
            <div class="db-connected-header">
                <span class="db-connected-label">Connected to database</span>
                <button class="db-edit-btn" onclick="editDbConfig()">Edit</button>
            </div>
            <div class="db-connection-display">
                <code id="dbConnectionDisplay">-</code>
                <button class="copy-btn" onclick="copyConnectionString()" title="Copy connection string">Copy</button>
            </div>
            <div class="db-api-hint">
                Use this connection string with the API server:<br>
                <code>python serve_api.py --db "&lt;connection string&gt;"</code>
            </div>
        </div>

        <div class="download-section">
            <div class="download-section-title">Download New Order</div>
            <div class="download-form">
                <input type="text" id="orderIdInput" placeholder="Enter Order ID (e.g., a1b2c3d4-e5f6-7890-abcd-ef1234567890)">
                <button id="downloadBtn" onclick="startDownload()">Download</button>
            </div>
        </div>

        <h2 class="section-header">Downloaded Orders</h2>
        <div class="split-view">
            <div class="split-left">
                <div class="orders-grid" id="ordersGrid">
                    <div class="empty-state">Loading orders...</div>
                </div>
                <div class="train-controls">
                    <div class="train-toggle-row">
                        <input type="checkbox" id="trainToggle">
                        <label for="trainToggle">Live Trains</label>
                    </div>
                    <button class="train-refresh-btn" id="trainRefreshBtn">↻ Refresh Trains</button>
                    <div class="train-status" id="trainStatus"></div>
                    <div class="weather-toggle-row" style="margin-top: 0.75rem;">
                        <input type="checkbox" id="weatherToggle">
                        <label for="weatherToggle">Weather Stations</label>
                    </div>
                    <button class="weather-refresh-btn" id="weatherRefreshBtn">↻ Refresh Weather</button>
                    <div class="weather-status" id="weatherStatus"></div>
                    <div class="lidar-toggle-row" style="margin-top: 0.75rem;">
                        <input type="checkbox" id="lidarToggle">
                        <label for="lidarToggle">LiDAR Tiles</label>
                    </div>
                    <div class="lidar-status" id="lidarStatus"></div>
                </div>
            </div>
            <div class="split-right">
                <div id="maplibre-map"></div>
            </div>
        </div>

        <div class="footer">
            Geotorget Management Server
        </div>
    </div>

    <div class="progress-overlay" id="progressOverlay">
        <div class="progress-modal">
            <button class="progress-close-btn" id="progressCloseBtn" onclick="closeProgressModal()">&times;</button>
            <h3 id="progressTitle">Publishing...</h3>

            <div class="progress-section" id="progressOverallSection">
                <div class="progress-label">
                    <span class="progress-label-text">Overall Progress</span>
                    <span class="progress-label-value" id="progressOverallValue">0 / 0 files</span>
                </div>
                <div class="progress-bar large">
                    <div class="progress-bar-fill" id="progressBar" style="width: 0%"></div>
                </div>
            </div>

            <div class="progress-section" id="progressFileSection" style="display: none;">
                <div class="progress-label">
                    <span class="progress-label-text">Current File</span>
                    <span class="progress-label-value" id="progressFileValue">0 MB / 0 MB</span>
                </div>
                <div class="progress-bar">
                    <div class="progress-bar-fill secondary" id="progressFileBar" style="width: 0%"></div>
                </div>
                <div class="progress-filename" id="progressFileName">-</div>
            </div>

            <div class="progress-message" id="progressMessage">Initializing...</div>

            <button class="progress-log-toggle" id="progressLogToggle" onclick="toggleProgressLog()">Show Log</button>
            <div class="progress-log" id="progressLog"></div>
        </div>
    </div>

    <script>
        var dbConnected = false;
        var currentDbConnection = null;

        function formatBytes(bytes) {
            if (bytes === 0) return '0 B';
            var k = 1024;
            var sizes = ['B', 'KB', 'MB', 'GB'];
            var i = Math.floor(Math.log(bytes) / Math.log(k));
            return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
        }

        function savePackageName(orderId, packageName, inputElement) {
            fetch('/api/orders/' + orderId + '/package-name', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({package_name: packageName})
            })
            .then(function(resp) { return resp.json(); })
            .then(function(data) {
                if (data.status === 'ok' && inputElement) {
                    inputElement.classList.add('saved');
                    setTimeout(function() {
                        inputElement.classList.remove('saved');
                    }, 1500);
                }
            })
            .catch(function(e) {
                console.error('Failed to save package name:', e);
            });
        }

        function resetProgressModal() {
            document.getElementById('progressBar').style.width = '0%';
            document.getElementById('progressFileBar').style.width = '0%';
            document.getElementById('progressOverallValue').textContent = '0 / 0 files';
            document.getElementById('progressFileValue').textContent = '0 MB / 0 MB';
            document.getElementById('progressFileName').textContent = '-';
            document.getElementById('progressFileSection').style.display = 'none';
            document.getElementById('progressMessage').textContent = 'Initializing...';
            // Clear log
            document.getElementById('progressLog').innerHTML = '';
            document.getElementById('progressLog').classList.remove('visible');
            document.getElementById('progressLogToggle').classList.remove('active');
            document.getElementById('progressLogToggle').textContent = 'Show Log';
            // Hide close button
            document.getElementById('progressCloseBtn').classList.remove('visible');
        }

        function closeProgressModal() {
            document.getElementById('progressOverlay').classList.remove('active');
            loadOrders();
            checkDbStatus();
        }

        function showCloseButton() {
            document.getElementById('progressCloseBtn').classList.add('visible');
        }

        function toggleProgressLog() {
            var log = document.getElementById('progressLog');
            var toggle = document.getElementById('progressLogToggle');
            if (log.classList.contains('visible')) {
                log.classList.remove('visible');
                toggle.classList.remove('active');
                toggle.textContent = 'Show Log';
            } else {
                log.classList.add('visible');
                toggle.classList.add('active');
                toggle.textContent = 'Hide Log';
                // Auto-scroll to bottom
                log.scrollTop = log.scrollHeight;
            }
        }

        function addLogEntry(message, type) {
            var log = document.getElementById('progressLog');
            var entry = document.createElement('div');
            entry.className = 'progress-log-entry ' + (type || 'info');

            var timestamp = document.createElement('span');
            timestamp.className = 'timestamp';
            var now = new Date();
            timestamp.textContent = now.toLocaleTimeString();

            var text = document.createTextNode(message);

            entry.appendChild(timestamp);
            entry.appendChild(text);
            log.appendChild(entry);

            // Auto-scroll if log is visible
            if (log.classList.contains('visible')) {
                log.scrollTop = log.scrollHeight;
            }
        }

        function checkDbStatus() {
            fetch('/api/db/status')
                .then(function(resp) { return resp.json(); })
                .then(function(data) {
                    var dot = document.getElementById('dbStatus');
                    var text = document.getElementById('dbStatusText');
                    var stats = document.getElementById('statsText');
                    var config = document.getElementById('dbConfig');
                    var connected = document.getElementById('dbConnected');

                    if (data.connected) {
                        dbConnected = true;
                        dot.className = 'status-dot connected';
                        text.textContent = 'Database connected';
                        stats.textContent = data.table_count + ' tables, ' + data.total_features.toLocaleString() + ' features';
                        config.style.display = 'none';
                        connected.style.display = 'block';
                        // Fetch the connection string
                        fetch('/api/config')
                            .then(function(r) { return r.json(); })
                            .then(function(cfg) {
                                currentDbConnection = cfg.db_connection;
                                document.getElementById('dbConnectionDisplay').textContent = cfg.db_display || cfg.db_connection;
                            });
                    } else if (data.configured) {
                        dot.className = 'status-dot disconnected';
                        text.textContent = 'Connection failed: ' + (data.error || 'Unknown error');
                        config.style.display = 'block';
                        connected.style.display = 'none';
                    } else {
                        dot.className = 'status-dot';
                        text.textContent = 'Database not configured';
                        config.style.display = 'block';
                        connected.style.display = 'none';
                    }
                })
                .catch(function(e) {
                    console.error('Failed to check DB status:', e);
                });
        }

        function saveDbConfig() {
            var conn = document.getElementById('dbConnection').value;
            if (!conn) {
                alert('Please enter a connection string');
                return;
            }

            fetch('/api/config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({db_connection: conn})
            })
            .then(function() {
                checkDbStatus();
                loadOrders();
                // Try to start Martin after db connection is configured
                startMartin();
            })
            .catch(function(e) {
                alert('Failed to save config: ' + e.message);
            });
        }

        function startMartin() {
            fetch('/api/martin/start', { method: 'POST' })
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    if (data.error) {
                        console.log('Martin start: ' + data.error);
                    } else {
                        console.log('Martin started successfully');
                    }
                    checkMartinStatus();
                })
                .catch(function(e) {
                    console.error('Failed to start Martin:', e);
                    checkMartinStatus();
                });
        }

        function checkMartinStatus() {
            fetch('/api/martin/status')
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    var dot = document.getElementById('martinStatus');
                    var text = document.getElementById('martinStatusText');

                    if (!data.installed) {
                        dot.className = 'status-dot';
                        text.textContent = 'Tiles: not installed';
                    } else if (data.running) {
                        dot.className = 'status-dot connected';
                        text.textContent = 'Tiles: Martin running';
                    } else {
                        dot.className = 'status-dot disconnected';
                        text.textContent = 'Tiles: Martin stopped';
                    }
                })
                .catch(function(e) {
                    console.error('Failed to check Martin status:', e);
                });
        }

        function initDb() {
            var conn = document.getElementById('dbConnection').value;
            if (!conn) {
                alert('Please enter a connection string first');
                return;
            }

            fetch('/api/config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({db_connection: conn})
            })
            .then(function() {
                return fetch('/api/db/init', {method: 'POST'});
            })
            .then(function(resp) { return resp.json(); })
            .then(function(data) {
                if (data.error) {
                    alert('Error: ' + data.error);
                } else {
                    alert(data.message);
                    checkDbStatus();
                }
            })
            .catch(function(e) {
                alert('Failed to initialize DB: ' + e.message);
            });
        }

        function copyConnectionString() {
            if (!currentDbConnection) {
                alert('No connection string available');
                return;
            }
            navigator.clipboard.writeText(currentDbConnection).then(function() {
                var btn = document.querySelector('.copy-btn');
                var originalText = btn.textContent;
                btn.textContent = 'Copied!';
                btn.classList.add('copied');
                setTimeout(function() {
                    btn.textContent = originalText;
                    btn.classList.remove('copied');
                }, 1500);
            }).catch(function() {
                // Fallback for older browsers
                var textarea = document.createElement('textarea');
                textarea.value = currentDbConnection;
                document.body.appendChild(textarea);
                textarea.select();
                document.execCommand('copy');
                document.body.removeChild(textarea);
                alert('Connection string copied to clipboard');
            });
        }

        function editDbConfig() {
            document.getElementById('dbConnected').style.display = 'none';
            document.getElementById('dbConfig').style.display = 'block';
            document.getElementById('dbConnection').value = currentDbConnection || '';
        }

        function loadOrders() {
            fetch('/api/orders')
                .then(function(resp) { return resp.json(); })
                .then(function(orders) {
                    var grid = document.getElementById('ordersGrid');

                    if (orders.length === 0) {
                        var emptyDiv = document.createElement('div');
                        emptyDiv.className = 'empty-state';
                        emptyDiv.textContent = 'No orders found. Download some data first!';
                        grid.replaceChildren(emptyDiv);
                        return;
                    }

                    var fragment = document.createDocumentFragment();

                    orders.forEach(function(order) {
                        var card = document.createElement('div');
                        card.className = 'order-card';

                        var typeClass = 'unknown';
                        if (order.data_type && order.data_type.indexOf('VECTOR') !== -1) {
                            typeClass = 'vector';
                        } else if (order.data_type && order.data_type.indexOf('LIDAR') !== -1) {
                            typeClass = 'lidar';
                        } else if (order.data_type && order.data_type.indexOf('RASTER') !== -1) {
                            typeClass = 'raster';
                        }

                        var header = document.createElement('div');
                        header.className = 'order-header';

                        var idSpan = document.createElement('span');
                        idSpan.className = 'order-id';
                        idSpan.textContent = order.order_id;

                        var badge = document.createElement('span');
                        badge.className = 'data-type-badge ' + typeClass;
                        badge.textContent = order.data_type_label || 'Unknown';

                        header.appendChild(idSpan);
                        header.appendChild(badge);
                        card.appendChild(header);

                        // Package name input row
                        var packageRow = document.createElement('div');
                        packageRow.className = 'package-name-row';

                        var packageLabel = document.createElement('span');
                        packageLabel.className = 'package-name-label';
                        packageLabel.textContent = 'LM Package:';

                        var packageInput = document.createElement('input');
                        packageInput.type = 'text';
                        packageInput.className = 'package-name-input';
                        packageInput.placeholder = 'Enter package name...';
                        packageInput.value = order.package_name || '';
                        packageInput.dataset.orderId = order.order_id;

                        (function(input, orderId) {
                            var saveTimeout = null;
                            input.addEventListener('input', function() {
                                input.classList.remove('saved');
                                if (saveTimeout) clearTimeout(saveTimeout);
                                saveTimeout = setTimeout(function() {
                                    savePackageName(orderId, input.value, input);
                                }, 500);
                            });
                            input.addEventListener('blur', function() {
                                if (saveTimeout) clearTimeout(saveTimeout);
                                savePackageName(orderId, input.value, input);
                            });
                        })(packageInput, order.order_id);

                        packageRow.appendChild(packageLabel);
                        packageRow.appendChild(packageInput);
                        card.appendChild(packageRow);

                        var meta = document.createElement('div');
                        meta.className = 'order-meta';

                        var sizeDiv = document.createElement('div');
                        sizeDiv.textContent = 'Size: ' + (order.total_size_mb || 0) + ' MB';
                        meta.appendChild(sizeDiv);

                        var layerCountDiv = document.createElement('div');
                        layerCountDiv.textContent = 'Layers: ' + (order.layers ? order.layers.length : 0);
                        meta.appendChild(layerCountDiv);

                        card.appendChild(meta);

                        if (order.layers && order.layers.length > 0) {
                            var layersList = document.createElement('div');
                            layersList.className = 'layers-list';

                            order.layers.forEach(function(layer) {
                                var item = document.createElement('div');
                                item.className = 'layer-item';
                                var isPublished = order.published_layers && order.published_layers.indexOf(layer) !== -1;
                                if (isPublished) {
                                    item.classList.add('published');
                                    item.textContent = String.fromCharCode(10003) + ' ' + layer;
                                } else {
                                    item.textContent = String.fromCharCode(9675) + ' ' + layer;
                                }
                                layersList.appendChild(item);
                            });

                            card.appendChild(layersList);
                        }

                        var actions = document.createElement('div');
                        actions.className = 'order-actions';

                        // Update button
                        var updateBtn = document.createElement('button');
                        updateBtn.className = 'btn btn-update';
                        updateBtn.textContent = 'Check Updates';
                        updateBtn.id = 'update-btn-' + order.order_id;

                        (function(orderId, btn) {
                            btn.onclick = function() { checkAndUpdateOrder(orderId); };
                        })(order.order_id, updateBtn);

                        actions.appendChild(updateBtn);

                        var publishBtn = document.createElement('button');
                        publishBtn.className = 'btn ' + (order.is_published ? 'btn-published' : 'btn-publish');
                        publishBtn.textContent = order.is_published ? 'Re-publish' : 'Publish to PostGIS';
                        publishBtn.disabled = !dbConnected || !order.is_publishable;

                        (function(orderId) {
                            publishBtn.onclick = function() { publishOrder(orderId); };
                        })(order.order_id);

                        if (!order.is_publishable) {
                            publishBtn.title = 'This data type is not yet supported for publishing';
                        }

                        actions.appendChild(publishBtn);
                        card.appendChild(actions);

                        fragment.appendChild(card);
                    });

                    grid.replaceChildren(fragment);

                    // Re-render layer toggles after layers are discovered
                    if (typeof MapViewer !== 'undefined') {
                        if (MapViewer.layersDiscovered) {
                            MapViewer.renderLayerToggles();
                        } else {
                            // Layers not yet discovered, poll until ready
                            var checkInterval = setInterval(function() {
                                if (MapViewer.layersDiscovered) {
                                    MapViewer.renderLayerToggles();
                                    clearInterval(checkInterval);
                                }
                            }, 200);
                            // Stop polling after 5 seconds
                            setTimeout(function() { clearInterval(checkInterval); }, 5000);
                        }
                    }
                })
                .catch(function(e) {
                    console.error('Failed to load orders:', e);
                });
        }

        function publishOrder(orderId) {
            var overlay = document.getElementById('progressOverlay');
            var title = document.getElementById('progressTitle');
            var overallBar = document.getElementById('progressBar');
            var overallValue = document.getElementById('progressOverallValue');
            var message = document.getElementById('progressMessage');

            resetProgressModal();
            title.textContent = 'Publishing to PostGIS';
            overallValue.textContent = '0 / 0 layers';
            message.textContent = 'Starting...';
            overlay.classList.add('active');

            addLogEntry('Starting publish for order: ' + orderId, 'info');

            fetch('/api/orders/' + orderId + '/publish', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({})
            })
            .then(function(resp) { return resp.json(); })
            .then(function(data) {
                if (data.error) {
                    message.textContent = 'Error: ' + data.error;
                    addLogEntry('Error: ' + data.error, 'error');
                    showCloseButton();
                    return;
                }

                addLogEntry('Connected to progress stream', 'info');
                var eventSource = new EventSource(data.progress_url);

                eventSource.onmessage = function(event) {
                    var progress = JSON.parse(event.data);

                    if (progress.status === 'keepalive') return;

                    // Log the progress message
                    if (progress.message) {
                        var logType = 'info';
                        if (progress.status === 'error') logType = 'error';
                        else if (progress.status === 'completed') logType = 'success';
                        addLogEntry(progress.message, logType);
                    }

                    // Log current layer being processed
                    if (progress.current_layer && progress.status === 'processing') {
                        addLogEntry('Processing layer: ' + progress.current_layer, 'info');
                    }

                    if (progress.layers_total > 0) {
                        var pct = (progress.layers_done / progress.layers_total) * 100;
                        overallBar.style.width = pct + '%';
                        overallValue.textContent = progress.layers_done + ' / ' + progress.layers_total + ' layers';
                    }

                    message.textContent = progress.message || progress.status;

                    if (progress.status === 'completed') {
                        overallBar.style.width = '100%';
                        overallValue.textContent = progress.layers_total + ' / ' + progress.layers_total + ' layers';
                        message.textContent = 'Publish complete!';
                        addLogEntry('Publish completed successfully', 'success');
                        eventSource.close();
                        showCloseButton();
                        // Restart Martin to pick up new tables
                        fetch('/api/martin/restart', { method: 'POST' })
                            .then(function() {
                                addLogEntry('Martin restarted for new layers', 'info');
                                checkMartinStatus();
                            })
                            .catch(function() {});
                        setTimeout(function() {
                            closeProgressModal();
                        }, 1500);
                    } else if (progress.status === 'error') {
                        // Handle multi-line error messages
                        var errorLines = (progress.error || 'Unknown error').split('\\n');
                        message.textContent = 'Error: ' + errorLines[0];
                        errorLines.forEach(function(line) {
                            if (line.trim()) {
                                addLogEntry(line, 'error');
                            }
                        });
                        // Auto-show log on error
                        document.getElementById('progressLog').classList.add('visible');
                        document.getElementById('progressLogToggle').classList.add('active');
                        document.getElementById('progressLogToggle').textContent = 'Hide Log';
                        eventSource.close();
                        // Show close button - don't auto-close on error
                        showCloseButton();
                    }
                };

                eventSource.onerror = function() {
                    eventSource.close();
                    message.textContent = 'Connection lost';
                    addLogEntry('Connection to server lost', 'error');
                    showCloseButton();
                };
            })
            .catch(function(e) {
                message.textContent = 'Error: ' + e.message;
                addLogEntry('Request failed: ' + e.message, 'error');
                showCloseButton();
            });
        }

        function checkAndUpdateOrder(orderId) {
            var btn = document.getElementById('update-btn-' + orderId);
            if (!btn) return;

            var originalText = btn.textContent;
            btn.textContent = 'Checking...';
            btn.disabled = true;

            fetch('/api/orders/' + orderId + '/check-updates')
                .then(function(resp) { return resp.json(); })
                .then(function(data) {
                    if (data.error) {
                        alert('Error checking updates: ' + data.error);
                        btn.textContent = originalText;
                        btn.disabled = false;
                        return;
                    }

                    if (data.has_update) {
                        // Show update available
                        btn.className = 'btn btn-update has-update';
                        btn.textContent = 'Download Update';

                        var newFilesCount = data.new_files ? data.new_files.length : 0;
                        var confirmMsg = 'Updates available!\\n\\n';
                        confirmMsg += 'Local: ' + (data.local_date ? data.local_date.substring(0, 10) : 'N/A') + '\\n';
                        confirmMsg += 'Remote: ' + (data.remote_date ? data.remote_date.substring(0, 10) : 'N/A') + '\\n';
                        confirmMsg += 'New/updated files: ' + newFilesCount + '\\n\\n';
                        confirmMsg += 'Download updates now?';

                        if (confirm(confirmMsg)) {
                            // Trigger download
                            startDownloadForOrder(orderId);
                        }
                        btn.disabled = false;
                    } else {
                        // No updates
                        btn.textContent = 'Up to date';
                        btn.className = 'btn btn-update';
                        setTimeout(function() {
                            btn.textContent = 'Check Updates';
                            btn.disabled = false;
                        }, 2000);
                    }
                })
                .catch(function(e) {
                    alert('Failed to check updates: ' + e.message);
                    btn.textContent = originalText;
                    btn.disabled = false;
                });
        }

        function startDownloadForOrder(orderId) {
            var overlay = document.getElementById('progressOverlay');
            var title = document.getElementById('progressTitle');
            var overallBar = document.getElementById('progressBar');
            var overallValue = document.getElementById('progressOverallValue');
            var fileSection = document.getElementById('progressFileSection');
            var fileBar = document.getElementById('progressFileBar');
            var fileValue = document.getElementById('progressFileValue');
            var fileName = document.getElementById('progressFileName');
            var message = document.getElementById('progressMessage');

            // Reset and show modal
            resetProgressModal();
            title.textContent = 'Downloading Update';
            message.textContent = 'Fetching file list...';
            overlay.classList.add('active');

            var lastLoggedFile = '';

            addLogEntry('Starting update download for order: ' + orderId, 'info');

            fetch('/api/download/' + orderId, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({})
            })
            .then(function(resp) { return resp.json(); })
            .then(function(data) {
                if (data.error) {
                    message.textContent = 'Error: ' + data.error;
                    addLogEntry('Error: ' + data.error, 'error');
                    showCloseButton();
                    return;
                }

                addLogEntry('Connected to progress stream', 'info');
                var eventSource = new EventSource(data.progress_url);

                eventSource.onmessage = function(event) {
                    var progress = JSON.parse(event.data);

                    if (progress.status === 'keepalive') return;

                    // Log status changes and new files
                    if (progress.status === 'fetching_list') {
                        addLogEntry('Fetching file list from server...', 'info');
                    } else if (progress.status === 'downloading' && progress.current_file && progress.current_file !== lastLoggedFile) {
                        lastLoggedFile = progress.current_file;
                        addLogEntry('Downloading: ' + progress.current_file, 'info');
                    }

                    // Update overall progress
                    if (progress.files_total > 0) {
                        var overallPct = (progress.files_done / progress.files_total) * 100;
                        overallBar.style.width = overallPct + '%';
                        overallValue.textContent = progress.files_done + ' / ' + progress.files_total + ' files';
                    }

                    // Update file progress
                    if (progress.status === 'downloading' && progress.current_file) {
                        fileSection.style.display = 'block';
                        fileName.textContent = progress.current_file;

                        if (progress.bytes_total > 0) {
                            var filePct = (progress.bytes_downloaded / progress.bytes_total) * 100;
                            fileBar.style.width = filePct + '%';
                            fileValue.textContent = formatBytes(progress.bytes_downloaded) + ' / ' + formatBytes(progress.bytes_total);
                        }
                    }

                    message.textContent = progress.message || progress.status;

                    if (progress.status === 'completed') {
                        overallBar.style.width = '100%';
                        fileBar.style.width = '100%';
                        overallValue.textContent = progress.files_total + ' / ' + progress.files_total + ' files';
                        message.textContent = 'Update complete!';
                        addLogEntry('Update completed: ' + progress.files_total + ' files', 'success');
                        eventSource.close();
                        showCloseButton();
                        setTimeout(function() {
                            closeProgressModal();
                        }, 1500);
                    } else if (progress.status === 'error') {
                        message.textContent = 'Error: ' + progress.error;
                        addLogEntry('Error: ' + progress.error, 'error');
                        eventSource.close();
                        showCloseButton();
                    }
                };

                eventSource.onerror = function() {
                    eventSource.close();
                    message.textContent = 'Connection lost';
                    addLogEntry('Connection to server lost', 'error');
                    showCloseButton();
                };
            })
            .catch(function(e) {
                message.textContent = 'Error: ' + e.message;
                addLogEntry('Request failed: ' + e.message, 'error');
                showCloseButton();
            });
        }

        function startDownload() {
            var orderId = document.getElementById('orderIdInput').value.trim();
            if (!orderId) {
                alert('Please enter an order ID');
                return;
            }

            var overlay = document.getElementById('progressOverlay');
            var title = document.getElementById('progressTitle');
            var overallBar = document.getElementById('progressBar');
            var overallValue = document.getElementById('progressOverallValue');
            var fileSection = document.getElementById('progressFileSection');
            var fileBar = document.getElementById('progressFileBar');
            var fileValue = document.getElementById('progressFileValue');
            var fileName = document.getElementById('progressFileName');
            var message = document.getElementById('progressMessage');
            var downloadBtn = document.getElementById('downloadBtn');

            // Reset and show modal
            resetProgressModal();
            title.textContent = 'Downloading Order';
            message.textContent = 'Fetching file list...';
            overlay.classList.add('active');
            downloadBtn.disabled = true;

            var currentFileSize = 0;
            var currentFileDownloaded = 0;
            var lastLoggedFile = '';

            addLogEntry('Starting download for order: ' + orderId, 'info');

            fetch('/api/download/' + orderId, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({})
            })
            .then(function(resp) { return resp.json(); })
            .then(function(data) {
                if (data.error) {
                    message.textContent = 'Error: ' + data.error;
                    addLogEntry('Error: ' + data.error, 'error');
                    downloadBtn.disabled = false;
                    showCloseButton();
                    return;
                }

                addLogEntry('Connected to progress stream', 'info');
                var eventSource = new EventSource(data.progress_url);

                eventSource.onmessage = function(event) {
                    var progress = JSON.parse(event.data);

                    if (progress.status === 'keepalive') return;

                    // Log status changes and new files
                    if (progress.status === 'fetching_list') {
                        addLogEntry('Fetching file list from server...', 'info');
                    } else if (progress.status === 'downloading' && progress.current_file && progress.current_file !== lastLoggedFile) {
                        lastLoggedFile = progress.current_file;
                        addLogEntry('Downloading: ' + progress.current_file, 'info');
                    }

                    // Update overall progress
                    if (progress.files_total > 0) {
                        var overallPct = (progress.files_done / progress.files_total) * 100;
                        overallBar.style.width = overallPct + '%';
                        overallValue.textContent = progress.files_done + ' / ' + progress.files_total + ' files';
                    }

                    // Update file progress
                    if (progress.status === 'downloading' && progress.current_file) {
                        fileSection.style.display = 'block';
                        fileName.textContent = progress.current_file;

                        if (progress.bytes_total > 0) {
                            var filePct = (progress.bytes_downloaded / progress.bytes_total) * 100;
                            fileBar.style.width = filePct + '%';
                            fileValue.textContent = formatBytes(progress.bytes_downloaded) + ' / ' + formatBytes(progress.bytes_total);
                        }
                    }

                    message.textContent = progress.message || progress.status;

                    if (progress.status === 'completed') {
                        overallBar.style.width = '100%';
                        fileBar.style.width = '100%';
                        overallValue.textContent = progress.files_total + ' / ' + progress.files_total + ' files';
                        message.textContent = 'Download complete!';
                        addLogEntry('Download completed: ' + progress.files_total + ' files', 'success');
                        eventSource.close();
                        downloadBtn.disabled = false;
                        document.getElementById('orderIdInput').value = '';
                        showCloseButton();
                        setTimeout(function() {
                            closeProgressModal();
                        }, 1500);
                    } else if (progress.status === 'error') {
                        message.textContent = 'Error: ' + progress.error;
                        addLogEntry('Error: ' + progress.error, 'error');
                        eventSource.close();
                        downloadBtn.disabled = false;
                        showCloseButton();
                    }
                };

                eventSource.onerror = function() {
                    eventSource.close();
                    message.textContent = 'Connection lost';
                    addLogEntry('Connection to server lost', 'error');
                    downloadBtn.disabled = false;
                    showCloseButton();
                };
            })
            .catch(function(e) {
                message.textContent = 'Error: ' + e.message;
                addLogEntry('Request failed: ' + e.message, 'error');
                downloadBtn.disabled = false;
                showCloseButton();
            });
        }

        // Allow Enter key to trigger download
        document.getElementById('orderIdInput').addEventListener('keypress', function(e) {
            if (e.key === 'Enter') {
                startDownload();
            }
        });

        checkDbStatus();
        checkMartinStatus();
        loadOrders();

        setInterval(function() {
            checkDbStatus();
            checkMartinStatus();
            loadOrders();
        }, 30000);

        // MapViewer - MapLibre GL JS integration
        var MapViewer = {
            map: null,
            layers: {},
            layersDiscovered: false,
            reloadDebounceTimer: null,
            martinAvailable: false,
            martinUrl: 'http://127.0.0.1:3000',

            // Initialize the map
            init: function() {
                this.map = new maplibregl.Map({
                    container: 'maplibre-map',
                    style: {
                        version: 8,
                        sources: {
                            'carto-dark': {
                                type: 'raster',
                                tiles: [
                                    'https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png',
                                    'https://b.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png',
                                    'https://c.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png'
                                ],
                                tileSize: 256,
                                attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>'
                            }
                        },
                        layers: [{
                            id: 'carto-dark',
                            type: 'raster',
                            source: 'carto-dark'
                        }],
                        glyphs: 'https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf'
                    },
                    center: [17, 62],
                    zoom: 4
                });

                this.map.addControl(new maplibregl.NavigationControl(), 'top-right');

                // Event handlers
                var self = this;
                this.map.on('moveend', function() { self.reloadVisibleLayers(); });
                this.map.on('click', function(e) { self.handleMapClick(e); });
                this.map.on('mousemove', function(e) { self.handleMouseMove(e); });

                // Discover layers after map loads
                this.map.on('load', function() {
                    self.discoverLayers();
                    self.checkMartinStatus();
                });
            },

            // Check Martin tile server status
            checkMartinStatus: function() {
                var self = this;
                fetch('/api/martin/status')
                    .then(function(resp) { return resp.json(); })
                    .then(function(data) {
                        self.martinAvailable = data.running;
                        self.updateMartinStatusUI(data);
                        if (data.running) {
                            console.log('Martin available at ' + self.martinUrl + ', using vector tiles');
                        } else {
                            console.log('Martin not available, using GeoJSON fallback');
                        }
                    })
                    .catch(function(e) {
                        console.error('Failed to check Martin status:', e);
                        self.martinAvailable = false;
                        self.updateMartinStatusUI({ installed: false, running: false });
                    });
            },

            // Update Martin status in UI
            updateMartinStatusUI: function(data) {
                var dot = document.getElementById('martinStatus');
                var text = document.getElementById('martinStatusText');
                if (!dot || !text) return;

                if (!data.installed) {
                    dot.className = 'status-dot';
                    text.textContent = 'Tiles: not installed';
                } else if (data.running) {
                    dot.className = 'status-dot connected';
                    text.textContent = 'Tiles: Martin running';
                } else {
                    dot.className = 'status-dot disconnected';
                    text.textContent = 'Tiles: Martin stopped';
                }
            },

            // Generate color from string hash
            hashColor: function(str) {
                var hash = 0;
                for (var i = 0; i < str.length; i++) {
                    hash = str.charCodeAt(i) + ((hash << 5) - hash);
                }
                var hue = hash % 360;
                return 'hsl(' + hue + ', 70%, 60%)';
            },

            // Discover available layers from API
            discoverLayers: function() {
                var self = this;
                fetch('/api/layers')
                    .then(function(resp) { return resp.json(); })
                    .then(function(layers) {
                        var promises = layers.map(function(layerName) {
                            return fetch('/api/layers/' + layerName)
                                .then(function(r) { return r.json(); })
                                .then(function(info) {
                                    self.layers[layerName] = {
                                        name: layerName,
                                        source_order: info.source_order,
                                        geometry_type: info.geometry_type,
                                        feature_count: info.feature_count,
                                        color: self.hashColor(layerName),
                                        visible: false,
                                        loading: false
                                    };
                                });
                        });
                        return Promise.all(promises);
                    })
                    .then(function() {
                        self.updateLayerToggles();
                    })
                    .catch(function(e) {
                        console.error('Failed to discover layers:', e);
                    });
            },

            // Update layer toggles in order cards
            updateLayerToggles: function() {
                var self = this;
                this.layersDiscovered = true;

                // Restore from localStorage
                var savedState = localStorage.getItem('mapLayerState');
                if (savedState) {
                    try {
                        var state = JSON.parse(savedState);
                        Object.keys(state).forEach(function(layerName) {
                            if (self.layers[layerName]) {
                                self.layers[layerName].visible = state[layerName];
                            }
                        });
                    } catch(e) {}
                }

                this.renderLayerToggles();

                // Restore visible layers on map
                Object.keys(this.layers).forEach(function(layerName) {
                    if (self.layers[layerName].visible) {
                        self.loadLayerFeatures(layerName);
                    }
                });
            },

            // Render layer toggles in order cards
            renderLayerToggles: function() {
                var self = this;

                // Find all layer items and add checkboxes to those that match published layers
                var layerItems = document.querySelectorAll('.layer-item.published');
                layerItems.forEach(function(item) {
                    // Extract layer name from text (format is "✓ layername")
                    var text = item.textContent.trim();
                    var layerName = text.replace(/^[\u2713\u25CB]\s*/, ''); // Remove checkmark or circle prefix

                    var layer = self.layers[layerName];
                    if (!layer) return;

                    // Check if already has checkbox
                    if (item.querySelector('input[type="checkbox"]')) return;

                    // Clear existing content and rebuild with checkbox
                    item.textContent = '';
                    item.style.display = 'flex';
                    item.style.alignItems = 'center';
                    item.style.gap = '8px';
                    item.style.cursor = 'pointer';

                    var checkbox = document.createElement('input');
                    checkbox.type = 'checkbox';
                    checkbox.checked = layer.visible;
                    checkbox.style.cursor = 'pointer';
                    checkbox.dataset.layer = layerName;
                    checkbox.addEventListener('change', function(e) {
                        e.stopPropagation();
                        self.toggleLayer(layerName, checkbox.checked);
                    });

                    var swatch = document.createElement('span');
                    swatch.className = 'layer-color-swatch';
                    swatch.style.backgroundColor = layer.color;
                    swatch.style.width = '12px';
                    swatch.style.height = '12px';
                    swatch.style.borderRadius = '2px';
                    swatch.style.display = 'inline-block';
                    swatch.style.flexShrink = '0';

                    var label = document.createElement('span');
                    label.textContent = layerName;
                    label.style.flex = '1';

                    item.appendChild(checkbox);
                    item.appendChild(swatch);
                    item.appendChild(label);

                    // Make entire row clickable
                    item.addEventListener('click', function(e) {
                        if (e.target !== checkbox) {
                            checkbox.checked = !checkbox.checked;
                            self.toggleLayer(layerName, checkbox.checked);
                        }
                    });
                });
            },

            // Toggle layer visibility
            toggleLayer: function(layerName, visible) {
                if (!this.layers[layerName]) return;

                this.layers[layerName].visible = visible;
                this.saveLayerState();

                if (visible) {
                    this.loadLayerFeatures(layerName);
                } else {
                    this.removeLayerFromMap(layerName);
                }
            },

            // Load layer features from API or use vector tiles
            loadLayerFeatures: function(layerName) {
                var self = this;
                var layer = this.layers[layerName];
                if (!layer) return;

                // Use Martin vector tiles if available
                if (this.martinAvailable) {
                    this.addVectorTileLayer(layerName);
                    return;
                }

                // Fallback to GeoJSON
                layer.loading = true;
                this.updateLoadingState(layerName, true);

                var bounds = this.map.getBounds();
                var bbox = [
                    bounds.getWest(),
                    bounds.getSouth(),
                    bounds.getEast(),
                    bounds.getNorth()
                ].join(',');

                // Request WGS84 output for map display, bbox in WGS84 needs conversion
                // Use srid=4326 for both input bbox and output coordinates
                fetch('/api/layers/' + layerName + '/features?bbox=' + bbox + '&limit=5000&srid=4326&bbox_srid=4326')
                    .then(function(resp) { return resp.json(); })
                    .then(function(geojson) {
                        layer.loading = false;
                        self.updateLoadingState(layerName, false);
                        self.addGeoJSONLayer(layerName, geojson);
                    })
                    .catch(function(e) {
                        console.error('Failed to load layer features:', e);
                        layer.loading = false;
                        self.updateLoadingState(layerName, false);
                    });
            },

            // Add vector tile layer from Martin
            addVectorTileLayer: function(layerName) {
                var layer = this.layers[layerName];
                if (!layer) return;

                var sourceId = 'layer-' + layerName;
                var layerId = 'layer-' + layerName;

                // Remove existing layers/source
                this.removeLayerFromMap(layerName);

                // Add vector tile source from Martin
                this.map.addSource(sourceId, {
                    type: 'vector',
                    tiles: [this.martinUrl + '/' + layerName + '/{z}/{x}/{y}'],
                    minzoom: 0,
                    maxzoom: 14
                });

                // Add layer based on geometry type
                var geometryType = layer.geometry_type.toLowerCase();

                if (geometryType.indexOf('point') !== -1) {
                    this.map.addLayer({
                        id: layerId,
                        type: 'circle',
                        source: sourceId,
                        'source-layer': layerName,
                        paint: {
                            'circle-radius': 5,
                            'circle-color': layer.color,
                            'circle-stroke-width': 1,
                            'circle-stroke-color': '#ffffff'
                        }
                    });
                } else if (geometryType.indexOf('line') !== -1) {
                    this.map.addLayer({
                        id: layerId,
                        type: 'line',
                        source: sourceId,
                        'source-layer': layerName,
                        paint: {
                            'line-color': layer.color,
                            'line-width': 2
                        }
                    });
                } else if (geometryType.indexOf('polygon') !== -1) {
                    // Add fill layer
                    this.map.addLayer({
                        id: layerId + '-fill',
                        type: 'fill',
                        source: sourceId,
                        'source-layer': layerName,
                        paint: {
                            'fill-color': layer.color,
                            'fill-opacity': 0.3
                        }
                    });
                    // Add outline layer
                    this.map.addLayer({
                        id: layerId,
                        type: 'line',
                        source: sourceId,
                        'source-layer': layerName,
                        paint: {
                            'line-color': layer.color,
                            'line-width': 2
                        }
                    });
                } else {
                    // Default to circle
                    this.map.addLayer({
                        id: layerId,
                        type: 'circle',
                        source: sourceId,
                        'source-layer': layerName,
                        paint: {
                            'circle-radius': 5,
                            'circle-color': layer.color
                        }
                    });
                }
            },

            // Update loading state in UI
            updateLoadingState: function(layerName, loading) {
                var toggle = document.querySelector('[data-layer="' + layerName + '"]');
                if (!toggle) return;

                var existingLoading = toggle.querySelector('.layer-loading');
                if (loading && !existingLoading) {
                    var loadingSpan = document.createElement('span');
                    loadingSpan.className = 'layer-loading';
                    loadingSpan.textContent = 'Loading...';
                    toggle.appendChild(loadingSpan);
                } else if (!loading && existingLoading) {
                    existingLoading.remove();
                }
            },

            // Add GeoJSON layer to map (fallback when Martin not available)
            addGeoJSONLayer: function(layerName, geojson) {
                var layer = this.layers[layerName];
                if (!layer) return;

                var sourceId = 'layer-' + layerName;
                var layerId = 'layer-' + layerName;

                // Remove existing layer/source if present
                if (this.map.getLayer(layerId)) {
                    this.map.removeLayer(layerId);
                }
                if (this.map.getSource(sourceId)) {
                    this.map.removeSource(sourceId);
                }

                // Add source
                this.map.addSource(sourceId, {
                    type: 'geojson',
                    data: geojson
                });

                // Add layer based on geometry type
                var geometryType = layer.geometry_type.toLowerCase();
                var layerConfig = {
                    id: layerId,
                    source: sourceId
                };

                if (geometryType.indexOf('point') !== -1) {
                    layerConfig.type = 'circle';
                    layerConfig.paint = {
                        'circle-radius': 5,
                        'circle-color': layer.color,
                        'circle-stroke-width': 1,
                        'circle-stroke-color': '#ffffff'
                    };
                } else if (geometryType.indexOf('line') !== -1) {
                    layerConfig.type = 'line';
                    layerConfig.paint = {
                        'line-color': layer.color,
                        'line-width': 2
                    };
                } else if (geometryType.indexOf('polygon') !== -1) {
                    // Add fill
                    var fillLayerId = layerId + '-fill';
                    this.map.addLayer({
                        id: fillLayerId,
                        type: 'fill',
                        source: sourceId,
                        paint: {
                            'fill-color': layer.color,
                            'fill-opacity': 0.3
                        }
                    });
                    // Add outline
                    layerConfig.type = 'line';
                    layerConfig.paint = {
                        'line-color': layer.color,
                        'line-width': 2
                    };
                } else {
                    // Default to circle
                    layerConfig.type = 'circle';
                    layerConfig.paint = {
                        'circle-radius': 5,
                        'circle-color': layer.color
                    };
                }

                this.map.addLayer(layerConfig);
            },

            // Remove layer from map
            removeLayerFromMap: function(layerName) {
                var layerId = 'layer-' + layerName;
                var fillLayerId = layerId + '-fill';
                var sourceId = 'layer-' + layerName;

                if (this.map.getLayer(fillLayerId)) {
                    this.map.removeLayer(fillLayerId);
                }
                if (this.map.getLayer(layerId)) {
                    this.map.removeLayer(layerId);
                }
                if (this.map.getSource(sourceId)) {
                    this.map.removeSource(sourceId);
                }
            },

            // Reload visible layers (debounced) - only for GeoJSON layers
            reloadVisibleLayers: function() {
                // Skip reload when using Martin (vector tiles handle this automatically)
                if (this.martinAvailable) return;

                var self = this;
                if (this.reloadDebounceTimer) {
                    clearTimeout(this.reloadDebounceTimer);
                }
                this.reloadDebounceTimer = setTimeout(function() {
                    Object.keys(self.layers).forEach(function(layerName) {
                        if (self.layers[layerName].visible) {
                            self.loadLayerFeatures(layerName);
                        }
                    });
                }, 500);
            },

            // Handle map click - show popup
            handleMapClick: function(e) {
                var self = this;
                var features = this.map.queryRenderedFeatures(e.point);
                if (features.length === 0) return;

                var feature = features[0];

                // Skip if clicking on trains, weather, or lidar layers (they have their own popups)
                if (feature.layer && (feature.layer.id === 'trains-layer' || feature.layer.id === 'weather-layer' || feature.layer.id === 'lidar-tiles-fill')) {
                    return;
                }
                var popupContent = document.createElement('div');

                Object.keys(feature.properties).forEach(function(key) {
                    var prop = document.createElement('div');
                    prop.className = 'popup-property';

                    var keyElem = document.createElement('div');
                    keyElem.className = 'popup-property-key';
                    keyElem.textContent = key;

                    var valueElem = document.createElement('div');
                    valueElem.className = 'popup-property-value';
                    valueElem.textContent = feature.properties[key];

                    prop.appendChild(keyElem);
                    prop.appendChild(valueElem);
                    popupContent.appendChild(prop);
                });

                new maplibregl.Popup()
                    .setLngLat(e.lngLat)
                    .setDOMContent(popupContent)
                    .addTo(this.map);
            },

            // Handle mouse move - change cursor
            handleMouseMove: function(e) {
                var features = this.map.queryRenderedFeatures(e.point);
                this.map.getCanvas().style.cursor = features.length > 0 ? 'pointer' : '';
            },

            // Save layer state to localStorage
            saveLayerState: function() {
                var state = {};
                Object.keys(this.layers).forEach(function(layerName) {
                    state[layerName] = this.layers[layerName].visible;
                }, this);
                localStorage.setItem('mapLayerState', JSON.stringify(state));
            }
        };

        // Initialize map when page loads
        if (typeof maplibregl !== 'undefined') {
            MapViewer.init();
        }

        // ==================== Train Viewer ====================
        var TrainViewer = {
            stations: null,
            trains: [],
            enabled: false,
            apiUrl: 'https://api.trafikinfo.trafikverket.se/v2/data.json',
            handlersAdded: false,

            init: function() {
                var self = this;
                var toggle = document.getElementById('trainToggle');
                var refreshBtn = document.getElementById('trainRefreshBtn');

                if (!toggle || !refreshBtn) return;

                toggle.addEventListener('change', function() {
                    self.enabled = this.checked;
                    refreshBtn.classList.toggle('visible', self.enabled);
                    if (self.enabled) {
                        self.loadTrains();
                    } else {
                        self.clearTrains();
                        document.getElementById('trainStatus').textContent = '';
                    }
                });

                refreshBtn.addEventListener('click', function() {
                    self.loadTrains();
                });
            },

            fetchStations: async function() {
                if (this.stations) return this.stations;

                var xml = '<REQUEST>' +
                    '<LOGIN authenticationkey="demokey"/>' +
                    '<QUERY objecttype="TrainStation" schemaversion="1.4">' +
                    '<FILTER><EQ name="Advertised" value="true"/></FILTER>' +
                    '<INCLUDE>LocationSignature</INCLUDE>' +
                    '<INCLUDE>AdvertisedLocationName</INCLUDE>' +
                    '<INCLUDE>Geometry.WGS84</INCLUDE>' +
                    '</QUERY></REQUEST>';

                var response = await fetch(this.apiUrl, {
                    method: 'POST',
                    headers: { 'Content-Type': 'text/xml' },
                    body: xml
                });
                var data = await response.json();

                this.stations = {};
                var stationList = data.RESPONSE?.RESULT?.[0]?.TrainStation || [];
                stationList.forEach(function(s) {
                    if (s.Geometry?.WGS84) {
                        var match = s.Geometry.WGS84.match(/POINT \\(([\\d.]+) ([\\d.]+)\\)/);
                        if (match) {
                            this.stations[s.LocationSignature] = {
                                name: s.AdvertisedLocationName,
                                lng: parseFloat(match[1]),
                                lat: parseFloat(match[2])
                            };
                        }
                    }
                }, this);

                return this.stations;
            },

            fetchTrains: async function() {
                var xml = '<REQUEST>' +
                    '<LOGIN authenticationkey="demokey"/>' +
                    '<QUERY objecttype="TrainAnnouncement" schemaversion="1.9" limit="500">' +
                    '<FILTER>' +
                    '<AND>' +
                    '<EXISTS name="TimeAtLocation" value="true"/>' +
                    '<GT name="TimeAtLocation" value="$dateadd(-00:15:00)"/>' +
                    '<LT name="TimeAtLocation" value="$dateadd(00:05:00)"/>' +
                    '</AND>' +
                    '</FILTER>' +
                    '<INCLUDE>AdvertisedTrainIdent</INCLUDE>' +
                    '<INCLUDE>LocationSignature</INCLUDE>' +
                    '<INCLUDE>FromLocation</INCLUDE>' +
                    '<INCLUDE>ToLocation</INCLUDE>' +
                    '<INCLUDE>TimeAtLocation</INCLUDE>' +
                    '<INCLUDE>AdvertisedTimeAtLocation</INCLUDE>' +
                    '<INCLUDE>Operator</INCLUDE>' +
                    '<INCLUDE>ActivityType</INCLUDE>' +
                    '</QUERY></REQUEST>';

                var response = await fetch(this.apiUrl, {
                    method: 'POST',
                    headers: { 'Content-Type': 'text/xml' },
                    body: xml
                });
                var data = await response.json();
                return data.RESPONSE?.RESULT?.[0]?.TrainAnnouncement || [];
            },

            loadTrains: async function() {
                var statusEl = document.getElementById('trainStatus');
                var refreshBtn = document.getElementById('trainRefreshBtn');
                refreshBtn.disabled = true;
                statusEl.textContent = 'Loading...';

                try {
                    await this.fetchStations();
                    var announcements = await this.fetchTrains();

                    // Group by train ID, keep most recent per train
                    var trainMap = {};
                    announcements.forEach(function(a) {
                        var id = a.AdvertisedTrainIdent;
                        var station = this.stations[a.LocationSignature];
                        if (!station) return;

                        var delay = 0;
                        if (a.TimeAtLocation && a.AdvertisedTimeAtLocation) {
                            var actual = new Date(a.TimeAtLocation);
                            var scheduled = new Date(a.AdvertisedTimeAtLocation);
                            delay = Math.round((actual - scheduled) / 60000);
                        }

                        var from = a.FromLocation?.[0]?.LocationName || '';
                        var to = a.ToLocation?.[0]?.LocationName || '';

                        // Resolve station names
                        if (this.stations[from]) from = this.stations[from].name;
                        if (this.stations[to]) to = this.stations[to].name;

                        if (!trainMap[id] || new Date(a.TimeAtLocation) > new Date(trainMap[id].time)) {
                            trainMap[id] = {
                                id: id,
                                lng: station.lng,
                                lat: station.lat,
                                stationName: station.name,
                                from: from,
                                to: to,
                                delay: delay,
                                operator: a.Operator || 'Unknown',
                                time: a.TimeAtLocation
                            };
                        }
                    }, this);

                    this.trains = Object.values(trainMap);
                    this.renderTrains();

                    var now = new Date().toLocaleTimeString('sv-SE', { hour: '2-digit', minute: '2-digit' });
                    statusEl.textContent = this.trains.length + ' trains - Updated ' + now;

                } catch (err) {
                    statusEl.textContent = 'Error: ' + err.message;
                }

                refreshBtn.disabled = false;
            },

            renderTrains: function() {
                if (!MapViewer.map) return;

                // Remove existing layer and source
                if (MapViewer.map.getLayer('trains-layer')) {
                    MapViewer.map.removeLayer('trains-layer');
                }
                if (MapViewer.map.getSource('trains-source')) {
                    MapViewer.map.removeSource('trains-source');
                }

                if (this.trains.length === 0) return;

                // Create GeoJSON
                var geojson = {
                    type: 'FeatureCollection',
                    features: this.trains.map(function(t) {
                        return {
                            type: 'Feature',
                            geometry: {
                                type: 'Point',
                                coordinates: [t.lng, t.lat]
                            },
                            properties: t
                        };
                    })
                };

                MapViewer.map.addSource('trains-source', {
                    type: 'geojson',
                    data: geojson
                });

                MapViewer.map.addLayer({
                    id: 'trains-layer',
                    type: 'circle',
                    source: 'trains-source',
                    paint: {
                        'circle-radius': 8,
                        'circle-color': [
                            'case',
                            ['<', ['get', 'delay'], 1], '#48bb78',
                            ['<', ['get', 'delay'], 10], '#ed8936',
                            '#f56565'
                        ],
                        'circle-stroke-width': 2,
                        'circle-stroke-color': '#ffffff'
                    }
                });

                // Add click handler only once
                if (!this.handlersAdded) {
                    var self = this;
                    MapViewer.map.on('click', 'trains-layer', function(e) {
                        var props = e.features[0].properties;
                        var delayClass = props.delay < 1 ? 'delay-ok' : (props.delay < 10 ? 'delay-warn' : 'delay-bad');
                        var delayText = props.delay <= 0 ? 'On time' : props.delay + ' min delay';

                        var html = '<div class="train-popup">' +
                            '<h4>Train ' + props.id + '</h4>' +
                            '<p>' + (props.from || '?') + ' → ' + (props.to || '?') + '</p>' +
                            '<p>At: ' + props.stationName + '</p>' +
                            '<p class="' + delayClass + '">Status: ' + delayText + '</p>' +
                            '<p>Operator: ' + props.operator + '</p>' +
                            '</div>';

                        new maplibregl.Popup()
                            .setLngLat(e.lngLat)
                            .setHTML(html)
                            .addTo(MapViewer.map);
                    });

                    MapViewer.map.on('mouseenter', 'trains-layer', function() {
                        MapViewer.map.getCanvas().style.cursor = 'pointer';
                    });
                    MapViewer.map.on('mouseleave', 'trains-layer', function() {
                        MapViewer.map.getCanvas().style.cursor = '';
                    });
                    this.handlersAdded = true;
                }
            },

            clearTrains: function() {
                if (!MapViewer.map) return;
                if (MapViewer.map.getLayer('trains-layer')) {
                    MapViewer.map.removeLayer('trains-layer');
                }
                if (MapViewer.map.getSource('trains-source')) {
                    MapViewer.map.removeSource('trains-source');
                }
                this.trains = [];
            }
        };

        // Initialize train viewer
        TrainViewer.init();

        // ==================== Weather Viewer ====================
        var WeatherViewer = {
            stations: [],
            enabled: false,
            apiUrl: 'https://api.trafikinfo.trafikverket.se/v2/data.json',
            handlersAdded: false,

            init: function() {
                var self = this;
                var toggle = document.getElementById('weatherToggle');
                var refreshBtn = document.getElementById('weatherRefreshBtn');

                if (!toggle || !refreshBtn) return;

                toggle.addEventListener('change', function() {
                    self.enabled = this.checked;
                    refreshBtn.classList.toggle('visible', self.enabled);
                    if (self.enabled) {
                        self.loadWeather();
                    } else {
                        self.clearWeather();
                        document.getElementById('weatherStatus').textContent = '';
                    }
                });

                refreshBtn.addEventListener('click', function() {
                    self.loadWeather();
                });
            },

            fetchWeather: async function() {
                var xml = '<REQUEST>' +
                    '<LOGIN authenticationkey="demokey"/>' +
                    '<QUERY objecttype="WeatherMeasurepoint" namespace="road.weatherinfo" schemaversion="2.1">' +
                    '<FILTER></FILTER>' +
                    '<INCLUDE>Id</INCLUDE>' +
                    '<INCLUDE>Name</INCLUDE>' +
                    '<INCLUDE>Geometry.WGS84</INCLUDE>' +
                    '<INCLUDE>Observation.Sample</INCLUDE>' +
                    '<INCLUDE>Observation.Air.Temperature.Value</INCLUDE>' +
                    '<INCLUDE>Observation.Air.RelativeHumidity.Value</INCLUDE>' +
                    '<INCLUDE>Observation.Air.VisibleDistance.Value</INCLUDE>' +
                    '<INCLUDE>Observation.Wind</INCLUDE>' +
                    '<INCLUDE>Observation.Weather.Precipitation</INCLUDE>' +
                    '<INCLUDE>Observation.Surface.Temperature.Value</INCLUDE>' +
                    '</QUERY></REQUEST>';

                var response = await fetch(this.apiUrl, {
                    method: 'POST',
                    headers: { 'Content-Type': 'text/xml' },
                    body: xml
                });
                var data = await response.json();
                return data.RESPONSE?.RESULT?.[0]?.WeatherMeasurepoint || [];
            },

            loadWeather: async function() {
                var statusEl = document.getElementById('weatherStatus');
                var refreshBtn = document.getElementById('weatherRefreshBtn');
                refreshBtn.disabled = true;
                statusEl.textContent = 'Loading...';

                try {
                    var rawStations = await this.fetchWeather();

                    this.stations = rawStations.map(function(s) {
                        var coords = null;
                        if (s.Geometry?.WGS84) {
                            var match = s.Geometry.WGS84.match(/POINT \\(([\\d.-]+) ([\\d.-]+)\\)/);
                            if (match) {
                                coords = { lng: parseFloat(match[1]), lat: parseFloat(match[2]) };
                            }
                        }
                        if (!coords) return null;

                        var obs = s.Observation || {};
                        var wind = obs.Wind?.[0] || {};

                        return {
                            id: s.Id,
                            name: s.Name,
                            lng: coords.lng,
                            lat: coords.lat,
                            airTemp: obs.Air?.Temperature?.Value,
                            surfaceTemp: obs.Surface?.Temperature?.Value,
                            humidity: obs.Air?.RelativeHumidity?.Value,
                            visibility: obs.Air?.VisibleDistance?.Value,
                            windSpeed: wind.Speed?.Value,
                            windDir: wind.Direction?.Value,
                            precipitation: obs.Weather?.Precipitation || 'unknown',
                            sample: obs.Sample
                        };
                    }).filter(function(s) { return s !== null; });

                    this.renderWeather();

                    var now = new Date().toLocaleTimeString('sv-SE', { hour: '2-digit', minute: '2-digit' });
                    statusEl.textContent = this.stations.length + ' stations - Updated ' + now;

                } catch (err) {
                    statusEl.textContent = 'Error: ' + err.message;
                }

                refreshBtn.disabled = false;
            },

            renderWeather: function() {
                if (!MapViewer.map) return;

                // Remove existing layer and source
                if (MapViewer.map.getLayer('weather-layer')) {
                    MapViewer.map.removeLayer('weather-layer');
                }
                if (MapViewer.map.getSource('weather-source')) {
                    MapViewer.map.removeSource('weather-source');
                }

                if (this.stations.length === 0) return;

                // Create GeoJSON
                var geojson = {
                    type: 'FeatureCollection',
                    features: this.stations.map(function(s) {
                        return {
                            type: 'Feature',
                            geometry: {
                                type: 'Point',
                                coordinates: [s.lng, s.lat]
                            },
                            properties: s
                        };
                    })
                };

                MapViewer.map.addSource('weather-source', {
                    type: 'geojson',
                    data: geojson
                });

                MapViewer.map.addLayer({
                    id: 'weather-layer',
                    type: 'circle',
                    source: 'weather-source',
                    paint: {
                        'circle-radius': 7,
                        'circle-color': [
                            'case',
                            ['<', ['get', 'airTemp'], 0], '#3182ce',
                            ['<', ['get', 'airTemp'], 10], '#38a169',
                            ['<', ['get', 'airTemp'], 20], '#dd6b20',
                            '#e53e3e'
                        ],
                        'circle-stroke-width': 2,
                        'circle-stroke-color': '#1a1a2e'
                    }
                });

                // Add click handler only once
                if (!this.handlersAdded) {
                    var self = this;
                    MapViewer.map.on('click', 'weather-layer', function(e) {
                        var p = e.features[0].properties;

                        var tempClass = 'temp-cool';
                        if (p.airTemp < 0) tempClass = 'temp-cold';
                        else if (p.airTemp >= 20) tempClass = 'temp-hot';
                        else if (p.airTemp >= 10) tempClass = 'temp-warm';

                        var windDirText = self.getWindDirection(p.windDir);
                        var visKm = p.visibility ? (p.visibility / 1000).toFixed(1) + ' km' : 'N/A';

                        var html = '<div class="weather-popup">' +
                            '<h4>' + p.name + '</h4>' +
                            '<div class="weather-grid">' +
                            '<span class="weather-label">Air Temp:</span>' +
                            '<span class="weather-value ' + tempClass + '">' + (p.airTemp != null ? p.airTemp.toFixed(1) + ' C' : 'N/A') + '</span>' +
                            '<span class="weather-label">Surface:</span>' +
                            '<span class="weather-value">' + (p.surfaceTemp != null ? p.surfaceTemp.toFixed(1) + ' C' : 'N/A') + '</span>' +
                            '<span class="weather-label">Humidity:</span>' +
                            '<span class="weather-value">' + (p.humidity != null ? p.humidity.toFixed(0) + '%' : 'N/A') + '</span>' +
                            '<span class="weather-label">Wind:</span>' +
                            '<span class="weather-value">' + (p.windSpeed != null ? p.windSpeed.toFixed(1) + ' m/s ' + windDirText : 'N/A') + '</span>' +
                            '<span class="weather-label">Visibility:</span>' +
                            '<span class="weather-value">' + visKm + '</span>' +
                            '<span class="weather-label">Precip:</span>' +
                            '<span class="weather-value">' + p.precipitation + '</span>' +
                            '</div></div>';

                        new maplibregl.Popup()
                            .setLngLat(e.lngLat)
                            .setHTML(html)
                            .addTo(MapViewer.map);
                    });

                    MapViewer.map.on('mouseenter', 'weather-layer', function() {
                        MapViewer.map.getCanvas().style.cursor = 'pointer';
                    });
                    MapViewer.map.on('mouseleave', 'weather-layer', function() {
                        MapViewer.map.getCanvas().style.cursor = '';
                    });
                    this.handlersAdded = true;
                }
            },

            getWindDirection: function(deg) {
                if (deg == null) return '';
                var dirs = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'];
                var idx = Math.round(deg / 45) % 8;
                return dirs[idx];
            },

            clearWeather: function() {
                if (!MapViewer.map) return;
                if (MapViewer.map.getLayer('weather-layer')) {
                    MapViewer.map.removeLayer('weather-layer');
                }
                if (MapViewer.map.getSource('weather-source')) {
                    MapViewer.map.removeSource('weather-source');
                }
                this.stations = [];
            }
        };

        // Initialize weather viewer
        WeatherViewer.init();

        // ==================== LiDAR Tile Viewer ====================
        var LidarViewer = {
            tiles: null,
            orderId: null,
            enabled: false,
            handlersAdded: false,
            downloadedTiles: {},
            downloading: {},

            init: function() {
                var self = this;
                var toggle = document.getElementById('lidarToggle');
                var statusDiv = document.getElementById('lidarStatus');

                if (!toggle) return;

                toggle.addEventListener('change', function() {
                    self.enabled = this.checked;
                    if (self.enabled) {
                        self.loadTiles();
                    } else {
                        self.removeTiles();
                        if (statusDiv) statusDiv.textContent = '';
                    }
                });
            },

            loadTiles: function() {
                var self = this;
                var statusDiv = document.getElementById('lidarStatus');

                // Find the first LiDAR on-demand order
                fetch('/api/orders')
                    .then(function(r) { return r.json(); })
                    .then(function(orders) {
                        var lidarOrder = orders.find(function(o) {
                            return o.data_type === 'lidar_index';
                        });

                        if (!lidarOrder) {
                            if (statusDiv) statusDiv.textContent = 'No LiDAR orders found';
                            return;
                        }

                        self.orderId = lidarOrder.order_id;
                        if (statusDiv) statusDiv.textContent = 'Loading tiles...';

                        // Load both tiles and downloaded status
                        return Promise.all([
                            fetch('/api/orders/' + self.orderId + '/lidar-tiles.geojson').then(function(r) { return r.json(); }),
                            fetch('/api/orders/' + self.orderId + '/lidar-tiles/downloaded').then(function(r) { return r.json(); })
                        ]);
                    })
                    .then(function(results) {
                        if (!results) return;
                        var geojson = results[0];
                        var downloaded = results[1];

                        // Mark downloaded tiles
                        self.downloadedTiles = {};
                        if (downloaded && downloaded.downloaded) {
                            downloaded.downloaded.forEach(function(name) {
                                self.downloadedTiles[name] = true;
                            });
                        }

                        // Add downloaded property to each feature
                        geojson.features.forEach(function(f) {
                            f.properties.downloaded = self.downloadedTiles[f.properties.filename] ? 1 : 0;
                        });

                        self.tiles = geojson;
                        self.renderTiles();

                        var downloadedCount = Object.keys(self.downloadedTiles).length;
                        if (statusDiv) {
                            statusDiv.textContent = geojson.features.length + ' tiles (' + downloadedCount + ' downloaded)';
                        }
                    })
                    .catch(function(err) {
                        console.error('Failed to load LiDAR tiles:', err);
                        if (statusDiv) statusDiv.textContent = 'Error loading tiles';
                    });
            },

            renderTiles: function() {
                var self = this;
                if (!MapViewer.map || !this.tiles) return;

                var map = MapViewer.map;

                // Remove existing layers first if re-rendering
                if (map.getLayer('lidar-tiles-fill')) {
                    map.removeLayer('lidar-tiles-fill');
                }
                if (map.getLayer('lidar-tiles-outline')) {
                    map.removeLayer('lidar-tiles-outline');
                }
                if (map.getSource('lidar-tiles')) {
                    map.removeSource('lidar-tiles');
                }

                // Add source
                map.addSource('lidar-tiles', {
                    type: 'geojson',
                    data: this.tiles
                });

                // Add fill layer with color based on downloaded status
                map.addLayer({
                    id: 'lidar-tiles-fill',
                    type: 'fill',
                    source: 'lidar-tiles',
                    paint: {
                        'fill-color': [
                            'case',
                            ['==', ['get', 'downloaded'], 1],
                            '#4CAF50',  // Green for downloaded
                            '#E91E63'   // Pink for not downloaded
                        ],
                        'fill-opacity': 0.25
                    }
                });

                // Add outline layer
                map.addLayer({
                    id: 'lidar-tiles-outline',
                    type: 'line',
                    source: 'lidar-tiles',
                    paint: {
                        'line-color': [
                            'case',
                            ['==', ['get', 'downloaded'], 1],
                            '#4CAF50',
                            '#E91E63'
                        ],
                        'line-width': 1.5,
                        'line-opacity': 0.8
                    }
                });

                // Add click handler (only once)
                if (!this.handlersAdded) {
                    map.on('click', 'lidar-tiles-fill', function(e) {
                        if (!self.enabled) return;
                        if (e.features.length === 0) return;

                        var feature = e.features[0];
                        var props = feature.properties;
                        var isDownloaded = self.downloadedTiles[props.filename];
                        var isDownloading = self.downloading[props.filename];

                        var btnHtml;
                        if (isDownloaded) {
                            btnHtml = '<span class="download-btn" style="background: #4CAF50; cursor: default;">Downloaded</span>';
                        } else if (isDownloading) {
                            btnHtml = '<span class="download-btn" style="background: #FF9800; cursor: wait;">Downloading...</span>';
                        } else {
                            btnHtml = '<a class="download-btn" href="#" onclick="LidarViewer.downloadTile(\\'' + props.filename + '\\'); return false;">Download Tile</a>';
                        }

                        var html = '<div class="lidar-popup">' +
                            '<h4>LiDAR Tile</h4>' +
                            '<div class="lidar-grid">' +
                            '<span class="lidar-label">File:</span>' +
                            '<span class="lidar-value">' + props.filename + '</span>' +
                            '<span class="lidar-label">Size:</span>' +
                            '<span class="lidar-value">' + props.size_mb + ' MB</span>' +
                            '<span class="lidar-label">Grid:</span>' +
                            '<span class="lidar-value">' + props.grid_x + ', ' + props.grid_y + ' km</span>' +
                            '<span class="lidar-label">Status:</span>' +
                            '<span class="lidar-value">' + (isDownloaded ? 'Downloaded' : 'Not downloaded') + '</span>' +
                            '</div>' +
                            btnHtml +
                            '</div>';

                        new maplibregl.Popup()
                            .setLngLat(e.lngLat)
                            .setHTML(html)
                            .addTo(map);
                    });

                    // Change cursor on hover
                    map.on('mouseenter', 'lidar-tiles-fill', function() {
                        map.getCanvas().style.cursor = 'pointer';
                    });
                    map.on('mouseleave', 'lidar-tiles-fill', function() {
                        map.getCanvas().style.cursor = '';
                    });

                    this.handlersAdded = true;
                }

                // Fit map to tiles
                if (this.tiles.features.length > 0) {
                    var bounds = new maplibregl.LngLatBounds();
                    this.tiles.features.forEach(function(f) {
                        f.geometry.coordinates[0].forEach(function(coord) {
                            bounds.extend(coord);
                        });
                    });
                    map.fitBounds(bounds, { padding: 50 });
                }
            },

            createProgressBar: function(statusDiv) {
                // Clear existing content safely
                while (statusDiv.firstChild) {
                    statusDiv.removeChild(statusDiv.firstChild);
                }

                // Create progress container
                var progressDiv = document.createElement('div');
                progressDiv.className = 'lidar-progress';

                var progressBar = document.createElement('div');
                progressBar.className = 'lidar-progress-bar';

                var progressFill = document.createElement('div');
                progressFill.className = 'lidar-progress-fill';
                progressFill.id = 'lidarProgressFill';
                progressFill.style.width = '0%';

                var progressText = document.createElement('div');
                progressText.className = 'lidar-progress-text';
                progressText.id = 'lidarProgressText';
                progressText.textContent = 'Starting download...';

                progressBar.appendChild(progressFill);
                progressDiv.appendChild(progressBar);
                progressDiv.appendChild(progressText);
                statusDiv.appendChild(progressDiv);
            },

            downloadTile: function(filename) {
                var self = this;
                if (!this.orderId) return;

                // Check if already downloaded or downloading
                if (this.downloadedTiles[filename]) {
                    alert('This tile is already downloaded');
                    return;
                }
                if (this.downloading[filename]) {
                    alert('This tile is currently downloading');
                    return;
                }

                // Mark as downloading
                this.downloading[filename] = true;
                var statusDiv = document.getElementById('lidarStatus');

                // Create progress bar using safe DOM methods
                if (statusDiv) {
                    this.createProgressBar(statusDiv);
                }

                // Close any open popups
                var popups = document.getElementsByClassName('maplibregl-popup');
                while (popups.length > 0) {
                    popups[0].remove();
                }

                // Use fetch with streaming for SSE progress
                var url = '/api/orders/' + this.orderId + '/lidar-tiles/' + encodeURIComponent(filename) + '/download';

                fetch(url, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'}
                }).then(function(response) {
                    var reader = response.body.getReader();
                    var decoder = new TextDecoder();

                    function read() {
                        reader.read().then(function(result) {
                            if (result.done) return;

                            var text = decoder.decode(result.value);
                            var lines = text.split('\\n');

                            lines.forEach(function(line) {
                                if (line.startsWith('data: ')) {
                                    try {
                                        var data = JSON.parse(line.substring(6));
                                        self.handleDownloadProgress(filename, data);
                                    } catch (e) {
                                        console.error('Failed to parse SSE data:', e);
                                    }
                                }
                            });

                            read();
                        });
                    }
                    read();
                }).catch(function(err) {
                    delete self.downloading[filename];
                    console.error('Failed to download tile:', err);
                    if (statusDiv) statusDiv.textContent = 'Download failed: ' + err.message;
                });
            },

            handleDownloadProgress: function(filename, data) {
                var self = this;
                var statusDiv = document.getElementById('lidarStatus');
                var progressFill = document.getElementById('lidarProgressFill');
                var progressText = document.getElementById('lidarProgressText');

                if (data.status === 'downloading') {
                    if (progressFill) progressFill.style.width = data.percent + '%';
                    if (progressText) {
                        var downloadedMB = (data.downloaded / (1024 * 1024)).toFixed(1);
                        var totalMB = (data.total / (1024 * 1024)).toFixed(1);
                        progressText.textContent = downloadedMB + ' / ' + totalMB + ' MB (' + data.percent + '%)';
                    }
                } else if (data.status === 'downloaded' || data.status === 'already_downloaded') {
                    delete this.downloading[filename];
                    this.downloadedTiles[filename] = true;

                    // Update the feature property
                    if (this.tiles) {
                        this.tiles.features.forEach(function(f) {
                            if (f.properties.filename === filename) {
                                f.properties.downloaded = 1;
                            }
                        });
                        this.renderTiles();
                    }

                    var downloadedCount = Object.keys(this.downloadedTiles).length;
                    if (statusDiv) {
                        statusDiv.textContent = this.tiles.features.length + ' tiles (' + downloadedCount + ' downloaded)';
                    }
                } else if (data.status === 'error') {
                    delete this.downloading[filename];
                    if (statusDiv) statusDiv.textContent = 'Download failed: ' + data.error;
                    alert('Download failed: ' + data.error);
                }
            },

            removeTiles: function() {
                if (!MapViewer.map) return;
                var map = MapViewer.map;

                if (map.getLayer('lidar-tiles-fill')) {
                    map.removeLayer('lidar-tiles-fill');
                }
                if (map.getLayer('lidar-tiles-outline')) {
                    map.removeLayer('lidar-tiles-outline');
                }
                if (map.getSource('lidar-tiles')) {
                    map.removeSource('lidar-tiles');
                }
                this.tiles = null;
            }
        };

        // Initialize LiDAR viewer
        LidarViewer.init();

        // ==================== Geo Chat Assistant ====================
        var GeoChat = {
            apiKey: null,
            context: null,
            messages: [],

            init: function() {
                var self = this;

                var chatToggle = document.getElementById('chatToggle');
                var chatWindow = document.getElementById('chatWindow');
                var chatClose = document.getElementById('chatClose');
                var apiKeyInput = document.getElementById('apiKeyInput');
                var chatApiKey = document.getElementById('chatApiKey');
                var chatSend = document.getElementById('chatSend');
                var chatInput = document.getElementById('chatInput');

                // Check all elements exist
                if (!chatToggle || !chatWindow || !chatClose || !apiKeyInput || !chatApiKey || !chatSend || !chatInput) {
                    console.error('GeoChat: Missing DOM elements');
                    return;
                }

                // Load saved API key
                this.apiKey = localStorage.getItem('claude_api_key');
                if (this.apiKey) {
                    chatApiKey.classList.add('hidden');
                }

                // Event listeners
                chatToggle.addEventListener('click', function() {
                    chatWindow.classList.toggle('open');
                    if (!self.context) self.loadContext();
                });

                chatClose.addEventListener('click', function() {
                    chatWindow.classList.remove('open');
                });

                apiKeyInput.addEventListener('change', function(e) {
                    self.apiKey = e.target.value;
                    localStorage.setItem('claude_api_key', self.apiKey);
                    chatApiKey.classList.add('hidden');
                });

                chatSend.addEventListener('click', function() {
                    self.sendMessage();
                });

                chatInput.addEventListener('keypress', function(e) {
                    if (e.key === 'Enter') self.sendMessage();
                });

                console.log('GeoChat initialized');
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

                var prompt = 'You are a geodata assistant for Swedish Lantmateriet data in PostGIS.\\n';
                prompt += 'Schema: ' + this.context.metadata.schema_name + ', SRID: 3006 (SWEREF99 TM)\\n\\n';

                prompt += 'TABLES:\\n';
                var tableCount = 0;
                for (var table in this.context.schema) {
                    if (tableCount >= 10) {
                        prompt += '... and ' + (Object.keys(this.context.schema).length - 10) + ' more tables\\n';
                        break;
                    }
                    var info = this.context.schema[table];
                    var shortName = table.split('.')[1];
                    prompt += shortName + ' (' + info.row_count + ' rows';
                    if (info.geometry_type) {
                        prompt += ', ' + info.geometry_type;
                    }
                    prompt += '): ';
                    // Only first 8 column names
                    var colNames = info.columns.slice(0, 8).map(function(c) { return c.name; });
                    prompt += colNames.join(', ');
                    if (info.columns.length > 8) prompt += '...';
                    prompt += '\\n';
                    tableCount++;
                }

                prompt += '\\nRULES:\\n';
                prompt += '- Generate exactly ONE valid PostgreSQL/PostGIS SELECT query in a ```sql block\\n';
                prompt += '- Always use full table names: "' + this.context.metadata.schema_name + '"."tablename"\\n';
                prompt += '- Use ST_* functions for spatial queries\\n';
                prompt += '- Keep queries simple - no dynamic SQL, no variables, no comments inside SQL\\n';
                prompt += '- Only ONE query per response - never multiple queries\\n';

                return prompt;
            },

            addMessage: function(role, content) {
                var container = document.getElementById('chatMessages');
                var div = document.createElement('div');
                div.className = 'chat-message ' + role;

                // Format message content safely
                var formatted = this.formatMessage(content);
                div.appendChild(formatted);

                container.appendChild(div);
                container.scrollTop = container.scrollHeight;

                if (role !== 'error') {
                    this.messages.push({ role: role, content: content });
                }
            },

            formatMessage: function(text) {
                // Create a document fragment for safe DOM construction
                var fragment = document.createDocumentFragment();
                var parts = text.split(/(```[\\s\\S]*?```)/g);

                parts.forEach(function(part) {
                    if (part.startsWith('```') && part.endsWith('```')) {
                        // Code block
                        var code = part.slice(3, -3);
                        // Remove language identifier if present
                        var lines = code.split('\\n');
                        if (lines[0] && !lines[0].includes(' ')) {
                            lines.shift();
                        }
                        var pre = document.createElement('pre');
                        var codeEl = document.createElement('code');
                        codeEl.textContent = lines.join('\\n').trim();
                        pre.appendChild(codeEl);
                        fragment.appendChild(pre);
                    } else if (part.trim()) {
                        // Regular text - handle inline code and newlines
                        var textParts = part.split(/(`[^`]+`)/g);
                        textParts.forEach(function(textPart) {
                            if (textPart.startsWith('`') && textPart.endsWith('`')) {
                                var inlineCode = document.createElement('code');
                                inlineCode.textContent = textPart.slice(1, -1);
                                fragment.appendChild(inlineCode);
                            } else if (textPart) {
                                // Handle newlines
                                var lines = textPart.split('\\n');
                                lines.forEach(function(line, index) {
                                    if (line) {
                                        fragment.appendChild(document.createTextNode(line));
                                    }
                                    if (index < lines.length - 1) {
                                        fragment.appendChild(document.createElement('br'));
                                    }
                                });
                            }
                        });
                    }
                });

                return fragment;
            },

            showTyping: function() {
                var container = document.getElementById('chatMessages');
                var div = document.createElement('div');
                div.className = 'chat-message assistant chat-typing';
                div.id = 'typingIndicator';

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

                this.addMessage('user', text);
                input.value = '';

                var sendBtn = document.getElementById('chatSend');
                sendBtn.disabled = true;

                this.showTyping();

                try {
                    var response = await this.callClaude(text);
                    this.hideTyping();

                    var sqlMatch = response.match(/```sql\\n([\\s\\S]*?)```/);
                    if (sqlMatch) {
                        this.addMessage('assistant', response);

                        var sql = sqlMatch[1].trim();
                        var results = await this.executeSQL(sql);

                        if (results.error) {
                            this.addMessage('error', 'SQL Error: ' + results.error);
                        } else {
                            var resultText = this.formatResults(results);
                            this.addMessage('assistant', resultText);

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
                var messages = [];
                var history = this.messages.slice(-10);
                if (history.length > 0) {
                    messages = history.map(function(m) {
                        return { role: m.role === 'user' ? 'user' : 'assistant', content: m.content };
                    });
                }
                messages.push({ role: 'user', content: userMessage });

                var response = await fetch('https://api.anthropic.com/v1/messages', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'x-api-key': this.apiKey,
                        'anthropic-version': '2023-06-01',
                        'anthropic-dangerous-direct-browser-access': 'true'
                    },
                    body: JSON.stringify({
                        model: 'claude-3-5-haiku-20241022',
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

    </script>

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
    <script>
        // Initialize chat after DOM elements exist
        console.log('Initializing GeoChat...');
        console.log('chatToggle element:', document.getElementById('chatToggle'));
        console.log('chatWindow element:', document.getElementById('chatWindow'));
        try {
            GeoChat.init();
            console.log('GeoChat initialized successfully');
        } catch (e) {
            console.error('GeoChat init error:', e);
        }
    </script>
</body>
</html>'''


def run_management_server(
    downloads_dir: Path,
    db_connection: Optional[str] = None,
    schema: str = "geotorget",
    host: str = "127.0.0.1",
    port: int = 5050
):
    """
    Run the management server.

    Args:
        downloads_dir: Directory containing downloaded orders
        db_connection: PostgreSQL connection string
        schema: Schema name for PostGIS tables
        host: Host to bind to
        port: Port to listen on
    """
    app = create_management_app(downloads_dir, db_connection, schema)
    app.run(host=host, port=port, debug=False, threaded=True)
