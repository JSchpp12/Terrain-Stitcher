import os
from dataclasses import dataclass
from typing import Optional
import json
import warnings
import numpy as np

from PIL import Image as pImage, UnidentifiedImageError

from terrain_stitcher.common import Bounds, World_Coordinates
from terrain_stitcher.common.tile_overlap import FindOverlappingChunks
from terrain_stitcher.common.tile import Tile, TileSide

# Relative touch tolerance. Adapts to USGS vs ArcGIS tile extents.
_TOUCH_TOL_FACTOR = 1e-3

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


# --- ratio-gated downscale resample ---------------------------------------
# Tiles are only ever downscaled (0.0 < scale_factor <= 1.0). The resample
# filter is ratio-gated so we don't trade away sharpness where there is
# nothing to gain:
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
#
# reducing_gap is a PIL parameter that must be >= 1.0 (or None). scale_factor
# is always <= 1.0, so it must NEVER be passed as reducing_gap -- use
# resize_tile_for_scale, which applies the gate and supplies _REDUCING_GAP.
_PROGRESSIVE_REDUCTION_THRESHOLD = 0.25
_REDUCING_GAP = 2.0


def resize_tile(
    src, cell_w, cell_h, reducing_gap: Optional[float] = None
) -> "pImage.Image":
    """Downscale a decoded tile to (cell_w, cell_h) with LANCZOS.

    ``reducing_gap`` is PIL's progressive-reduction gap (must be >= 1.0 or
    None); pass it for large downscale ratios to pre-shrink via area
    averaging before the final LANCZOS pass. Returns a new PIL image; the
    caller owns mode conversion and closing. For scale_factor-driven
    downscale use :func:`resize_tile_for_scale` instead -- it applies the
    ratio gate so ``scale_factor`` (< 1.0) is never misused as
    ``reducing_gap``.
    """

    if reducing_gap is not None:
        return src.resize(
            (cell_w, cell_h), resample=pImage.LANCZOS, reducing_gap=reducing_gap
        )
    return src.resize((cell_w, cell_h), resample=pImage.LANCZOS)


def resize_tile_for_scale(src, cell_w, cell_h, scale_factor):
    """Downscale a decoded tile to (cell_w, cell_h), ratio-gated for quality.

    The single source of truth for the downscale resample strategy, used by
    both the strip path (``_stitch_strip``) and the whole-group path
    (``GatheredTiles.pasteTiles``). Above the threshold a single LANCZOS
    pass is sharpest; at/below it, LANCZOS with ``_REDUCING_GAP`` pre-shrinks
    to avoid moire and is faster. See the block above for the rationale.
    """
    if scale_factor > _PROGRESSIVE_REDUCTION_THRESHOLD:
        return resize_tile(src, cell_w, cell_h)
    return src.resize(
        (cell_w, cell_h), resample=pImage.LANCZOS, reducing_gap=_REDUCING_GAP
    )


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
                    src = resize_tile_for_scale(
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


def _check_touching(grid: dict[tuple[int, int], str], nameToTile: dict) -> None:
    """Warn (not fail) on grid neighbors that don't properly touch/align.

    Enumerates every adjacent pair directly from the sparse grid, so cross-
    group neighbor relationships (which partitioning would cut) are still
    checked. Walking only the present cells and probing their east/south
    neighbors by key lookup keeps this O(tiles) instead of O(span).
    """
    for (r, c), name in grid.items():
        tile = nameToTile[name]
        east_name = grid.get((r, c + 1))
        if east_name is not None:
            _warn_if_not_touching(tile, nameToTile[east_name], TileSide.EAST)
        south_name = grid.get((r + 1, c))
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


def buildTileGrid(nameToBounds: dict) -> dict[tuple[int, int], str]:
    """Place each tile at grid[(row, col)] using bounds centers.

    Rows run north->south (lat desc), cols run west->east (lon asc) so that
    (0, 0) is the NW tile. Returns a SPARSE dict: only present cells have
    entries; absent cells (boundaries, holes) are simply not present, so
    memory scales with the number of tiles, not the (row, col) span.

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

    # Map ascending row index -> grid row (north at row 0).
    grid_row = (rows - 1) - row_idx_asc

    grid: dict[tuple[int, int], str] = {}
    for name, r, c in zip(names, grid_row.tolist(), col_idx.tolist()):
        grid[(r, c)] = name
    return grid


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
