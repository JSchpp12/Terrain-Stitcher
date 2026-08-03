from __future__ import annotations

import enum
import os
import gc
import json
import shutil
from dataclasses import dataclass
from typing import Optional

import numpy as np
from PIL import Image as pImage, UnidentifiedImageError
from concurrent.futures import ProcessPoolExecutor, as_completed
from terrain_stitcher.functions.OrthoStitcher import (
    _output_stem,
    _resample_strategy_label,
    _plan_strips,
    _stitch_strip,
    _paste_strip,
    _save_canvas,
    process_group,
)
from terrain_stitcher.common import World_Coordinates, get_all_files_in_directory

from terrain_stitcher.functions.ElevationGeoPrep import DEFAULT_PADDING_DEG
from terrain_stitcher.common.bounds import Bounds
from terrain_stitcher.arcgis.acquisition_source import ArcGisProAcquisitionSource
from terrain_stitcher.arcgis.tile_info import TileInfo
from terrain_stitcher.common import Tile, TileSide
from terrain_stitcher.stitching import GatheredTiles, ManifestReader
from tqdm import tqdm


@dataclass
class StitchedGroup:
    """Lightweight summary of one stitched output image from the ArcGIS import.

    The streaming import path processes groups one at a time and drops each
    GatheredTiles after saving, so it cannot return the full group objects
    (they would re-materialise the whole cache's tile metadata in memory).
    Instead it returns one of these per output image -- just the window origin
    and the tile count -- for callers that want a quick summary. The manifest
    on disk carries the bounds.
    """

    origin: tuple[int, int]
    n_tiles: int


def readManifest(input_dir: str) -> dict:
    """Backward-compatible wrapper returning ``{name: Bounds}`` from one load."""
    return ManifestReader(input_dir).nameToBounds


def readElevationFiles(input_dir: str) -> Optional[list]:
    """Backward-compatible wrapper returning the ``elevation_files`` list.

    Returns ``None`` when the manifest is absent. Building Bounds is wasted
    work for this field, so this stays a light parse rather than delegating to
    :class:`ManifestReader`.
    """
    manifest_path = os.path.join(input_dir, "height_info.json")
    if not os.path.isfile(manifest_path):
        return None
    with open(manifest_path) as f:
        data = json.load(f)
    return data.get("elevation_files")


def _build_arcgis_group(
    origin: tuple[int, int],
    nr: "np.ndarray",
    nc: "np.ndarray",
    min_row: int,
    min_col: int,
    all_layers_dir: str,
    level_folder: str,
    lod: int,
    scale_factor: float,
    path_builder,
) -> "GatheredTiles":
    """Build one window's GatheredTiles from its normalized (row, col) arrays.

    ``nr``/``nc`` are the window's present tiles' NORMALIZED coordinates (i.e.
    original cache row/col with the global NW-most tile subtracted, so they
    start at 0). ``origin`` is the window's normalized NW cell
    ``((nr // dim) * dim, (nc // dim) * dim)``. The window-relative paste
    position is ``nr - origin_r`` / ``nc - origin_c`` (in ``[0, dim)``); the
    cache-native (row, col) for the image_path is recovered as
    ``nr + min_row`` / ``nc + min_col``. Keeping the window-relative position
    small is what keeps each group's canvas ``cols * cell_w`` by
    ``rows * cell_h`` sized to the window (not the full cache coordinate
    span) -- without it a 1-tile group at a high LOD would ask for a canvas
    millions of tiles wide.

    image_path is derived from (row, col) + the level folder (no stored path
    strings) and no per-tile Bounds are built (a group's envelope is computed
    from its row/col extent + cache geometry at manifest time). This is the
    per-group working set of the streaming import: it exists only for the
    window currently being stitched and is dropped immediately after.
    """
    r0n, c0n = origin
    positions: dict[tuple[int, int], Tile] = {}
    tiles: list[Tile] = []
    for r, c in zip(nr.tolist(), nc.tolist()):
        pos = (int(r) - r0n, int(c) - c0n)  # window-relative, in [0, dim)
        actual_row = int(r) + min_row
        actual_col = int(c) + min_col
        name = f"L{lod:02d}_R{actual_row:08x}_C{actual_col:08x}"
        tile = Tile(
            name=name,
            image_path=path_builder(
                all_layers_dir, level_folder, actual_row, actual_col
            ),
            bounds=None,  # geometry-derived at manifest time, not per tile
        )
        positions[pos] = tile
        tiles.append(tile)
    root = positions[min(positions)]  # min (r, c) lexicographically == NW-most
    return GatheredTiles(
        root=root,
        tiles=tiles,
        origin=origin,
        positions=positions,
        scale_factor=scale_factor,
    )


def _stitch_one_group(
    group: "GatheredTiles",
    out_abs: str,
    pool: ProcessPoolExecutor,
    num_workers: int,
    resume: bool,
) -> None:
    """Stitch a single group to its output PNG, using the shared worker pool.

    One group at a time: either fan its row-strips across the pool (small
    enough canvas that strips round-trip through IPC) or run the whole group
    as one pool task with the canvas living inside the worker (giant
    canvas). Either way the driver holds at most this group's tiles + this
    group's canvas (strips) or just this group's tiles (whole-group); once
    the PNG is saved the group is dropped by the caller. Bounding work to
    one group is what keeps the streaming import's peak memory proportional
    to one window's tile count instead of the whole cache's.
    """
    out_path = os.path.join(out_abs, _output_stem(group.origin) + ".png")
    if resume and os.path.isfile(out_path):
        return
    mode, _cw, _ch = group.canvas_meta()
    positions = group.get_traversal()
    rows = max(r for r, _ in positions) + 1
    cols = max(c for _, c in positions) + 1
    strips = _plan_strips(group, mode, num_workers)
    if strips:
        canvas = pImage.new(mode, (cols * group.cell_width, rows * group.cell_height))
        fut_to_r = {
            pool.submit(_stitch_strip, spec): r_start for r_start, spec in strips
        }
        for fut in as_completed(fut_to_r):
            _paste_strip(canvas, fut.result(), fut_to_r[fut], group.cell_height)
        _save_canvas(canvas, out_path)
        del canvas
    else:
        # Whole-group: canvas is allocated and saved inside the worker, never
        # round-tripped. We submit one task and block on it before the next
        # group, so only one giant canvas is live at a time regardless of
        # num_workers (which would otherwise run num_workers multi-GB
        # canvases concurrently and blow the commit limit).
        pool.submit(process_group, out_abs, group, resume).result()


def writeManifestFromEntries(
    output_dir: str,
    entries: list[dict],
    elevation_files: Optional[list] = None,
) -> str:
    """Write height_info.json from precomputed per-image entries.

    Each entry is ``{"name", "bounds"}`` (bounds already a JSON-able dict).
    Used by the streaming ArcGIS import, which computes each group's envelope
    from cache geometry (not from in-memory GatheredTiles) and processes
    groups one at a time, so the manifest can't be built by re-iterating a
    list of groups at the end the way ``writeManifest`` does.
    """
    data = {"images": entries}
    if elevation_files:
        data["elevation_files"] = list(elevation_files)
    manifest_path = os.path.join(output_dir, "height_info.json")
    with open(manifest_path, "w") as f:
        json.dump(data, f, indent=2)
    return manifest_path


# Basename of the single merged elevation GeoTIFF written into the gather-ortho
# output when -e is supplied. Matches prep-geo's default output name.
_ELEVATION_MERGED_NAME = "elevation_merged.tif"


def _merge_elevation_into_output(
    shape_file: str,
    elevation_data_dir: str,
    output_dir: str,
    padding_deg: float,
) -> list:
    """Merge the elevation GeoTIFFs covering ``shape_file`` into one tif in
    ``output_dir`` by reusing the prep-geo merge (clip + composite to EPSG:4326).

    gather-ortho -e produces a single continuous elevation GeoTIFF instead of
    copying the raw tiles. Returns a one-element list holding the merged file's
    basename, for height_info.json["elevation_files"] (the schema/downstream
    consumer are unchanged). Shape.json is copied into the output too, since the
    old copy-elevation path placed it there. ElevationGeoPrep.main is called
    directly, so an empty elevation dir or a region with no intersecting tile
    raises exactly as prep-geo does.
    """
    from terrain_stitcher.functions.ElevationGeoPrep import main as merge_elevation

    merged_path = merge_elevation(
        shape_file,
        elevation_data_dir,
        os.path.join(output_dir, _ELEVATION_MERGED_NAME),
        padding_deg=padding_deg,
    )
    shutil.copy2(shape_file, os.path.join(output_dir, os.path.basename(shape_file)))
    return [os.path.basename(merged_path)]


ALL_LAYERS_DIR = "_alllayers"


def process(
    source,
    shape_file,
    image_dir,
    output_dir,
    dimension,
    scale_factor,
    resume,
    num_workers,
    lod,
    elevation_data_dir=None,
    elevation_padding_deg=0.0,
):

    rows, cols, level_folder, chosen_lod = source.discover_survivors(
        shape_file, image_dir, num_workers=num_workers, lod=lod
    )

    if rows.size == 0:
        print("No tiles survive the shape filter; nothing to stitch.")
        writeManifestFromEntries(output_dir, [], None)
        return []

    print(f"Using LOD {chosen_lod}: {rows.size} tile(s) cover the region")

    # Elevation: merge the GeoTIFFs covering the shape AOI into one continuous
    # GeoTIFF (clipped + composited to EPSG:4326) by reusing the prep-geo merge,
    # instead of copying the raw tiles. The merged file is written into the
    # output and recorded as a one-element elevation_files list so the
    # height_info.json schema and its downstream consumer are unchanged. Fails
    # loudly when no elevation tile intersects the AOI (or the dir is empty),
    # forcing the user to supply coverage. Requires -s/--shape for the clip.
    elevation_files = None
    if elevation_data_dir is not None:
        if not shape_file:
            raise Exception(
                "--elevationDataDir/-e requires --shape/-s so the AOI can be "
                "clipped from the elevation GeoTIFFs."
            )
        elevation_files = _merge_elevation_into_output(
            shape_file, elevation_data_dir, output_dir, elevation_padding_deg
        )

    # Normalize coordinates to 0-based against the NW-most present tile so
    # window origins start at (0, 0) and output filenames stay tidy.
    min_row = int(rows.min())
    min_col = int(cols.min())
    nr = rows - min_row
    nc = cols - min_col

    # Bucket each present tile into its N x N window by floor-dividing its
    # normalized coordinates by `dimension`. Only present cells become
    # windows, so this is O(tiles) -- no dense (row, col) span allocation.
    dim = dimension
    origin_r = (nr // dim) * dim
    origin_c = (nc // dim) * dim
    windows: dict[tuple[int, int], list[int]] = {}
    for i in range(nr.size):
        origin = (int(origin_r[i]), int(origin_c[i]))
        windows.setdefault(origin, []).append(i)

    print(f"Partitioning into {dimension}x{dimension} groups...")
    print(
        f"Tile gathering complete: {len(windows)} group(s) from " f"{rows.size} tile(s)"
    )

    from terrain_stitcher.arcgis.tile_bounds import TileBoundsCalculator

    # Precompute the manifest entry per window from each window's present
    # row/col extent + cache geometry (one 5-point reproject per window), so
    # the manifest does not need any in-memory GatheredTiles.
    calc = TileBoundsCalculator(source.cache_info)
    sorted_origins = sorted(windows)
    entries: list[dict] = []
    for origin in sorted_origins:
        idxs = windows[origin]
        wr = nr[idxs]
        wc = nc[idxs]
        r_lo = int(wr.min()) + min_row
        r_hi = int(wr.max()) + min_row
        c_lo = int(wc.min()) + min_col
        c_hi = int(wc.max()) + min_col
        bounds = calc.window_bounds(chosen_lod, r_lo, c_lo, r_hi, c_hi)
        entries.append({"name": _output_stem(origin), "bounds": bounds.toJSON()})

    # Stream-stitch one group at a time so the driver holds only the current
    # window's tiles + (for strips) its canvas. Whole-group tasks block
    # before the next group is built, capping giant-canvas concurrency at 1.
    out_abs = os.path.abspath(output_dir)
    print(f"Resample: {_resample_strategy_label(scale_factor)}")
    msg = f"Stitching {len(sorted_origins)} group(s) on {num_workers} workers"
    if resume:
        msg += " (--resume)"
    print(msg + "...")
    skipped = 0
    summaries: list[StitchedGroup] = []
    with ProcessPoolExecutor(max_workers=num_workers) as pool:
        pbar = tqdm(sorted_origins, desc="Stitching groups", unit="grp")
        for origin in pbar:
            idxs = windows[origin]
            out_path = os.path.join(out_abs, _output_stem(origin) + ".png")
            if resume and os.path.isfile(out_path):
                skipped += 1
                summaries.append(StitchedGroup(origin=origin, n_tiles=len(idxs)))
                continue
            group = _build_arcgis_group(
                origin,
                nr[idxs],
                nc[idxs],
                min_row,
                min_col,
                image_dir,
                level_folder,
                chosen_lod,
                scale_factor,
                source.tile_path_for,
            )
            _stitch_one_group(group, out_abs, pool, num_workers, resume)
            summaries.append(StitchedGroup(origin=origin, n_tiles=len(idxs)))
            del group
            gc.collect()
        pbar.close()

    manifest_path = writeManifestFromEntries(output_dir, entries, elevation_files)
    done = len(summaries) - skipped
    note = f" ({skipped} skipped via --resume)" if skipped else ""
    print(f"Stitched {done} group(s){note}; wrote {len(entries)} image(s).")
    print(f"Wrote manifest: {manifest_path} ({len(entries)} image(s))")
    return summaries


CONF_XML = "conf.xml"


def import_from_download(
    shape_file: Optional[str],
    download_dir: str,
    min_level: int,
    max_level: int,
    output_dir: str,
    dimension: int = 1,
    scale_factor: float = 1.0,
    resume: bool = False,
    workers: Optional[int] = None,
    elevation_data_dir: Optional[str] = None,
    elevation_padding_deg: float = DEFAULT_PADDING_DEG,
):
    if dimension < 1:
        raise ValueError("dimension must be >= 1")
    if not (0.0 < scale_factor <= 1.0):
        raise ValueError(
            "scale_factor must be in (0.0, 1.0]; only downscaling is supported"
        )
    os.makedirs(output_dir, exist_ok=True)
    num_workers = max(1, workers if workers is not None else (os.cpu_count() or 1))

    source = ArcGisProAcquisitionSource.from_tile_scheme(
        min_level=min_level,
        max_level=max_level,
        extract_function=TileInfo.from_xyz_paths,
    )

    return process(
        source,
        shape_file,
        download_dir,
        output_dir,
        dimension,
        scale_factor,
        resume,
        num_workers,
        elevation_padding_deg=elevation_padding_deg,
        elevation_data_dir=elevation_data_dir,
        lod=min_level,
    )


def import_from_arcgis_dir(
    shape_file: Optional[str],
    cache_dir: str,
    output_dir: str,
    dimension: int = 1,
    scale_factor: float = 1.0,
    resume: bool = False,
    workers: Optional[int] = None,
    elevation_data_dir: Optional[str] = None,
    lod: Optional[int] = None,
    elevation_padding_deg: float = DEFAULT_PADDING_DEG,
) -> list["StitchedGroup"]:
    """Import an ArcGISPro tile cache directly into stitched output.

    Folds the old gather-ortho --source arcgis -> prep-ortho -> stitch-ortho
    chain into one pass. The cache native (row, col) grid partitions the
    surviving tiles into N x N windows, which are composited straight from the
    source PNGs in _alllayers (no per-tile zip / unzip / re-encode
    round-trip). The output -- merged gathered_r*_c*.png images plus a
    height_info.json manifest -- is the same schema the separate
    stitch-ortho stage produced, so the downstream renderer needs no changes.

    `lod` selects the cache level of detail to stitch; when None the highest
    LOD with surviving tiles is used. A cache ships every LOD it was built
    at, so scoping to one LOD is what keeps the (row, col) grid
    non-overlapping (mixing LODs would stack differently-resolutioned tiles
    over the same ground).

    Memory model: this is the streaming path for very large caches. Discovery
    returns the survivors as int64 (row, col) arrays -- no per-tile
    TileInfo/Bounds objects, no WGS84 reprojection -- so the whole-cache index
    is ~8 bytes/tile. Tile paths are derived from (row, col) + the level
    folder and each group's manifest envelope from its row/col extent + cache
    geometry, so no per-tile path strings or Bounds are ever stored. Groups
    are stitched one at a time (one group's tiles + one canvas resident at a
    time) and dropped after saving, so peak memory is proportional to one
    window's tile count rather than the whole cache's. Whole-group tasks
    (giant canvases) run one at a time regardless of ``workers`` so they
    can't collectively exceed the commit limit; ``workers`` still parallelises
    the row-strips of smaller, strip-eligible groups.
    """
    if dimension < 1:
        raise ValueError("dimension must be >= 1")
    if not (0.0 < scale_factor <= 1.0):
        raise ValueError(
            "scale_factor must be in (0.0, 1.0]; only downscaling is supported"
        )
    if not os.path.isdir(cache_dir):
        raise Exception("Cache directory does not exist")
    os.makedirs(output_dir, exist_ok=True)
    num_workers = max(1, workers if workers is not None else (os.cpu_count() or 1))

    source = ArcGisProAcquisitionSource.from_cache_dir(
        cache_dir, extract_function=TileInfo.from_paths
    )
    all_layers_dir = os.path.join(str(cache_dir), ALL_LAYERS_DIR)

    return process(
        source,
        shape_file,
        all_layers_dir,
        output_dir,
        dimension,
        scale_factor,
        resume,
        num_workers,
        lod,
        elevation_data_dir,
        elevation_padding_deg,
    )
