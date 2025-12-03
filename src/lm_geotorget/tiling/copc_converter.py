"""
COPC (Cloud Optimized Point Cloud) conversion for LAZ files.

Converts LAZ files to COPC format for efficient web-based streaming and visualization.
COPC is a LAZ 1.4 file with octree organization, designed for range requests.
Requires pdal CLI tool to be installed and available in PATH.

See: https://copc.io/
"""

import shutil
import subprocess
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Callable


@dataclass
class CopcConversionResult:
    """Result of converting a single LAZ tile to COPC format."""
    tile_name: str
    success: bool
    output_path: Optional[Path] = None
    point_count: Optional[int] = None
    error: Optional[str] = None


@dataclass
class CopcBatchResult:
    """Result of batch converting multiple LAZ tiles to COPC."""
    order_id: str
    total: int
    succeeded: int
    failed: int
    results: list[CopcConversionResult]


class CopcConverter:
    """
    Convert LAZ files to COPC (Cloud Optimized Point Cloud) format.

    Uses pdal CLI tool for the actual conversion.
    COPC format enables efficient streaming and Level-of-Detail rendering
    of massive point clouds in web browsers via copc.js.
    """

    @staticmethod
    def is_pdal_installed() -> bool:
        """Check if pdal is available in PATH."""
        return shutil.which("pdal") is not None

    @staticmethod
    def get_pdal_version() -> Optional[str]:
        """Get pdal version string."""
        if not CopcConverter.is_pdal_installed():
            return None
        try:
            result = subprocess.run(
                ["pdal", "--version"],
                capture_output=True,
                text=True,
                timeout=10
            )
            # Parse version from output like "pdal 2.5.0 (git-version: ...)"
            for line in result.stdout.split('\n'):
                if 'pdal' in line.lower():
                    return line.strip()
            return result.stdout.strip()
        except (subprocess.TimeoutExpired, subprocess.SubprocessError):
            return None

    @staticmethod
    def supports_copc() -> bool:
        """Check if installed pdal version supports COPC writer."""
        if not CopcConverter.is_pdal_installed():
            return False
        try:
            # Check if writers.copc is available
            result = subprocess.run(
                ["pdal", "--drivers"],
                capture_output=True,
                text=True,
                timeout=10
            )
            return "writers.copc" in result.stdout
        except (subprocess.TimeoutExpired, subprocess.SubprocessError):
            return False

    def convert_tile(
        self,
        laz_path: Path,
        output_dir: Path,
        timeout_seconds: int = 600
    ) -> CopcConversionResult:
        """
        Convert a single LAZ file to COPC format.

        Args:
            laz_path: Path to the LAZ file
            output_dir: Directory where COPC output will be written
            timeout_seconds: Maximum time for conversion (default 10 minutes)

        Returns:
            CopcConversionResult with success status and output path
        """
        tile_name = laz_path.stem

        if not laz_path.exists():
            return CopcConversionResult(
                tile_name=tile_name,
                success=False,
                error=f"LAZ file not found: {laz_path}"
            )

        if not self.is_pdal_installed():
            return CopcConversionResult(
                tile_name=tile_name,
                success=False,
                error="pdal not installed. Install with: sudo apt install pdal"
            )

        if not self.supports_copc():
            return CopcConversionResult(
                tile_name=tile_name,
                success=False,
                error="pdal version does not support COPC writer. Upgrade to pdal >= 2.4"
            )

        # Create output directory
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{tile_name}.copc.laz"

        try:
            # Create pdal pipeline JSON
            pipeline = [
                str(laz_path),
                {
                    "type": "writers.copc",
                    "filename": str(output_path)
                }
            ]

            # Run pdal pipeline
            result = subprocess.run(
                ["pdal", "pipeline", "--stdin"],
                input=json.dumps(pipeline),
                capture_output=True,
                text=True,
                timeout=timeout_seconds
            )

            if result.returncode != 0:
                return CopcConversionResult(
                    tile_name=tile_name,
                    success=False,
                    error=f"pdal conversion failed: {result.stderr or result.stdout}"
                )

            # Verify output was created
            if not output_path.exists():
                return CopcConversionResult(
                    tile_name=tile_name,
                    success=False,
                    error="Conversion completed but output file not found"
                )

            # Get point count from the output file
            point_count = self._get_point_count(output_path)

            return CopcConversionResult(
                tile_name=tile_name,
                success=True,
                output_path=output_path,
                point_count=point_count
            )

        except subprocess.TimeoutExpired:
            return CopcConversionResult(
                tile_name=tile_name,
                success=False,
                error=f"Conversion timed out after {timeout_seconds} seconds"
            )
        except subprocess.SubprocessError as e:
            return CopcConversionResult(
                tile_name=tile_name,
                success=False,
                error=f"Subprocess error: {e}"
            )

    def _get_point_count(self, copc_path: Path) -> Optional[int]:
        """Get point count from COPC file using pdal info."""
        try:
            result = subprocess.run(
                ["pdal", "info", "--summary", str(copc_path)],
                capture_output=True,
                text=True,
                timeout=30
            )
            if result.returncode == 0:
                info = json.loads(result.stdout)
                return info.get("summary", {}).get("num_points")
        except (subprocess.SubprocessError, json.JSONDecodeError):
            pass
        return None

    def convert_tiles(
        self,
        order_dir: Path,
        tile_names: list[str],
        progress_callback: Optional[Callable[[str, int, int, Optional[str]], None]] = None,
        timeout_per_tile: int = 600
    ) -> CopcBatchResult:
        """
        Convert multiple LAZ tiles to COPC format.

        Args:
            order_dir: Order directory containing tiles/ subdirectory
            tile_names: List of LAZ filenames to convert (e.g., ["tile1.laz", "tile2.laz"])
            progress_callback: Optional callback(status, done, total, current_tile)
            timeout_per_tile: Timeout per tile in seconds

        Returns:
            CopcBatchResult with overall status and per-tile results
        """
        order_id = order_dir.name
        tiles_dir = order_dir / "tiles"
        copc_dir = order_dir / "copc"
        copc_dir.mkdir(parents=True, exist_ok=True)

        results: list[CopcConversionResult] = []
        succeeded = 0
        failed = 0
        total = len(tile_names)

        for i, tile_name in enumerate(tile_names):
            # Report progress
            if progress_callback:
                progress_callback("converting", i, total, tile_name)

            # Build path to LAZ file
            laz_path = tiles_dir / tile_name

            # Convert
            result = self.convert_tile(laz_path, copc_dir, timeout_per_tile)
            results.append(result)

            if result.success:
                succeeded += 1
            else:
                failed += 1

        # Final progress report
        if progress_callback:
            progress_callback("completed", total, total, None)

        return CopcBatchResult(
            order_id=order_id,
            total=total,
            succeeded=succeeded,
            failed=failed,
            results=results
        )

    @staticmethod
    def get_converted_tiles(order_dir: Path) -> list[dict]:
        """
        List tiles that have been converted to COPC format.

        Args:
            order_dir: Order directory containing copc/ subdirectory

        Returns:
            List of dicts with tile_name, path, laz_name, and size_mb
        """
        copc_dir = order_dir / "copc"
        if not copc_dir.exists():
            return []

        converted = []
        for copc_file in copc_dir.glob("*.copc.laz"):
            # Extract original tile name (remove .copc.laz suffix)
            tile_name = copc_file.name.replace(".copc.laz", "")
            size_bytes = copc_file.stat().st_size

            converted.append({
                "tile_name": tile_name,
                "path": str(copc_file),
                "laz_name": f"{tile_name}.laz",
                "copc_name": copc_file.name,
                "size_mb": round(size_bytes / (1024 * 1024), 2)
            })

        return converted

    @staticmethod
    def is_tile_converted(order_dir: Path, tile_name: str) -> bool:
        """
        Check if a specific tile has been converted to COPC format.

        Args:
            order_dir: Order directory
            tile_name: Tile name (without .laz extension)

        Returns:
            True if the tile has been converted
        """
        # Handle both "tilename" and "tilename.laz" inputs
        if tile_name.endswith(".laz"):
            tile_name = tile_name[:-4]

        copc_path = order_dir / "copc" / f"{tile_name}.copc.laz"
        return copc_path.exists()

    @staticmethod
    def get_copc_path(order_dir: Path, tile_name: str) -> Optional[Path]:
        """
        Get path to COPC file for a tile.

        Args:
            order_dir: Order directory
            tile_name: Tile name (with or without .laz extension)

        Returns:
            Path to COPC file if it exists, None otherwise
        """
        if tile_name.endswith(".laz"):
            tile_name = tile_name[:-4]

        copc_path = order_dir / "copc" / f"{tile_name}.copc.laz"
        return copc_path if copc_path.exists() else None
