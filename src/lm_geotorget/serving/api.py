"""
FastAPI REST API for serving geodata from PostGIS.

Provides endpoints for:
- Layer discovery and metadata
- Feature queries with spatial filtering
- Order management and publishing
"""

import os
from typing import Optional
from pathlib import Path

try:
    from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False

try:
    import psycopg2
    import psycopg2.extras
    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False


# Pydantic models for API responses
if HAS_FASTAPI:

    class LayerInfo(BaseModel):
        name: str
        geometry_type: str
        srid: int
        feature_count: int
        columns: list[str]

    class LayerDetail(BaseModel):
        name: str
        geometry_type: str
        srid: int
        feature_count: int
        columns: list[dict]
        bbox: Optional[list[float]] = None
        source_order: Optional[str] = None

    class Feature(BaseModel):
        type: str = "Feature"
        id: int
        geometry: dict
        properties: dict

    class FeatureCollection(BaseModel):
        type: str = "FeatureCollection"
        features: list[Feature]
        total_count: Optional[int] = None

    class OrderInfo(BaseModel):
        order_id: str
        data_type: str
        layers: list[str]
        is_publishable: bool
        total_size_mb: float

    class PublishRequest(BaseModel):
        layers: Optional[list[str]] = None

    class PublishResult(BaseModel):
        order_id: str
        success: bool
        layers_processed: list[str]
        total_features: int
        error: Optional[str] = None

    class StatusInfo(BaseModel):
        orders_processed: int
        total_tables: int
        total_features: int
        database_connected: bool


def create_app(
    db_connection: str,
    downloads_dir: Path,
    schema: str = "geotorget"
) -> "FastAPI":
    """
    Create the FastAPI application.

    Args:
        db_connection: PostgreSQL connection string
        downloads_dir: Directory containing downloaded orders
        schema: Schema name for PostGIS tables

    Returns:
        Configured FastAPI app
    """
    if not HAS_FASTAPI:
        raise ImportError(
            "FastAPI is required for the API server. "
            "Install with: pip install fastapi uvicorn"
        )

    if not HAS_PSYCOPG2:
        raise ImportError(
            "psycopg2 is required for the API server. "
            "Install with: pip install psycopg2-binary"
        )

    app = FastAPI(
        title="Geotorget API",
        description="REST API for Swedish geodata from Lantmateriet Geotorget",
        version="0.2.0"
    )

    # CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Store config in app state
    app.state.db_connection = db_connection
    app.state.downloads_dir = Path(downloads_dir)
    app.state.schema = schema

    def get_db():
        """Get database connection."""
        return psycopg2.connect(db_connection)

    # ==================== Health & Status ====================

    @app.get("/health")
    def health_check():
        """Health check endpoint."""
        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
            return {"status": "healthy", "database": "connected"}
        except Exception as e:
            return JSONResponse(
                status_code=503,
                content={"status": "unhealthy", "database": "disconnected", "error": str(e)}
            )

    @app.get("/api/status", response_model=StatusInfo)
    def get_status():
        """Get API and processing status."""
        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    # Count tables
                    cur.execute(f"""
                        SELECT COUNT(DISTINCT f_table_name)
                        FROM geometry_columns
                        WHERE f_table_schema = %s
                    """, (schema,))
                    row = cur.fetchone()
                    table_count = row[0] if row and len(row) > 0 else 0

                    # Count features from metadata
                    cur.execute(f"""
                        SELECT
                            COUNT(DISTINCT order_id),
                            COALESCE(SUM(feature_count), 0)
                        FROM "{schema}"._metadata
                    """)
                    row = cur.fetchone()
                    order_count = row[0] if row and len(row) > 0 else 0
                    feature_count = row[1] if row and len(row) > 1 else 0

            return StatusInfo(
                orders_processed=order_count,
                total_tables=table_count,
                total_features=feature_count,
                database_connected=True
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    # ==================== Layers ====================

    @app.get("/api/layers", response_model=list[LayerInfo])
    def list_layers():
        """List all available layers."""
        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    # Get all geometry tables
                    # Note: %% escapes the % for psycopg2, \\_ escapes underscore for LIKE
                    cur.execute("""
                        SELECT
                            gc.f_table_name,
                            gc.type,
                            gc.srid
                        FROM geometry_columns gc
                        WHERE gc.f_table_schema = %s
                        AND gc.f_table_name NOT LIKE '\\_%%'
                        ORDER BY gc.f_table_name
                    """, (schema,))

                    all_rows = cur.fetchall()
                    layers = []

                    for row in all_rows:
                        if not row or len(row) < 3:
                            continue
                        table_name = row[0]
                        geom_type = row[1] if row[1] else "GEOMETRY"
                        srid = row[2] if row[2] else 0

                        # Get feature count
                        try:
                            cur.execute(f'SELECT COUNT(*) FROM "{schema}"."{table_name}"')
                            count_row = cur.fetchone()
                            count = count_row[0] if count_row else 0
                        except Exception:
                            count = 0

                        # Get columns
                        try:
                            cur.execute(f"""
                                SELECT column_name
                                FROM information_schema.columns
                                WHERE table_schema = %s AND table_name = %s
                                AND column_name NOT IN ('geom', 'fid', '_source_order', '_loaded_at')
                            """, (schema, table_name))
                            columns = [r[0] for r in cur.fetchall() if r]
                        except Exception:
                            columns = []

                        layers.append(LayerInfo(
                            name=table_name,
                            geometry_type=geom_type,
                            srid=srid,
                            feature_count=count,
                            columns=columns
                        ))

                    return layers
        except Exception as e:
            import traceback
            raise HTTPException(status_code=500, detail=f"{str(e)}\n{traceback.format_exc()}")

    @app.get("/api/layers/{layer}", response_model=LayerDetail)
    def get_layer_info(layer: str):
        """Get detailed information about a layer."""
        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    # Get geometry info
                    cur.execute(f"""
                        SELECT type, srid
                        FROM geometry_columns
                        WHERE f_table_schema = %s AND f_table_name = %s
                    """, (schema, layer))
                    row = cur.fetchone()
                    if not row:
                        raise HTTPException(status_code=404, detail=f"Layer not found: {layer}")

                    geom_type, srid = row[0], row[1] if len(row) > 1 else 0

                    # Get count
                    cur.execute(f'SELECT COUNT(*) FROM "{schema}"."{layer}"')
                    count_row = cur.fetchone()
                    count = count_row[0] if count_row and len(count_row) > 0 else 0

                    # Get bbox
                    cur.execute(f"""
                        SELECT
                            ST_XMin(extent), ST_YMin(extent),
                            ST_XMax(extent), ST_YMax(extent)
                        FROM (
                            SELECT ST_Extent(geom) as extent
                            FROM "{schema}"."{layer}"
                        ) t
                    """)
                    bbox_row = cur.fetchone()
                    bbox = list(bbox_row) if bbox_row and len(bbox_row) >= 4 and bbox_row[0] is not None else None

                    # Get columns with types
                    cur.execute(f"""
                        SELECT column_name, data_type
                        FROM information_schema.columns
                        WHERE table_schema = %s AND table_name = %s
                        AND column_name NOT IN ('geom', 'fid')
                        ORDER BY ordinal_position
                    """, (schema, layer))
                    columns = [{"name": r[0], "type": r[1]} for r in cur.fetchall() if r and len(r) >= 2]

                    # Get source order
                    cur.execute(f'SELECT _source_order FROM "{schema}"."{layer}" LIMIT 1')
                    source_row = cur.fetchone()
                    source_order = source_row[0] if source_row and len(source_row) > 0 else None

                    return LayerDetail(
                        name=layer,
                        geometry_type=geom_type,
                        srid=srid,
                        feature_count=count,
                        columns=columns,
                        bbox=bbox,
                        source_order=source_order
                    )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/layers/{layer}/features", response_model=FeatureCollection)
    def query_features(
        layer: str,
        bbox: Optional[str] = Query(None, description="Bounding box: minx,miny,maxx,maxy"),
        limit: int = Query(1000, ge=1, le=10000),
        offset: int = Query(0, ge=0)
    ):
        """
        Query features from a layer.

        Supports bounding box filtering and pagination.
        """
        try:
            with get_db() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    # Verify layer exists
                    cur.execute(f"""
                        SELECT 1 FROM geometry_columns
                        WHERE f_table_schema = %s AND f_table_name = %s
                    """, (schema, layer))
                    if not cur.fetchone():
                        raise HTTPException(status_code=404, detail=f"Layer not found: {layer}")

                    # Get column names (excluding geometry)
                    cur.execute(f"""
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema = %s AND table_name = %s
                        AND column_name NOT IN ('geom')
                        ORDER BY ordinal_position
                    """, (schema, layer))
                    columns = [r["column_name"] for r in cur.fetchall()]
                    col_list = ", ".join(f'"{c}"' for c in columns)

                    # Build WHERE clause
                    where_parts = []
                    params = []

                    if bbox:
                        try:
                            minx, miny, maxx, maxy = map(float, bbox.split(","))
                            where_parts.append(
                                "geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)"
                            )
                            params.extend([minx, miny, maxx, maxy])
                        except ValueError:
                            raise HTTPException(
                                status_code=400,
                                detail="Invalid bbox format. Use: minx,miny,maxx,maxy"
                            )

                    where_clause = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

                    # Get total count
                    count_query = f'SELECT COUNT(*) FROM "{schema}"."{layer}" {where_clause}'
                    cur.execute(count_query, params)
                    total_count = cur.fetchone()["count"]

                    # Get features
                    query = f"""
                        SELECT {col_list}, ST_AsGeoJSON(geom)::json as geometry
                        FROM "{schema}"."{layer}"
                        {where_clause}
                        ORDER BY fid
                        LIMIT %s OFFSET %s
                    """
                    cur.execute(query, params + [limit, offset])

                    features = []
                    for row in cur.fetchall():
                        row_dict = dict(row)
                        geometry = row_dict.pop("geometry")
                        fid = row_dict.get("fid", 0)

                        features.append(Feature(
                            id=fid,
                            geometry=geometry,
                            properties=row_dict
                        ))

                    return FeatureCollection(
                        features=features,
                        total_count=total_count
                    )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/layers/{layer}/features/{fid}", response_model=Feature)
    def get_feature(layer: str, fid: int):
        """Get a single feature by ID."""
        try:
            with get_db() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    # Get column names
                    cur.execute(f"""
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema = %s AND table_name = %s
                        AND column_name NOT IN ('geom')
                    """, (schema, layer))
                    columns = [r["column_name"] for r in cur.fetchall()]
                    col_list = ", ".join(f'"{c}"' for c in columns)

                    cur.execute(f"""
                        SELECT {col_list}, ST_AsGeoJSON(geom)::json as geometry
                        FROM "{schema}"."{layer}"
                        WHERE fid = %s
                    """, (fid,))

                    row = cur.fetchone()
                    if not row:
                        raise HTTPException(status_code=404, detail=f"Feature not found: {fid}")

                    row_dict = dict(row)
                    geometry = row_dict.pop("geometry")

                    return Feature(
                        id=fid,
                        geometry=geometry,
                        properties=row_dict
                    )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    # ==================== Orders ====================

    @app.get("/api/orders", response_model=list[OrderInfo])
    def list_orders():
        """List all downloaded orders."""
        from ..tiling.processor import get_order_info

        orders = []
        downloads_dir = app.state.downloads_dir

        for order_dir in downloads_dir.iterdir():
            if order_dir.is_dir() and not order_dir.name.startswith("."):
                try:
                    info = get_order_info(order_dir)
                    orders.append(OrderInfo(
                        order_id=info["order_id"],
                        data_type=info["data_type_label"],
                        layers=info["layers"],
                        is_publishable=info["is_publishable"],
                        total_size_mb=info["total_size_mb"]
                    ))
                except Exception:
                    continue

        return orders

    @app.post("/api/orders/{order_id}/publish", response_model=PublishResult)
    def publish_order(
        order_id: str,
        request: PublishRequest,
        background_tasks: BackgroundTasks
    ):
        """Publish an order to PostGIS."""
        from ..tiling.processor import DataProcessor

        processor = DataProcessor(
            downloads_dir=app.state.downloads_dir,
            db_connection=app.state.db_connection,
            schema=app.state.schema
        )

        try:
            result = processor.process_order(
                order_id=order_id,
                layers=request.layers
            )

            return PublishResult(
                order_id=order_id,
                success=result.success,
                layers_processed=[r.layer_name for r in result.layers_processed],
                total_features=result.total_features,
                error=result.error
            )
        except Exception as e:
            return PublishResult(
                order_id=order_id,
                success=False,
                layers_processed=[],
                total_features=0,
                error=str(e)
            )
        finally:
            processor.close()

    return app


def run_server(
    db_connection: str,
    downloads_dir: Path,
    schema: str = "geotorget",
    host: str = "0.0.0.0",
    port: int = 8000
):
    """
    Run the API server.

    Args:
        db_connection: PostgreSQL connection string
        downloads_dir: Directory containing downloaded orders
        schema: Schema name for PostGIS tables
        host: Host to bind to
        port: Port to listen on
    """
    try:
        import uvicorn
    except ImportError:
        raise ImportError(
            "uvicorn is required to run the server. "
            "Install with: pip install uvicorn"
        )

    app = create_app(db_connection, downloads_dir, schema)
    uvicorn.run(app, host=host, port=port)
