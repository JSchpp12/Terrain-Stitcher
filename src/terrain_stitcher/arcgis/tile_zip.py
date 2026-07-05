from __future__ import annotations

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from terrain_stitcher.arcgis.tile_info import TileInfo


def tile_chunk_name(tile: TileInfo) -> str:
    """Stable, unique, human-readable name for a tile.

    Used as the zip archive's basename (and, later, as the sidecar's
    ``imageFileName`` stem). The hex parts round-trip the original ArcGIS
    ``R``/``C`` encoding because ``row_number``/``col_number`` are parsed
    from hex and reformatted with the same zero padding.
    """
    return f"L{tile.layer_number:02d}_R{tile.row_number:08x}_C{tile.col_number:08x}"


def compress_tile_to_zip(
    tile: TileInfo,
    all_layers_dir: str | Path,
    output_dir: str | Path,
) -> Path:
    """Compress a single tile's PNG into a zip archive in ``output_dir``.

    The archive is named ``<tile_chunk_name>.zip`` and contains the source
    PNG under its original filename. Returns the path to the created zip.
    """
    src = Path(all_layers_dir) / tile.path
    if not src.is_file():
        raise FileNotFoundError(f"Tile source not found: {src}")

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    zip_path = out / f"{tile_chunk_name(tile)}.zip"

    with ZipFile(zip_path, "w", ZIP_DEFLATED) as zf:
        zf.write(src, arcname=src.name)

    return zip_path
