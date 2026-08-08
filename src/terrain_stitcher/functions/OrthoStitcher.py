from terrain_stitcher.stitching.grid_functions import (
    build_tile_grid,
    resize_tile_for_scale,
    _PROGRESSIVE_REDUCTION_THRESHOLD,
    _REDUCING_GAP,
)
from typing import Optional
from PIL import Image as pImage, UnidentifiedImageError
from concurrent.futures import ProcessPoolExecutor, as_completed
from tqdm import tqdm
from terrain_stitcher.stitching import GatheredTiles, ManifestReader
from terrain_stitcher.common import World_Coordinates, get_all_files_in_directory
import json
from terrain_stitcher.functions.ElevationGeoPrep import DEFAULT_PADDING_DEG
from terrain_stitcher.common.bounds import Bounds
from terrain_stitcher.arcgis.acquisition_source import ArcGisProAcquisitionSource
from terrain_stitcher.arcgis.tile_info import TileInfo
from terrain_stitcher.common import Tile, TileSide
import os
import shutil


def _buildGroup(
    positional_tiles: list[tuple[tuple[int, int], Tile]],
    origin: tuple[int, int],
    scale_factor: float = 1.0,
) -> GatheredTiles:
    """Build a GatheredTiles from one window's ((row, col), Tile) entries.

    `positional_tiles` are window-relative (0-based against the window's NW
    cell); `origin` is that NW cell's (row, col) in the global grid. The
    positions are stored verbatim as the group's placement map, so placement
    is by explicit position rather than graph traversal. `root` is the
    NW-most present tile (min (row, col)), whose image supplies the canvas
    mode / cell size.
    """
    by_pos: dict[tuple[int, int], Tile] = {(r, c): t for (r, c), t in positional_tiles}
    tiles = [t for _, t in positional_tiles]
    root = by_pos[min(by_pos)]  # min (r, c) lexicographically == NW-most
    return GatheredTiles(
        root=root,
        tiles=tiles,
        origin=origin,
        positions=by_pos,
        scale_factor=scale_factor,
    )


# The downscale resample strategy (threshold + reducing_gap) and its
# rationale live with resize_tile_for_scale in grid_functions -- one source
# of truth shared by the strip path (_stitch_strip) and the whole-group path
# (GatheredTiles.pasteTiles), so the two can't drift apart (which is what
# made pasteTiles pass scale_factor, always < 1.0, as reducing_gap, which PIL
# requires >= 1.0). _PROGRESSIVE_REDUCTION_THRESHOLD and _REDUCING_GAP are
# re-exported here for _resample_strategy_label and existing callers/tests.


def _resize_tile(src, cell_w, cell_h, scale_factor):
    """Downscale a decoded tile to (cell_w, cell_h), ratio-gated for quality.

    Thin wrapper over :func:`resize_tile_for_scale` (see grid_functions for
    the threshold / reducing_gap rationale). Kept for the strip path and for
    callers/tests that reference it by name.
    """
    return resize_tile_for_scale(src, cell_w, cell_h, scale_factor)


def _resample_strategy_label(scale_factor):
    """Human-readable description of the resample strategy for console output."""
    if scale_factor == 1.0:
        return "none (scale_factor=1.0, no resampling)"
    if scale_factor > _PROGRESSIVE_REDUCTION_THRESHOLD:
        return f"LANCZOS (scale_factor={scale_factor})"
    return (
        f"LANCZOS + progressive reduction (reducing_gap={_REDUCING_GAP}, "
        f"scale_factor={scale_factor} <= {_PROGRESSIVE_REDUCTION_THRESHOLD})"
    )


def _stitch_strip(spec):
    """Build one row-strip of a group's merged image in a worker process.

    Returns a PIL image of size ``strip_size`` in the group's canvas mode, with
    each listed tile decoded, (optionally) ratio-gated-resized to the cell size,
    converted to the canvas mode, and pasted at its window-relative position.
    Absent cells keep the canvas default (black / transparent). ``spec`` is a
    plain tuple so it pickles cheaply for the process pool:
    ``(tiles, strip_size, canvas_mode, scale_factor, cell_w, cell_h)`` where
    ``tiles`` is a list of ``(name, image_path, col, row_in_strip)``.
    """
    tiles, strip_size, canvas_mode, scale_factor, cell_w, cell_h = spec
    strip = pImage.new(canvas_mode, strip_size)
    for name, image_path, col, row_in_strip in tiles:
        try:
            src = pImage.open(image_path)
        except (OSError, UnidentifiedImageError) as exc:
            raise OSError(
                f"could not open tile {name!r} at {image_path!r}: {exc}"
            ) from exc
        with src:
            if scale_factor != 1.0:
                src = _resize_tile(src, cell_w, cell_h, scale_factor)
            src = src.convert(canvas_mode)
            mask = src if "A" in src.getbands() else None
            strip.paste(src, (col * cell_w, row_in_strip * cell_h), mask)
    return strip


def _paste_strip(canvas, strip, r_start, cell_h):
    """Composite a finished row-strip onto the group canvas at its row offset.

    Uses the strip's own alpha as the paste mask when the canvas has alpha, so
    transparent holes in the strip leave the underlying canvas untouched --
    matching pasteTiles' per-tile alpha semantics exactly (strips partition the
    rows, so no two strips own the same pixel).
    """
    mask = strip if "A" in strip.getbands() else None
    canvas.paste(strip, (0, r_start * cell_h), mask)


# --- parallel stitch (within-group row-strips) -----------------------------
# A group is stitched as one or more horizontal row-strips run in a process
# pool, so a single big group (e.g. --dimension 300) fans its decode/resize
# work across every core instead of one. Row-strips partition the canvas (no
# pixel is owned by two strips), so compositing them back with the same
# alpha-mask rule as pasteTiles is pixel-identical to a serial pasteTiles. The
# composite canvas is allocated lazily in the driver (only when a group's
# first strip returns) and freed once saved, so we never hold every group's
# canvas at once.

# Below this many tiles in a group, strip/IPC overhead isn't worth it; run the
# whole group as one pool task instead (canvas allocated in the worker).
_MIN_TILES_FOR_STRIPS = 64
# Above this uncompressed canvas size, run the whole group as one pool task
# (canvas in the worker) rather than strip-booking: each strip of a giant
# canvas is itself giant, and round-tripping it through IPC would dominate
# memory.
_STRIP_CANVAS_BUDGET = 512 * 1024 * 1024

# Approx bytes per pixel per PIL canvas mode, for the budget guard.
_BPP_BY_MODE = {
    "1": 1,
    "L": 1,
    "P": 1,
    "LA": 2,
    "RGB": 3,
    "RGBA": 4,
    "I;16": 2,
    "I": 4,
    "F": 4,
}


def _plan_strips(group, mode, num_workers):
    """Split a group into horizontal row-strip tasks, or return [] to run the
    whole group as one pool task (too few tiles, or canvas too big to want its
    strips round-tripped through IPC). Strips partition the group's rows into
    ``num_workers`` contiguous bands; empty bands are skipped.
    """
    positions = group.get_traversal()
    n = len(positions)
    if n < _MIN_TILES_FOR_STRIPS:
        return []
    rows = max(r for r, _ in positions) + 1
    cols = max(c for _, c in positions) + 1
    cell_w, cell_h = group.cell_width, group.cell_height
    bpp = _BPP_BY_MODE.get(mode, 4)
    if cols * cell_w * rows * cell_h * bpp > _STRIP_CANVAS_BUDGET:
        return []

    k = max(1, min(num_workers, n, rows))
    base, rem = divmod(rows, k)
    strips = []
    r_lo = 0
    for i in range(k):
        band = base + (1 if i < rem else 0)
        r_hi = r_lo + band
        if r_hi > r_lo:
            band_tiles = [
                (tile.name, tile.image_path, c, r - r_lo)
                for (r, c), tile in positions.items()
                if r_lo <= r < r_hi
            ]
            if band_tiles:
                strip_size = (cols * cell_w, band * cell_h)
                spec = (
                    band_tiles,
                    strip_size,
                    mode,
                    group.scale_factor,
                    cell_w,
                    cell_h,
                )
                strips.append((r_lo, spec))
        r_lo = r_hi
    return strips


def _validate_tile_images(nameToTile) -> None:
    """Fail fast if any referenced tile PNG is missing or empty.

    Without this, a single bad tile only surfaces deep inside the threaded
    stitch (one group per worker), potentially hours in, as a bare PIL
    traceback that names neither the tile nor its path. Checking up front turns
    that into an immediate, actionable error listing every bad file before any
    stitching work is spent.
    """
    missing = []
    empty = []
    for name, tile in nameToTile.items():
        path = tile.image_path
        if not os.path.isfile(path):
            missing.append(f"  {name}: {path}")
            continue
        if os.path.getsize(path) == 0:
            empty.append(f"  {name}: {path}")
    problems = []
    if missing:
        problems.append("missing tile image files:")
        problems.extend(missing)
    if empty:
        problems.append("empty tile image files (0 bytes):")
        problems.extend(empty)
    if problems:
        raise FileNotFoundError(
            "tile image validation failed before stitching:\n" + "\n".join(problems)
        )


def partitionGroups(
    grid: dict[tuple[int, int], str],
    nameToTile: dict,
    dimension: int,
    scale_factor: float = 1.0,
) -> list[GatheredTiles]:
    """Walk the sparse grid in N x N windows, emit one GatheredTiles per window.

    ``grid`` is a sparse ``{(row, col): name}`` of only the present cells
    (from buildTileGrid/buildCacheGrid). Each present cell is bucketed into its
    window by floor-dividing its coordinates by ``dimension``; the window origin
    is ``(r // dimension) * dimension, (c // dimension) * dimension`` and the
    tile's window-relative position is the remainder. Empty windows (no
    present cells) never appear, so this is O(tiles) -- not the O(span) the old
    dense double-range walk cost, which visited every cell of the full
    (row, col) extent looking for the handful that weren't None.

    Every non-empty window becomes a single group, whether or not it is a
    complete N x N block. Complete interior windows produce full N x N images;
    partial edge windows (the grid isn't a multiple of N) and windows with
    holes produce smaller/ragged merged images with the absent cells left
    blank. Each present tile is placed at its window-relative (row, col), so no
    tile is ever dropped (isolated "islands" included) and no single-tile
    leftovers are emitted. Windows are emitted in row-major order of their
    origins (sorted), matching the previous dense iteration.
    """
    windows: dict[tuple[int, int], list[tuple[tuple[int, int], str]]] = {}
    for (r, c), name in grid.items():
        origin = ((r // dimension) * dimension, (c // dimension) * dimension)
        windows.setdefault(origin, []).append(((r - origin[0], c - origin[1]), name))

    groups: list[GatheredTiles] = []
    for origin in sorted(windows):
        present = [(pos, nameToTile[name]) for pos, name in windows[origin]]
        groups.append(_buildGroup(present, origin, scale_factor))
    return groups


def _output_stem(origin: tuple[int, int]) -> str:
    """Filename stem (no extension) for a group's merged image, from its origin."""
    return f"gathered_r{origin[0]}_c{origin[1]}"


def _save_canvas(canvas, out_path):
    """Save a finished merged image via temp-then-replace (atomic on volume).

    The temp name has no .png extension, so format="PNG" is passed explicitly
    (PIL would otherwise infer the format from the extension and fail on
    ".tmp").
    """
    tmp_path = out_path + ".tmp"
    canvas.save(tmp_path, format="PNG")
    os.replace(tmp_path, out_path)


def writeManifest(
    output_dir: str,
    groups: list["GatheredTiles"],
    elevation_files: Optional[list] = None,
) -> str:
    """Write height_info.json describing every merged image produced by gather.

    One entry per group: {"name", "bounds"}, where bounds is the group's merged
    envelope (Bounds.toJSON). The schema matches what OrthoPrep emits and what
    readManifest consumes, so the downstream renderer needs no changes. Each
    entry's name is the saved image's stem (name + ".png" exists on disk).

    ``elevation_files``, when provided, is re-emitted so the downstream renderer
    can locate the elevation files (required by the consumer; prep-ortho records
    them in its manifest). Omitted otherwise to match the input.
    Returns the manifest path.
    """
    images = [
        {"name": _output_stem(g.origin), "bounds": g.mergedBounds().toJSON()}
        for g in groups
    ]
    data = {"images": images}
    if elevation_files:
        data["elevation_files"] = list(elevation_files)
    manifest_path = os.path.join(output_dir, "height_info.json")
    with open(manifest_path, "w") as f:
        json.dump(data, f, indent=2)
    return manifest_path


def process_group(
    output_dir: os.PathLike, group: GatheredTiles, resume: bool = False
) -> None:
    out_name = _output_stem(group.origin) + ".png"
    out_path = os.path.join(output_dir, out_name)
    # With --resume, skip groups whose output already exists. process_group
    # writes via an atomic temp-then-replace save (_save_canvas), so the
    # presence of the final file guarantees a complete, valid write from a
    # previous run. Lets an interrupted stitch pick up where it left off
    # instead of re-doing hours of finished work on every invocation.
    if resume and os.path.isfile(out_path):
        return
    image = group.createMergedImage()
    group.pasteTiles(image)
    _save_canvas(image, out_path)


def stitch_groups(
    groups: list["GatheredTiles"],
    nameToTile: dict,
    elevation_files: Optional[list],
    output_dir: str,
    num_workers: int,
    resume: bool = False,
    scale_factor: float = 1.0,
    passthrough_dir: Optional[str] = None,
) -> list["GatheredTiles"]:
    """Composite `groups` into merged PNGs + a height_info.json manifest.

    Shared between the stitch-ortho stage (groups built from a parsed
    prep-ortho manifest) and the folded ArcGIS import path (groups built
    straight from the cache grid). Both feed the same strip/ProcessPool
    composite machinery here, so behaviour -- within-group row-strip
    parallelism, lazy canvas allocation, atomic temp-then-replace saves,
    --resume skip, resample strategy -- is identical.

    `passthrough_dir`: when set (the stitch-ortho case), every non-tile file
    in that directory (elevation TIFs, Shape.json, sidecars, .star_ignore
    markers) is copied verbatim into `output_dir` so the stitched output
    stays self-contained. When `None` (the ArcGIS import case, where the
    input is the multi-million-file cache itself), pass-through is skipped --
    elevation/shape are handled separately by the caller.
    """
    out_abs = os.path.abspath(output_dir)

    pending = []
    skipped = 0
    for gi, group in enumerate(groups):
        out_path = os.path.join(out_abs, _output_stem(group.origin) + ".png")
        if resume and os.path.isfile(out_path):
            skipped += 1
            continue
        mode, _cw, _ch = group.canvas_meta()
        strips = _plan_strips(group, mode, num_workers)
        pending.append((gi, group, out_path, mode, strips))

    print(f"Resample: {_resample_strategy_label(scale_factor)}")
    msg = f"Stitching {len(pending)} group(s) on {num_workers} workers"
    if skipped:
        msg += f" ({skipped} skipped via --resume)"
    print(msg + "...")
    with tqdm(total=len(pending), desc="Stitching groups") as pbar:
        if pending:
            with ProcessPoolExecutor(max_workers=num_workers) as pool:
                futures = {}
                remaining = {}
                meta = {}
                canvas_by_gi = {}
                out_path_by_gi = {}
                for gi, group, out_path, mode, strips in pending:
                    out_path_by_gi[gi] = out_path
                    if strips:
                        positions = group.get_traversal()
                        rows = max(r for r, _ in positions) + 1
                        cols = max(c for _, c in positions) + 1
                        remaining[gi] = len(strips)
                        meta[gi] = (
                            mode,
                            cols,
                            rows,
                            group.cell_width,
                            group.cell_height,
                        )
                        for r_start, spec in strips:
                            fut = pool.submit(_stitch_strip, spec)
                            futures[fut] = ("strip", gi, r_start)
                    else:
                        remaining[gi] = 1
                        fut = pool.submit(process_group, out_abs, group, resume)
                        futures[fut] = ("whole", gi)
                for fut in as_completed(futures):
                    kind = futures[fut][0]
                    if kind == "whole":
                        fut.result()
                        pbar.update(1)
                    else:
                        _, gi, r_start = futures[fut]
                        strip = fut.result()
                        gmode, cols, rows, cw, ch = meta[gi]
                        if gi not in canvas_by_gi:
                            canvas_by_gi[gi] = pImage.new(gmode, (cols * cw, rows * ch))
                        _paste_strip(canvas_by_gi[gi], strip, r_start, ch)
                        remaining[gi] -= 1
                        if remaining[gi] == 0:
                            _save_canvas(canvas_by_gi[gi], out_path_by_gi[gi])
                            del canvas_by_gi[gi]
                            pbar.update(1)

    manifest_path = writeManifest(output_dir, groups, elevation_files)
    print(f"Wrote manifest: {manifest_path} ({len(groups)} image(s))")

    if passthrough_dir is not None:
        tile_image_names = {name + ".png" for name in nameToTile}
        files_in_input = [
            os.path.basename(f) for f in get_all_files_in_directory(passthrough_dir)
        ]
        for input in files_in_input:
            if input == "height_info.json" or input in tile_image_names:
                continue
            shutil.copy2(
                os.path.join(passthrough_dir, input), os.path.join(output_dir, input)
            )

    return groups


def main(
    input_dir: str,
    output_dir: str,
    dimension: int = 1,
    verify_tile_coverage: bool = True,
    scale_factor: float = 1.0,
    resume: bool = False,
    workers: Optional[int] = None,
) -> list[GatheredTiles]:
    if dimension < 1:
        raise ValueError("dimension must be >= 1")
    if not (0.0 < scale_factor <= 1.0):
        raise ValueError(
            "scale_factor must be in (0.0, 1.0]; only downscaling is supported"
        )
    if not os.path.isdir(input_dir):
        raise Exception("Input directory does not exist")
    os.makedirs(output_dir, exist_ok=True)

    grid, nameToTile, elevation_files = build_tile_grid(input_dir, verify_tile_coverage)
    _validate_tile_images(nameToTile)

    print(f"Partitioning into {dimension}x{dimension} groups...")
    groups = partitionGroups(grid, nameToTile, dimension, scale_factor)
    print(
        f"Tile gathering complete: {len(groups)} group(s) from "
        f"{len(nameToTile)} tile(s)"
    )

    num_workers = max(1, workers if workers is not None else (os.cpu_count() or 1))
    return stitch_groups(
        groups,
        nameToTile,
        elevation_files,
        output_dir,
        num_workers,
        resume=resume,
        scale_factor=scale_factor,
        passthrough_dir=input_dir,
    )
