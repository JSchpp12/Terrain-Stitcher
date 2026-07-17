from __future__ import annotations

import enum
import os
import json
import shutil
import warnings
from dataclasses import dataclass
from typing import Optional

import numpy as np
from PIL import Image as pImage, UnidentifiedImageError
from concurrent.futures import ProcessPoolExecutor, as_completed

from terrain_stitcher.common import World_Coordinates, get_all_files_in_directory
from terrain_stitcher.common.tile_overlap import FindOverlappingChunks
from terrain_stitcher.sources import Bounds

from tqdm import tqdm


class TileSide(enum.Enum):
    NORTH = "north"
    EAST = "east"
    SOUTH = "south"
    WEST = "west"


@dataclass
class Tile:
    """A single ortho tile and its geographic bounds.

    A tile carries no neighbor/position state of its own; placement within a
    merged image comes from the window-relative positions captured on its
    GatheredTiles group (snapshotted from the grid at partition time).
    """

    name: str
    image_path: str
    bounds: Bounds

    # edge helpers (bounds corners are rectangular for both USGS & ArcGIS tiles)
    def north_lat(self) -> float:
        return self.bounds.coords_northWest.get_lat()

    def south_lat(self) -> float:
        return self.bounds.coords_southEast.get_lat()

    def west_lon(self) -> float:
        return self.bounds.coords_northWest.get_lon()

    def east_lon(self) -> float:
        return self.bounds.coords_northEast.get_lon()


@dataclass
class GatheredTiles:
    """One output image's worth of tiles, placed by explicit positions.

    Each GatheredTiles produces exactly one output image: the tiles of one
    N x N window of the global grid. A complete window has N**2 tiles; a
    partial window (edge band, or a window with holes) has fewer. Every
    present tile is placed at its window-relative (row, col); absent cells
    (holes / out-of-grid) are simply not present and stay blank on the canvas.

    `positions` is the window-relative {(row, col): Tile} mapping captured
    from the grid at partition time (0-based against the window's NW cell).
    `origin` is that NW cell's (row, col) in the global grid, used for naming
    the output image and for computing merged bounds. `root` is the NW-most
    present tile, whose image supplies the canvas mode / cell size.
    """

    root: Tile
    tiles: list[Tile]
    origin: tuple[int, int] = (0, 0)
    cell_width: int = 0
    cell_height: int = 0
    positions: Optional[dict[tuple[int, int], Tile]] = None
    # Per-tile downscale fraction applied during the stitch pass
    # (0.0 < scale_factor <= 1.0; 1.0 = no scaling). See createMergedImage.
    scale_factor: float = 1.0

    def get_traversal(self) -> dict[tuple[int, int], Tile]:
        """Return the window-relative {(row, col): Tile} placement map.

        Positions are captured from the grid when the group is built, so this
        is a direct lookup -- no graph traversal, and every present tile
        (including isolated "islands" surrounded by holes) is included.
        """
        if self.positions is None:
            raise ValueError(
                "group has no positions; build via partitionGroups/_buildGroup"
            )
        return self.positions

    def canvas_meta(self) -> tuple[str, int, int]:
        """Return (mode, cell_width, cell_height) and cache cell size, WITHOUT
        allocating the merged-image canvas.

        Reads only the root tile's header (mode + size) to derive the cell size;
        every tile in a group shares source dimensions (a prep-ortho guarantee),
        so one tile is enough. This lets the stitch planner size and partition
        work and allocate a group's canvas lazily -- only when that group
        actually starts compositing -- instead of allocating every group's
        canvas up front.
        """
        positions = self.get_traversal()
        if not positions:
            raise ValueError("cannot build canvas meta for an empty group")
        with _open_tile_image(self.root) as src:
            mode = src.mode
            self.cell_width, self.cell_height = self._scaled_cell(*src.size)
        return mode, self.cell_width, self.cell_height

    def createMergedImage(self) -> "pImage.Image":
        """Create a blank Pillow image sized to hold every tile in this group.

        Size is (grid_cols * cell_width, grid_rows * cell_height); grid extents
        come from get_traversal() and the cell size/mode from canvas_meta(). The
        canvas mode is inherited from the source tiles so RGB/RGBA is preserved.
        Populates self.cell_width / self.cell_height for the paste step.
        """
        mode, _cw, _ch = self.canvas_meta()
        positions = self.get_traversal()
        rows = max(r for r, _ in positions) + 1
        cols = max(c for _, c in positions) + 1
        return pImage.new(mode, (cols * self.cell_width, rows * self.cell_height))

    def _scaled_cell(self, width: int, height: int) -> tuple[int, int]:
        """Downscale a tile's pixel size by `self.scale_factor`.

        Returns the source size unchanged when `scale_factor` is 1.0 (the
        fast path -- no resample), otherwise the floor of the scaled
        dimensions. Every tile in a group shares the same source dimensions
        (a prep-ortho guarantee), so the root tile's scaled size is the cell
        size every tile is resampled to at paste time.
        """
        if self.scale_factor == 1.0:
            return width, height
        return int(width * self.scale_factor), int(height * self.scale_factor)

    def pasteTiles(self, canvas: "pImage.Image") -> "pImage.Image":
        """Paste every tile in the group onto `canvas` at its traversed position.

        Each tile pastes at (col * cell_width, row * cell_height). Requires
        createMergedImage() to have run first, since it reads the cell size and
        establishes the canvas mode. Tiles are converted to the canvas mode, and
        when a tile carries an alpha channel it is used as its own paste mask so
        transparency is preserved. Returns `canvas` for convenience.
        """
        if self.cell_width == 0 or self.cell_height == 0:
            raise ValueError(
                "cell size unknown; call createMergedImage() before pasteTiles()"
            )

        for (row, col), tile in self.get_traversal().items():
            with _open_tile_image(tile) as src:
                if self.scale_factor != 1.0:
                    src = _resize_tile(
                        src,
                        self.cell_width,
                        self.cell_height,
                        self.scale_factor,
                    )
                src = src.convert(canvas.mode)
                mask = src if "A" in src.getbands() else None
                canvas.paste(
                    src,
                    (col * self.cell_width, row * self.cell_height),
                    mask,
                )
        return canvas

    def mergedBounds(self) -> Bounds:
        """Total area coverage of every stitched tile in this group, as a Bounds.

        The lat/lon ranges are the min/max latitude and longitude across ALL
        four corners (NW, NE, SE, SW) of every tile in the group -- not just the
        named "north"/"south"/"east"/"west" edges. The edge helpers
        (north_lat/south_lat/...) assume each tile's NW corner holds the
        max latitude and its SE corner holds the min, which is true for
        axis-aligned tiles (ArcGIS / Web Mercator) but NOT for tiles whose
        named corners are not the true extrema (e.g. rotated orthophotos).
        Using only those edges produced an envelope smaller than the actual
        stitched coverage, so the downstream GDAL geotransform derived from
        these ranges was wrong on the stitched output.

        Taking the true min/max over every corner guarantees the ranges span
        the full stitched area coverage; for axis-aligned tiles the result is
        identical to the previous edge-only computation. For a leftover 1-tile
        group this collapses to that tile's own envelope. The center is the
        envelope midpoint so it stays consistent with the corners, and every
        value is a float (World_Coordinates parses/validates on construction).
        """
        corners = [
            corner
            for t in self.tiles
            for corner in (
                t.bounds.coords_northEast,
                t.bounds.coords_southEast,
                t.bounds.coords_southWest,
                t.bounds.coords_northWest,
            )
        ]
        north = max(c.get_lat() for c in corners)
        south = min(c.get_lat() for c in corners)
        west = min(c.get_lon() for c in corners)
        east = max(c.get_lon() for c in corners)
        return Bounds(
            coords_northEast=World_Coordinates(north, east),
            coords_southEast=World_Coordinates(south, east),
            coords_southWest=World_Coordinates(south, west),
            coords_northWest=World_Coordinates(north, west),
            coords_center=World_Coordinates((north + south) / 2, (east + west) / 2),
        )


def _cluster_tol(values: np.ndarray) -> float:
    """Tolerance = half the minimum spacing between sorted distinct values.

    Adapts to USGS vs ArcGIS tile extents automatically (no hardcoded number).
    """
    if values.size < 2:
        return 1.0
    diffs = np.diff(np.sort(values))
    return max(float(diffs.min()) * 0.5, 1e-9)


def _cluster(values: np.ndarray, tol: float) -> np.ndarray:
    """Group sorted values within `tol` into clusters; return cluster means.

    Values are sorted ascending and a new cluster starts wherever the gap
    between adjacent values exceeds ``tol``. Because processing is in sorted
    order, comparing a value against the previous value is equivalent to
    comparing against the last member of the running cluster, so this matches
    the original "compare to last member" chaining rule exactly.
    """
    if values.size == 0:
        return np.empty(0, dtype=np.float64)
    a = np.sort(values)
    if a.size == 1:
        return a.copy()
    # Start index of each cluster: the first element, plus every index that
    # immediately follows a gap larger than tol.
    starts = np.concatenate(([0], np.nonzero(np.diff(a) > tol)[0] + 1))
    sums = np.add.reduceat(a, starts)
    counts = np.diff(np.concatenate((starts, [a.size])))
    return sums / counts


def _nearest_index(centers: np.ndarray, values: np.ndarray) -> np.ndarray:
    """Index of the nearest center for each value; ``centers`` must be ascending.

    Vectorized replacement for the per-tile linear scan. Ties break to the
    lower index, matching the original strict ``<`` comparison.
    """
    if centers.size == 0:
        return np.empty(0, dtype=np.intp)
    if centers.size == 1:
        return np.zeros(values.shape, dtype=np.intp)
    i = np.searchsorted(centers, values)
    i = np.clip(i, 1, centers.size - 1)
    left = values - centers[i - 1]
    right = centers[i] - values
    return np.where(left <= right, i - 1, i).astype(np.intp)


def buildTileGrid(nameToBounds: dict) -> list[list[Optional[str]]]:
    """Place each tile at grid[row][col] using bounds centers.

    Rows run north->south (lat desc), cols run west->east (lon asc) so that
    grid[0][0] is the NW tile. Empty cells (boundaries, holes) are None.

    Centers and cluster means are computed with numpy so the per-tile nearest-
    center lookup is a single vectorized ``searchsorted`` (O(n log centers))
    instead of an O(n * centers) Python scan. For directories with millions of
    tiles that is the difference between seconds and hours.
    """
    names = list(nameToBounds.keys())
    if not names:
        return []

    n = len(names)
    lats = np.fromiter(
        (nameToBounds[name].getCenter().get_lat() for name in names),
        dtype=np.float64,
        count=n,
    )
    lons = np.fromiter(
        (nameToBounds[name].getCenter().get_lon() for name in names),
        dtype=np.float64,
        count=n,
    )

    # Cluster near-equal coordinates into row/col centers. Both are produced in
    # ascending order; rows are then reversed so row 0 is the northernmost.
    row_means_asc = _cluster(lats, _cluster_tol(lats))
    col_means_asc = _cluster(lons, _cluster_tol(lons))

    row_idx_asc = _nearest_index(row_means_asc, lats)
    col_idx = _nearest_index(col_means_asc, lons)

    rows = row_means_asc.shape[0]
    cols = col_means_asc.shape[0]

    # Map ascending row index -> grid row (north at row 0).
    grid_row = (rows - 1) - row_idx_asc

    grid: list[list[Optional[str]]] = [[None] * cols for _ in range(rows)]
    for name, r, c in zip(names, grid_row.tolist(), col_idx.tolist()):
        grid[r][c] = name
    return grid


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


def partitionGroups(
    grid: list[list[Optional[str]]],
    nameToTile: dict,
    dimension: int,
    scale_factor: float = 1.0,
) -> list[GatheredTiles]:
    """Walk the grid in N x N windows and emit one GatheredTiles per window.

    Every non-empty window becomes a single group, whether or not it is a
    complete N x N block. Complete interior windows produce full N x N images;
    partial edge windows (the grid isn't a multiple of N) and windows with
    holes produce smaller/ragged merged images with the absent cells left
    blank. Each present tile is placed at its window-relative (row, col),
    captured straight from the grid, so no tile is ever dropped (isolated
    "islands" included) and no single-tile leftovers are emitted.
    """
    rows = len(grid)
    cols = len(grid[0]) if rows else 0
    groups: list[GatheredTiles] = []

    for r0 in range(0, rows, dimension):
        for c0 in range(0, cols, dimension):
            r_end = r0 + dimension
            c_end = c0 + dimension

            present: list[tuple[tuple[int, int], Tile]] = []
            for r in range(r0, min(r_end, rows)):
                for c in range(c0, min(c_end, cols)):
                    name = grid[r][c]
                    if name is not None:
                        present.append(((r - r0, c - c0), nameToTile[name]))

            if present:
                groups.append(_buildGroup(present, (r0, c0), scale_factor))

    return groups


# Relative touch tolerance. Adapts to USGS vs ArcGIS tile extents.
_TOUCH_TOL_FACTOR = 1e-3


def _warn_if_not_touching(a: Tile, b: Tile, side: TileSide) -> None:
    ref_w = abs(a.east_lon() - a.west_lon())
    ref_h = abs(a.north_lat() - a.south_lat())
    tol_w = _TOUCH_TOL_FACTOR * ref_w
    tol_h = _TOUCH_TOL_FACTOR * ref_h

    if side == TileSide.EAST:
        gap = abs(a.east_lon() - b.west_lon())
        if gap > tol_w:
            warnings.warn(
                f'Tiles "{a.name}" and "{b.name}" are east-neighbors but their '
                f"edges do not touch (lon gap {gap:.6g})."
            )
        if (
            abs(a.north_lat() - b.north_lat()) > tol_h
            or abs(a.south_lat() - b.south_lat()) > tol_h
        ):
            warnings.warn(
                f'Tiles "{a.name}" and "{b.name}" (east-neighbors) are '
                f"vertically misaligned; seam may be offset."
            )
    else:  # SOUTH
        gap = abs(a.south_lat() - b.north_lat())
        if gap > tol_h:
            warnings.warn(
                f'Tiles "{a.name}" and "{b.name}" are south-neighbors but their '
                f"edges do not touch (lat gap {gap:.6g})."
            )
        if (
            abs(a.west_lon() - b.west_lon()) > tol_w
            or abs(a.east_lon() - b.east_lon()) > tol_w
        ):
            warnings.warn(
                f'Tiles "{a.name}" and "{b.name}" (south-neighbors) are '
                f"horizontally misaligned; seam may be offset."
            )


def _check_touching(grid: list[list[Optional[str]]], nameToTile: dict) -> None:
    """Warn (not fail) on grid neighbors that don't properly touch/align.

    Enumerates every adjacent pair directly from the grid, so cross-group
    neighbor relationships (which partitioning would cut) are still checked.
    """
    rows = len(grid)
    cols = len(grid[0]) if rows else 0
    for r in range(rows):
        for c in range(cols):
            name = grid[r][c]
            if name is None:
                continue
            tile = nameToTile[name]
            east_name = grid[r][c + 1] if c + 1 < cols else None
            if east_name is not None:
                _warn_if_not_touching(tile, nameToTile[east_name], TileSide.EAST)
            south_name = grid[r + 1][c] if r + 1 < rows else None
            if south_name is not None:
                _warn_if_not_touching(tile, nameToTile[south_name], TileSide.SOUTH)


def _check_overlaps(tiles: list[Tile], threshold: float = 0.05) -> None:
    """Fail if any two tiles overlap by area beyond `threshold`.

    Tile duck-types as the object FindOverlappingChunks expects (it has a
    `.bounds` attribute). Edge-touching grid neighbors produce ~0 intersection
    area and are not flagged, so a clean grid passes.
    """
    pairs = FindOverlappingChunks(tiles, threshold=threshold)
    if not pairs:
        return

    conflicts = []
    for i, j, ratio in pairs:
        conflicts.append(
            f'"{tiles[i].name}" & "{tiles[j].name}" (overlap ratio {ratio:.2f})'
        )
    raise RuntimeError(
        "Aborting stitch: overlapping tile coverage detected. The downstream "
        "renderer draws every tile without overlap detection, so overlapping "
        "coverage would be drawn multiple times. Conflicts: " + "; ".join(conflicts)
    )


class ManifestReader:
    """Load height_info.json once and expose tile bounds + elevation files.

    A single ``json.load`` parses the manifest; ``nameToBounds`` is built from
    the ``"images"`` entries and the heavy parsed dict is released immediately
    afterward, so the resident footprint is just the Bounds objects plus the
    small elevation-files list. Reading bounds and elevation files through one
    instance avoids the previous double-parse of the same (potentially huge)
    file that separate ``readManifest`` + ``readElevationFiles`` calls performed.
    """

    def __init__(self, input_dir: str) -> None:
        self._input_dir = input_dir
        self.nameToBounds: dict = {}
        self.elevation_files: Optional[list] = None
        self._load()

    def _load(self) -> None:
        manifest_path = os.path.join(self._input_dir, "height_info.json")
        if not os.path.isfile(manifest_path):
            raise Exception("height_info.json not found in input directory")

        with open(manifest_path) as f:
            data = json.load(f)

        self.elevation_files = data.get("elevation_files")
        for entry in data.get("images", []):
            name = entry["name"]
            self.nameToBounds[name] = Bounds.fromDict(entry["bounds"])

    def __len__(self) -> int:
        return len(self.nameToBounds)


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


def _output_stem(origin: tuple[int, int]) -> str:
    """Filename stem (no extension) for a group's merged image, from its origin."""
    return f"gathered_r{origin[0]}_c{origin[1]}"


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


def _open_tile_image(tile: Tile) -> "pImage.Image":
    """Open a tile PNG, re-raising with the tile name and path on failure.

    PIL's ``Image.open`` reads ``fp.read(16)`` to identify the format and
    raises a bare ``OSError`` / ``UnidentifiedImageError`` when a tile file is
    missing, empty, or not a recognizable image. That traceback names neither
    the tile nor its path, which makes a single bad tile in a multi-hour
    threaded stitch impossible to diagnose. This wrapper re-raises with the
    tile name and path while preserving the original exception via chaining so
    the full traceback is still available.
    """
    try:
        return pImage.open(tile.image_path)
    except (OSError, UnidentifiedImageError) as exc:
        raise OSError(
            f"could not open tile {tile.name!r} at {tile.image_path!r}: {exc}"
        ) from exc


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


# --- downscale resample strategy ------------------------------------------
# Tiles are only ever downscaled (0.0 < scale_factor <= 1.0). The resample
# filter is ratio-gated so we don't trade away sharpness where there is nothing
# to gain:
#   * Small downscale (scale_factor > _PROGRESSIVE_REDUCTION_THRESHOLD): a
#     single LANCZOS pass. Sharpest available filter, and at small ratios
#     LANCZOS has no large-ratio aliasing to fix, so progressive reduction
#     would only soften the result for no benefit.
#   * Large downscale (scale_factor <= threshold): LANCZOS with reducing_gap,
#     which pre-shrinks via fast area-averaging (box) passes then runs a final
#     LANCZOS pass on the pre-reduced image. This avoids the moire/aliasing a
#     plain LANCZOS introduces at big downscale factors (it undersamples its
#     wide kernel), and is much faster -- the expensive kernel runs on a
#     fraction of the pixels.
_PROGRESSIVE_REDUCTION_THRESHOLD = 0.25
_REDUCING_GAP = 2.0


def _resize_tile(src, cell_w, cell_h, scale_factor):
    """Downscale a decoded tile to (cell_w, cell_h), ratio-gated for quality.

    Returns a new PIL image; the caller owns mode conversion and closing. See
    the strategy block above for the ratio-gate rationale.
    """
    if scale_factor > _PROGRESSIVE_REDUCTION_THRESHOLD:
        return src.resize((cell_w, cell_h), resample=pImage.LANCZOS)
    return src.resize(
        (cell_w, cell_h), resample=pImage.LANCZOS, reducing_gap=_REDUCING_GAP
    )


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


def _save_canvas(canvas, out_path):
    """Save a finished merged image via temp-then-replace (atomic on volume).

    The temp name has no .png extension, so format="PNG" is passed explicitly
    (PIL would otherwise infer the format from the extension and fail on
    ".tmp").
    """
    tmp_path = out_path + ".tmp"
    canvas.save(tmp_path, format="PNG")
    os.replace(tmp_path, out_path)


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


def _paste_strip(canvas, strip, r_start, cell_h):
    """Composite a finished row-strip onto the group canvas at its row offset.

    Uses the strip's own alpha as the paste mask when the canvas has alpha, so
    transparent holes in the strip leave the underlying canvas untouched --
    matching pasteTiles' per-tile alpha semantics exactly (strips partition the
    rows, so no two strips own the same pixel).
    """
    mask = strip if "A" in strip.getbands() else None
    canvas.paste(strip, (0, r_start * cell_h), mask)


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


def build_tile_grid(input_dir, verify_tile_coverage: bool):
    reader = ManifestReader(input_dir)
    nameToBounds = reader.nameToBounds
    if not nameToBounds:
        raise Exception("No images listed in height_info.json")

    # Carry the elevation files reference through to the stitched manifest;
    # the downstream renderer opens them via height_info.json["elevation_files"].
    elevation_files = reader.elevation_files

    print("Building tile grid...")
    grid = buildTileGrid(nameToBounds)
    nameToTile = {
        name: Tile(
            name=name,
            image_path=os.path.join(input_dir, name + ".png"),
            bounds=bounds,
        )
        for name, bounds in nameToBounds.items()
    }

    if verify_tile_coverage:
        print("Validating tile coverage...")
        _check_touching(grid, nameToTile)  # warn on gaps/misalignment
        _check_overlaps(list(nameToTile.values()), 0.05)  # fail on any area overlap

    return grid, nameToTile, elevation_files


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

    out_abs = os.path.abspath(output_dir)
    num_workers = max(1, workers if workers is not None else (os.cpu_count() or 1))

    # Plan each group using only a light root-tile header read (canvas_meta):
    # no group canvas is allocated here. Strip-eligible groups become several
    # row-strip tasks (within-group parallelism, so one big --dimension group
    # uses every core); the rest -- too few tiles, or a canvas too big to
    # strip-book -- become one whole-group task whose canvas is allocated in
    # the worker. A strip group's composite canvas is allocated lazily, only
    # when its first strip returns, and freed once it's saved, so we never hold
    # every group's canvas at once. Use --workers to lower concurrency if a run
    # exhausts memory.
    pending = []  # (gi, group, out_path, mode, strips_or_None)
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
                remaining = {}  # gi -> tasks left for this group
                meta = {}  # gi -> (mode, cols, rows, cell_w, cell_h)
                canvas_by_gi = {}  # gi -> lazily-allocated composite canvas
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
                        fut.result()  # propagate worker exceptions
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

    # Pass through any non-tile files (elevation TIF, Shape.json,
    # .star_ignore markers, per-chunk .json, etc.) so the stitched output
    # directory stays self-contained. The manifest is intentionally NOT
    # copied: writeManifest() just wrote the authoritative stitched
    # manifest (one entry per merged image, named gathered_r*_c*), and
    # the input manifest still carries the original per-tile names --
    # copying it would clobber the stitched manifest with those original
    # names.
    # Only the per-tile ortho PNGs (<tile_name>.png) are consumed by the
    # stitch step and re-emitted as merged images, so they are intentionally
    # not copied. Everything else -- sidecar .json/.txt, Shape.json,
    # .star_ignore markers, and especially the elevation GeoTIFFs -- is passed
    # through verbatim, even when it shares a stem with a tile (e.g. an
    # elevation TIF named after the same grid cell as an ortho tile). Skipping
    # by stem alone would drop those elevation files and leave the stitched
    # manifest pointing at files that never made it to the output directory.
    tile_image_names = {name + ".png" for name in nameToTile}
    files_in_input = [
        os.path.basename(f) for f in get_all_files_in_directory(input_dir)
    ]
    for input in files_in_input:
        if input == "height_info.json" or input in tile_image_names:
            continue
        shutil.copy2(os.path.join(input_dir, input), os.path.join(output_dir, input))

    return groups
