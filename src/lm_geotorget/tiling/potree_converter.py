"""
Potree conversion for LAZ point cloud files.

Converts LAZ files to Potree octree format for efficient web-based 3D visualization.
Requires PotreeConverter CLI tool to be installed and available in PATH.
"""

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Callable


@dataclass
class ConversionResult:
    """Result of converting a single LAZ tile to Potree format."""
    tile_name: str
    success: bool
    output_dir: Optional[Path] = None
    error: Optional[str] = None


@dataclass
class BatchConversionResult:
    """Result of batch converting multiple LAZ tiles."""
    order_id: str
    total: int
    succeeded: int
    failed: int
    results: list[ConversionResult]


class PotreeConverter:
    """
    Convert LAZ files to Potree octree format.

    Uses PotreeConverter CLI tool for the actual conversion.
    Potree format enables efficient Level-of-Detail rendering of
    massive point clouds in web browsers.
    """

    CONVERTER_NAME = "PotreeConverter"

    @staticmethod
    def is_installed() -> bool:
        """Check if PotreeConverter is available in PATH."""
        return shutil.which(PotreeConverter.CONVERTER_NAME) is not None

    @staticmethod
    def get_version() -> Optional[str]:
        """Get PotreeConverter version string."""
        if not PotreeConverter.is_installed():
            return None
        try:
            result = subprocess.run(
                [PotreeConverter.CONVERTER_NAME, "--version"],
                capture_output=True,
                text=True,
                timeout=10
            )
            return result.stdout.strip() or result.stderr.strip()
        except (subprocess.TimeoutExpired, subprocess.SubprocessError):
            return None

    def convert_tile(
        self,
        laz_path: Path,
        output_dir: Path,
        timeout_seconds: int = 600
    ) -> ConversionResult:
        """
        Convert a single LAZ file to Potree format.

        Args:
            laz_path: Path to the LAZ file
            output_dir: Directory where Potree output will be written
            timeout_seconds: Maximum time for conversion (default 10 minutes)

        Returns:
            ConversionResult with success status and output path
        """
        tile_name = laz_path.stem

        if not laz_path.exists():
            return ConversionResult(
                tile_name=tile_name,
                success=False,
                error=f"LAZ file not found: {laz_path}"
            )

        if not self.is_installed():
            return ConversionResult(
                tile_name=tile_name,
                success=False,
                error="PotreeConverter not installed. Install from: https://github.com/potree/PotreeConverter"
            )

        # Create output directory
        tile_output_dir = output_dir / tile_name
        tile_output_dir.mkdir(parents=True, exist_ok=True)

        try:
            # Run PotreeConverter
            # Command: PotreeConverter <input> -o <output>
            result = subprocess.run(
                [
                    self.CONVERTER_NAME,
                    str(laz_path),
                    "-o", str(tile_output_dir)
                ],
                capture_output=True,
                text=True,
                timeout=timeout_seconds
            )

            if result.returncode != 0:
                return ConversionResult(
                    tile_name=tile_name,
                    success=False,
                    error=f"PotreeConverter failed: {result.stderr or result.stdout}"
                )

            # Verify output was created (check for metadata.json or cloud.js)
            metadata_file = tile_output_dir / "metadata.json"
            cloud_file = tile_output_dir / "cloud.js"

            if not metadata_file.exists() and not cloud_file.exists():
                return ConversionResult(
                    tile_name=tile_name,
                    success=False,
                    error="Conversion completed but output files not found"
                )

            return ConversionResult(
                tile_name=tile_name,
                success=True,
                output_dir=tile_output_dir
            )

        except subprocess.TimeoutExpired:
            return ConversionResult(
                tile_name=tile_name,
                success=False,
                error=f"Conversion timed out after {timeout_seconds} seconds"
            )
        except subprocess.SubprocessError as e:
            return ConversionResult(
                tile_name=tile_name,
                success=False,
                error=f"Subprocess error: {e}"
            )

    def convert_tiles(
        self,
        order_dir: Path,
        tile_names: list[str],
        progress_callback: Optional[Callable[[str, int, int, Optional[str]], None]] = None,
        timeout_per_tile: int = 600
    ) -> BatchConversionResult:
        """
        Convert multiple LAZ tiles to Potree format.

        Args:
            order_dir: Order directory containing tiles/ subdirectory
            tile_names: List of LAZ filenames to convert (e.g., ["tile1.laz", "tile2.laz"])
            progress_callback: Optional callback(status, done, total, current_tile)
            timeout_per_tile: Timeout per tile in seconds

        Returns:
            BatchConversionResult with overall status and per-tile results
        """
        order_id = order_dir.name
        tiles_dir = order_dir / "tiles"
        potree_dir = order_dir / "potree"
        potree_dir.mkdir(parents=True, exist_ok=True)

        results: list[ConversionResult] = []
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
            result = self.convert_tile(laz_path, potree_dir, timeout_per_tile)
            results.append(result)

            if result.success:
                succeeded += 1
            else:
                failed += 1

        # Final progress report
        if progress_callback:
            progress_callback("completed", total, total, None)

        return BatchConversionResult(
            order_id=order_id,
            total=total,
            succeeded=succeeded,
            failed=failed,
            results=results
        )

    @staticmethod
    def get_converted_tiles(order_dir: Path) -> list[dict]:
        """
        List tiles that have been converted to Potree format.

        Args:
            order_dir: Order directory containing potree/ subdirectory

        Returns:
            List of dicts with tile_name, path, and laz_name
        """
        potree_dir = order_dir / "potree"
        if not potree_dir.exists():
            return []

        converted = []
        for tile_dir in potree_dir.iterdir():
            if not tile_dir.is_dir():
                continue

            # Check for Potree output files
            has_metadata = (tile_dir / "metadata.json").exists()
            has_cloud = (tile_dir / "cloud.js").exists()

            if has_metadata or has_cloud:
                converted.append({
                    "tile_name": tile_dir.name,
                    "path": str(tile_dir),
                    "laz_name": f"{tile_dir.name}.laz"
                })

        return converted

    @staticmethod
    def is_tile_converted(order_dir: Path, tile_name: str) -> bool:
        """
        Check if a specific tile has been converted to Potree format.

        Args:
            order_dir: Order directory
            tile_name: Tile name (without .laz extension)

        Returns:
            True if the tile has been converted
        """
        tile_dir = order_dir / "potree" / tile_name
        if not tile_dir.exists():
            return False

        return (tile_dir / "metadata.json").exists() or (tile_dir / "cloud.js").exists()
