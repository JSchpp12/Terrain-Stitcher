from typing import List, Optional
from collections.abc import Callable
from pathlib import Path
from terrain_stitcher.arcgis.tile_bounds import TileBoundsCalculator, TileFootprints
from terrain_stitcher.arcgis.tile_filter import ShapeTileFilter
from terrain_stitcher.arcgis.tile_info import TileInfo
from terrain_stitcher.arcgis.bounded_tile_info import BoundedTileInfo
from terrain_stitcher.arcgis.tile_files import gather_tile_files
from terrain_stitcher.arcgis.tile_zip import process_tile
from terrain_stitcher.arcgis.tile_scheme import TileSchemeInfo
import numpy as np

# --- worker-process state --------------------------------------------------
# The ProcessPoolExecutor workers are long-lived for the whole acquisition run.
# The cache metadata + projected shape box are sent to each worker ONCE via
# `initializer`/`initargs` and cached in these module globals, so a row task is
# just a directory-path string -- no per-tile or per-row Bounds data crosses the
# process boundary, and the (non-picklable) pyproj Transformer is built inside
# each worker rather than pickled across it.
_WORKER_CACHE: Optional[TileSchemeInfo] = None
_WORKER_CALC: Optional[TileBoundsCalculator] = None
_WORKER_FILTER: Optional[ShapeTileFilter] = None
_WORKER_ALL_LAYERS: Optional[str] = None
_WORKER_OUT: Optional[str] = None
_WORKER_EXTRACT_LAT_LON_FROM_PATH: Optional[Callable] = None


def _init_worker(
    cache_info: TileSchemeInfo,
    box: Optional[tuple],
    all_layers_dir: str,
    output_dir: str,
    extract_lat_lon_from_path_function: Callable,
) -> None:
    """Per-worker one-time setup, run by the pool's ``initializer``.

    Builds the (non-picklable) pyproj Transformer inside the worker so it is
    never sent across the process boundary; only picklable inputs
    (``cache_info``, the projected shape box, paths) arrive via ``initargs``.
    """
    global _WORKER_CACHE, _WORKER_CALC, _WORKER_FILTER
    global _WORKER_ALL_LAYERS, _WORKER_OUT, _WORKER_EXTRACT_LAT_LON_FROM_PATH

    _WORKER_CACHE = cache_info
    _WORKER_CALC = TileBoundsCalculator(cache_info)
    _WORKER_FILTER = (
        ShapeTileFilter.from_box(cache_info, box) if box is not None else None
    )
    _WORKER_ALL_LAYERS = all_layers_dir
    _WORKER_OUT = output_dir
    _WORKER_EXTRACT_LAT_LON_FROM_PATH = extract_lat_lon_from_path_function


def _process_tiles(tile_paths: list[str]) -> tuple[int, int, int]:
    tile_infos: List[TileInfo] = _WORKER_EXTRACT_LAT_LON_FROM_PATH(
        tile_paths, _WORKER_ALL_LAYERS
    )
    discovered = len(tile_infos)
    footprints = None
    if _WORKER_CALC is not None:
        footprints = _WORKER_CALC.projected_footprints(tile_infos)
    else:
        raise Exception("Worker calculation for bounding box was never assigned")

    filtered_out = 0
    if _WORKER_FILTER is not None:
        keep = _WORKER_FILTER.mask(footprints)
        before = len(tile_infos)
        tile_infos = [t for t, k in zip(tile_infos, keep) if k]
        footprints = TileFootprints(
            west_x=footprints.west_x[keep],
            east_x=footprints.east_x[keep],
            south_y=footprints.south_y[keep],
            north_y=footprints.north_y[keep],
        )
        filtered_out = before - len(tile_infos)

    if not tile_infos:
        return (discovered, 0, filtered_out)

    bounded_tiles: List[BoundedTileInfo] = _WORKER_CALC.bounds_for_all(
        tile_infos, footprints=footprints
    )

    out: Optional[Path] = None
    if _WORKER_OUT is not None:
        out = Path(_WORKER_OUT)
    else:
        raise Exception("Worker output path was never assigned")

    out.mkdir(parents=True, exist_ok=True)
    for bounded in bounded_tiles:
        process_tile(bounded, _WORKER_ALL_LAYERS, out)

    return (discovered, len(bounded_tiles), filtered_out)


def _discover_row_survivors(row_dir: str) -> tuple[str, np.ndarray, np.ndarray]:
    """Discover surviving tile coordinates for one row dir, no bounds work.

    Mirrors ``_process_row_worker``'s gather -> footprint -> shape-filter
    pipeline but stops at the filter and returns ONLY the surviving (row,
    col) integer coordinates as two int64 numpy arrays -- plus the level
    folder name (e.g. ``L22``). No :class:`TileInfo` / :class:`Bounds` objects
    cross the process boundary and no WGS84 reprojection runs here.

    Returning bare coordinates (not TileInfo wrappers) is what keeps the
    driver's resident footprint small enough to stitch caches with tens of
    millions of tiles: the survivors of the whole cache become one (N, 2)
    int64 array (~8 bytes/tile) instead of one wrapper object per tile
    (~hundreds of bytes/tile) plus a full WGS84 Bounds set. The tile's
    image_path and WGS84 bounds are both derivable later from (lod, row,
    col) + cache geometry, so they need not be stored per tile up front.
    """
    level_folder = Path(row_dir).parent.name
    tile_paths = gather_tile_files(row_dir, _WORKER_CACHE.cache_tile_format)
    if not tile_paths:
        return level_folder, np.empty(0, dtype=np.int64), np.empty(0, dtype=np.int64)
    tile_infos: List[TileInfo] = _WORKER_EXTRACT_LAT_LON_FROM_PATH(
        tile_paths, _WORKER_ALL_LAYERS
    )
    if _WORKER_CALC is None:
        raise Exception("Worker calculation for bounding box was never assigned")
    footprints = _WORKER_CALC.projected_footprints(tile_infos)
    if _WORKER_FILTER is not None:
        keep = _WORKER_FILTER.mask(footprints)
        tile_infos = [t for t, k in zip(tile_infos, keep) if k]
    if not tile_infos:
        return level_folder, np.empty(0, dtype=np.int64), np.empty(0, dtype=np.int64)
    rows = np.fromiter(
        (t.row_number for t in tile_infos), dtype=np.int64, count=len(tile_infos)
    )
    cols = np.fromiter(
        (t.col_number for t in tile_infos), dtype=np.int64, count=len(tile_infos)
    )
    return level_folder, rows, cols
