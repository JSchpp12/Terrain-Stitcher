from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional

from tqdm import tqdm

from terrain_stitcher.arcgis.cache_xml import ArcGisCacheInfo, LevelOfDetailInfo
from terrain_stitcher.arcgis.tile_bounds import TileBoundsCalculator, TileFootprints
from terrain_stitcher.arcgis.tile_files import discover_row_dirs, gather_tile_files
from terrain_stitcher.arcgis.tile_filter import ShapeTileFilter
from terrain_stitcher.arcgis.tile_info import BoundedTileInfo, TileInfo
from terrain_stitcher.arcgis.tile_zip import process_tile
from terrain_stitcher.common import ParseArea
from terrain_stitcher.sources.acquisition import AcquisitionSource

CONF_XML = "conf.xml"
ALL_LAYERS_DIR = "_alllayers"


# --- worker-process state --------------------------------------------------
# The ProcessPoolExecutor workers are long-lived for the whole acquisition run.
# The cache metadata + projected shape box are sent to each worker ONCE via
# `initializer`/`initargs` and cached in these module globals, so a row task is
# just a directory-path string -- no per-tile or per-row Bounds data crosses the
# process boundary, and the (non-picklable) pyproj Transformer is built inside
# each worker rather than pickled across it.
_WORKER_CACHE: Optional[ArcGisCacheInfo] = None
_WORKER_CALC: Optional[TileBoundsCalculator] = None
_WORKER_FILTER: Optional[ShapeTileFilter] = None
_WORKER_ALL_LAYERS: Optional[str] = None
_WORKER_OUT: Optional[str] = None


def _init_worker(
    cache_info: ArcGisCacheInfo,
    box: Optional[tuple],
    all_layers_dir: str,
    output_dir: str,
) -> None:
    """Per-worker one-time setup, run by the pool's ``initializer``.

    Builds the (non-picklable) pyproj Transformer inside the worker so it is
    never sent across the process boundary; only picklable inputs
    (``cache_info``, the projected shape box, paths) arrive via ``initargs``.
    """
    global _WORKER_CACHE, _WORKER_CALC, _WORKER_FILTER
    global _WORKER_ALL_LAYERS, _WORKER_OUT
    _WORKER_CACHE = cache_info
    _WORKER_CALC = TileBoundsCalculator(cache_info)
    _WORKER_FILTER = (
        ShapeTileFilter.from_box(cache_info, box) if box is not None else None
    )
    _WORKER_ALL_LAYERS = all_layers_dir
    _WORKER_OUT = output_dir


def _process_row_worker(row_dir: str) -> tuple[int, int, int]:
    """Handle one entire cache row inside a worker subprocess.

    Runs the full gather -> parse -> footprint -> filter -> bounds -> zip ->
    sidecar pipeline for one row directory and returns ``(discovered,
    processed, filtered_out)``. Every per-row allocation (tile paths, TileInfo
    list, numpy footprint arrays, BoundedTileInfo list) is local to this call
    and freed on return, so peak memory is bounded by one row's tile count.

    Tiles are zipped/written sequentially within the worker; the parallelism
    comes from many workers each owning a different row. Only the row
    directory path is sent in and three ints come back, so IPC is negligible
    regardless of how many tiles the row contains.
    """
    tile_paths = gather_tile_files(row_dir, _WORKER_CACHE.cache_tile_format)
    if not tile_paths:
        return (0, 0, 0)

    tile_infos: List[TileInfo] = TileInfo.from_paths(tile_paths, _WORKER_ALL_LAYERS)
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

    out : Optional[Path] = None
    if _WORKER_OUT is not None:
        out = Path(_WORKER_OUT)
    else:
        raise Exception("Worker output path was never assigned")
        
    out.mkdir(parents=True, exist_ok=True)
    for bounded in bounded_tiles:
        process_tile(bounded, _WORKER_ALL_LAYERS, out)

    return (discovered, len(bounded_tiles), filtered_out)


class ArcGisProAcquisitionSource(AcquisitionSource):
    """Acquisition source for orthoimagery exported from ArcGISPro.

    The cache layout is the standard Esri exploded tile cache: a directory
    containing ``conf.xml`` plus an ``_alllayers`` tile tree. The instance is
    built from the cache's ``conf.xml`` and exposes the parsed
    :class:`LevelOfDetailInfo` list via ``self.levels``.

    If a shape file is supplied to :meth:`acquire`, only tiles whose footprint
    overlaps the shape region are processed; otherwise every tile in the cache
    is emitted.

    Concurrency model
    ------------------
    Each row directory is handed to a long-lived worker *subprocess* that runs
    the whole row pipeline (discover -> parse -> footprint -> filter ->
    reproject -> zip -> sidecar) end to end. ``num_workers`` such processes
    persist for the entire acquisition run, so each imports PIL/numpy/pyproj
    once and reuses them across every row it owns.

    The cache metadata and the projected shape box are sent to each worker once
    at startup (via the pool ``initializer``); after that a task is just a row
    directory path, and only three ints come back. No per-tile Bounds cross the
    boundary, and the non-picklable pyproj Transformer is built inside the
    worker rather than pickled. The main process simply submits every row up
    front -- each task is a path string, so the executor's queue is just a few
    thousand short strings and needs no backpressure.
    """

    def __init__(self, cache_xml_path: Optional[str] = None) -> None:
        self._cache_info: Optional[ArcGisCacheInfo] = None
        self.levels: list[LevelOfDetailInfo] = []
        if cache_xml_path is not None:
            self._load_from_xml(cache_xml_path)

    def _load_from_xml(self, cache_xml_path: str) -> None:
        self._cache_info = ArcGisCacheInfo.from_xml(cache_xml_path)
        self.levels = list(self._cache_info.levels)

    @property
    def cache_info(self) -> Optional[ArcGisCacheInfo]:
        return self._cache_info

    @classmethod
    def from_cache_dir(cls, cache_dir: str) -> "ArcGisProAcquisitionSource":
        """Construct from a cache directory by reading its ``conf.xml``."""
        xml_path = os.path.join(cache_dir, CONF_XML)
        if not os.path.isfile(xml_path):
            raise FileNotFoundError(
                f"ArcGIS cache directory missing conf.xml: {cache_dir}"
            )
        return cls(xml_path)

    def _build_tile_filter(
        self, shape_file: Optional[str]
    ) -> Optional[ShapeTileFilter]:
        """Build a callable tile filter from a shape file, or None to keep all."""
        if not shape_file:
            return None
        sPath = (
            shape_file
            if os.path.isabs(shape_file)
            else os.path.join(os.getcwd(), shape_file)
        )
        if not os.path.isfile(sPath):
            raise FileNotFoundError(f"Shape file not found: {sPath}")
        region = ParseArea.fromJSONFile(sPath).getTotalRegion()
        return ShapeTileFilter(self.cache_info, region)

    def acquire(
        self,
        shape_file: str,
        output_dir: str,
        input_dir: Optional[str] = None,
        num_workers: Optional[int] = None,
    ) -> None:
        """Process every tile in the cache into zip + sidecar archives.

        ``num_workers`` controls the size of the long-lived subprocess pool;
        each worker owns whole row directories. Defaults to
        ``os.cpu_count()`` -- tune it down on slow disks (where I/O, not
        compression, dominates) or up on fast NVMe with many cores.

        Note on load balancing: one task per row is coarser than per-tile
        dispatch, so a cache whose rows vary wildly in tile count (e.g. many
        levels of detail mixed together) can leave workers idle while a few
        giant rows finish. If that becomes the bottleneck, the natural
        follow-up is to split oversized rows into sub-row tasks -- but that
        reintroduces passing per-tile data, which this design avoids.
        """
        # Ensure cache metadata is loaded from the input directory.
        if self._cache_info is None:
            if input_dir is None:
                raise ValueError(
                    "ArcGISPro acquisition requires an input cache directory (-i/--input)"
                )
            self._load_from_xml(os.path.join(input_dir, CONF_XML))

        all_layers_dir = os.path.join(str(input_dir), ALL_LAYERS_DIR)

        # Build the shape filter in the main process only to extract the
        # projected shape box (a picklable tuple).
        # Each worker rebuilds a transformer-free copy from the box via
        # ShapeTileFilter.from_box.
        tile_filter = self._build_tile_filter(shape_file)
        box = tile_filter.box if tile_filter is not None else None

        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)

        # Enumerate row directories once (cheap: directory listing only). The
        # per-row walk that materialises tile paths happens inside the workers.
        row_dirs = discover_row_dirs(all_layers_dir)
        if not row_dirs:
            print(f"No tile rows found under {all_layers_dir}; nothing to process.")
            return

        print(f"Discovered {len(row_dirs)} row directory(s) across all levels.")

        if num_workers is None:
            num_workers = max(1, (os.cpu_count() or 1))
        if num_workers < 1:
            raise ValueError("num_workers must be >= 1")

        total_discovered = 0
        total_processed = 0
        total_filtered = 0

        # Submit every row up front. Each task is just a path string, so the
        # executor's queue holds strings Workers pull rows and
        # walk the tile files themselves; PNG bytes never cross the boundary.
        pbar = tqdm(total=len(row_dirs), desc="Processing rows", unit="row")
        try:
            with ProcessPoolExecutor(
                max_workers=num_workers,
                initializer=_init_worker,
                initargs=(self.cache_info, box, str(all_layers_dir), str(out_path)),
            ) as executor:
                futures = [
                    executor.submit(_process_row_worker, row_dir)
                    for row_dir in row_dirs
                ]
                # `as_completed` yields rows as they finish; `f.result()`
                # raises on the first row that fails.
                for f in as_completed(futures):
                    discovered, processed, filtered_out = f.result()
                    total_discovered += discovered
                    total_processed += processed
                    total_filtered += filtered_out
                    pbar.update(1)
        finally:
            pbar.close()

        if total_discovered == 0:
            print("No tiles found in the cache; nothing to process.")
            return

        if tile_filter is not None:
            print(
                f"Shape filter removed {total_filtered} tile(s) outside the region; "
                f"{total_processed} tile(s) were processed across "
                f"{len(row_dirs)} row directory(s) using {num_workers} worker(s)."
            )
        else:
            print(
                f"Processed {total_processed} tile(s) across "
                f"{len(row_dirs)} row directory(s) using {num_workers} worker(s)."
            )
        print(f"Done: wrote tile archive(s) + sidecar(s) to {out_path}.")
