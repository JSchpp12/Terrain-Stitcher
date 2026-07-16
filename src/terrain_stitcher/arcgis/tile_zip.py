from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import partial
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from terrain_stitcher.arcgis.tile_info import BoundedTileInfo, TileInfo
from terrain_stitcher.sources import Bounds, ImageDataWriter

try:
    from tqdm import tqdm as _tqdm
except ImportError:  # pragma: no cover - tqdm is a declared dependency
    _tqdm = None

# I/O-bound zip + json writes; a healthy default pool.
DEFAULT_TILE_WORKERS = 12

# Sidecar files use .json (prep's _findMetadataPath reads both .json and .txt).
SIDECAR_SUFFIX = ".json"


def tile_chunk_name(tile: TileInfo) -> str:
    """Stable, unique, human-readable name for a tile.

    Used as the zip archive's basename and the sidecar's ``imageFileName``
    stem. The hex parts round-trip the original ArcGIS ``R``/``C`` encoding
    because ``row_number``/``col_number`` are parsed from hex and reformatted
    with the same zero padding.
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


def write_tile_sidecar(
    tile: TileInfo,
    bounds: Bounds,
    output_dir: str | Path,
) -> Path:
    """Write the ``ImageDataWriter`` sidecar JSON next to the tile's zip.

    Produces ``<chunk>.json`` containing
    ``{"bounds": {...5 corners...}, "imageFileName": "<chunk>.zip"}``,
    which is the schema the prep phase's ``_findMetadataPath`` reads.
    Returns the path to the created sidecar.
    """
    chunk = tile_chunk_name(tile)
    image_file_name = f"{chunk}.zip"
    sidecar_name = f"{chunk}{SIDECAR_SUFFIX}"
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    ImageDataWriter(bounds, imageFileName=image_file_name).writeFileContents(
        str(out), image_file_name, sidecar_name
    )
    return out / sidecar_name


def process_tile(
    bounded: BoundedTileInfo,
    all_layers_dir: str | Path,
    output_dir: str | Path,
) -> Path:
    """Process one tile: create its zip and its sidecar in ``output_dir``.

    Takes the tile and its bounds bundled as a :class:`BoundedTileInfo`, so
    the parallel dispatch in :func:`process_tiles` submits self-contained
    units of work rather than matching up two parallel lists. Returns the
    path to the sidecar file.
    """
    compress_tile_to_zip(bounded.tile, all_layers_dir, output_dir)
    return write_tile_sidecar(bounded.tile, bounded.bounds, output_dir)


def process_tiles(
    bounded_tiles: list[BoundedTileInfo],
    all_layers_dir: str | Path,
    output_dir: str | Path,
    num_workers: int = DEFAULT_TILE_WORKERS,
    show_progress: bool | None = None,
) -> list[Path]:
    """Dispatch a threadpool over the ``(tile_info, bounds)`` pairs.

    Each :class:`BoundedTileInfo` is turned into a ``<chunk>.zip`` plus a
    ``<chunk>.json`` sidecar in ``output_dir``. Returns the list of sidecar
    paths in input order.

    A tqdm progress bar is shown when ``show_progress`` is True. When it is
    ``None`` (the default), the bar is shown only if stderr is a TTY, so it
    stays quiet under pytest / redirected output.
    """
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    if show_progress is None:
        show_progress = sys.stderr.isatty()

    fn = partial(process_tile, all_layers_dir=all_layers_dir, output_dir=output_dir)
    results: list = [None] * len(bounded_tiles)

    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        future_to_index = {
            executor.submit(fn, bt): i for i, bt in enumerate(bounded_tiles)
        }
        iterator = as_completed(future_to_index)
        if show_progress and _tqdm is not None:
            iterator = _tqdm(
                iterator,
                total=len(bounded_tiles),
                desc="Processing tiles",
                unit="tile",
            )
        for future in iterator:
            results[future_to_index[future]] = future.result()

    return results
