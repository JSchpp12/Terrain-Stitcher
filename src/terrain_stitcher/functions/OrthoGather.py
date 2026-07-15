from __future__ import annotations

import enum
import os
import json
import shutil
import warnings
from dataclasses import dataclass
from typing import Optional

from PIL import Image as pImage
from concurrent.futures import ThreadPoolExecutor, as_completed

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

    def createMergedImage(self) -> "pImage.Image":
        """Create a blank Pillow image sized to hold every tile in this group.

        The size is (grid_cols * cell_width, grid_rows * cell_height), where the
        grid extents come from traverse() and the cell size is read from the
        root tile's image. prep-ortho guarantees every tile in a group shares
        the same pixel dimensions, so reading one tile is sufficient. The canvas
        mode is inherited from the source tiles so RGB/RGBA is preserved.

        Populates self.cell_width / self.cell_height for the subsequent paste
        step (each tile pastes at (col * cell_width, row * cell_height)).
        """
        positions = self.get_traversal()
        if not positions:
            raise ValueError("cannot create a merged image for an empty group")

        rows = max(r for r, _ in positions) + 1
        cols = max(c for _, c in positions) + 1

        with pImage.open(self.root.image_path) as src:
            mode = src.mode
            self.cell_width, self.cell_height = src.size

        return pImage.new(mode, (cols * self.cell_width, rows * self.cell_height))

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
            with pImage.open(tile.image_path) as src:
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


def _cluster_tol(values: list[float]) -> float:
    """Tolerance = half the minimum spacing between sorted distinct values.

    Adapts to USGS vs ArcGIS tile extents automatically (no hardcoded number).
    """
    if len(values) < 2:
        return 1.0
    ordered = sorted(values)
    diffs = [ordered[i + 1] - ordered[i] for i in range(len(ordered) - 1)]
    return max(min(diffs) * 0.5, 1e-9)


def _cluster(values: list[float], tol: float) -> list[float]:
    """Group sorted values within `tol` into clusters; return their means."""
    if not values:
        return []
    ordered = sorted(values)
    clusters: list[list[float]] = [[ordered[0]]]
    for v in ordered[1:]:
        if v - clusters[-1][-1] <= tol:
            clusters[-1].append(v)
        else:
            clusters.append([v])
    return [sum(c) / len(c) for c in clusters]


def _nearest_index(centers: list[float], value: float) -> int:
    best: Optional[float] = None
    best_idx = 0
    for i, c in enumerate(centers):
        d = abs(c - value)
        if best is None or d < best:
            best, best_idx = d, i
    return best_idx


def buildTileGrid(nameToBounds: dict) -> list[list[Optional[str]]]:
    """Place each tile at grid[row][col] using bounds centers.

    Rows run north->south (lat desc), cols run west->east (lon asc) so that
    grid[0][0] is the NW tile. Empty cells (boundaries, holes) are None.
    """
    names = list(nameToBounds.keys())
    centers = {
        n: (
            nameToBounds[n].getCenter().get_lat(),
            nameToBounds[n].getCenter().get_lon(),
        )
        for n in names
    }

    lats = [centers[n][0] for n in names]
    lons = [centers[n][1] for n in names]

    row_centers = sorted(_cluster(lats, _cluster_tol(lats)), reverse=True)
    col_centers = sorted(_cluster(lons, _cluster_tol(lons)))

    grid: list[list[Optional[str]]] = [[None] * len(col_centers) for _ in row_centers]
    for n in names:
        r = _nearest_index(row_centers, centers[n][0])
        c = _nearest_index(col_centers, centers[n][1])
        grid[r][c] = n
    return grid


def _buildGroup(
    positional_tiles: list[tuple[tuple[int, int], Tile]],
    origin: tuple[int, int],
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
    return GatheredTiles(root=root, tiles=tiles, origin=origin, positions=by_pos)


def partitionGroups(
    grid: list[list[Optional[str]]],
    nameToTile: dict,
    dimension: int,
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
                groups.append(_buildGroup(present, (r0, c0)))

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


def readManifest(input_dir: str) -> dict:
    """Read the prep-ortho manifest (height_info.json) -> {name: Bounds}.

    The manifest lists each tile as {"name", "bounds"}; that is all the graph
    layer needs (no image files are opened in this layer).
    """
    manifestPath = os.path.join(input_dir, "height_info.json")
    if not os.path.isfile(manifestPath):
        raise Exception("height_info.json not found in input directory")

    with open(manifestPath) as f:
        data = json.load(f)

    nameToBounds: dict = {}
    for entry in data.get("images", []):
        name = entry["name"]
        nameToBounds[name] = Bounds.fromDict(entry["bounds"])
    return nameToBounds


def readElevationFiles(input_dir: str) -> Optional[list]:
    """Read the optional ``elevation_files`` field from height_info.json.

    prep-ortho records the list of elevation filenames here; stitch-ortho must
    carry them through to its own manifest so the downstream renderer can
    open the elevation files. Returns None when the field is absent.
    """
    manifestPath = os.path.join(input_dir, "height_info.json")
    if not os.path.isfile(manifestPath):
        return None
    with open(manifestPath) as f:
        data = json.load(f)
    return data.get("elevation_files")


def _output_stem(origin: tuple[int, int]) -> str:
    """Filename stem (no extension) for a group's merged image, from its origin."""
    return f"gathered_r{origin[0]}_c{origin[1]}"


def writeManifest(output_dir: str, groups: list["GatheredTiles"],
                  elevation_files: Optional[list] = None) -> str:
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


def process_group(output_dir: os.PathLike, group: GatheredTiles) -> None:
    image = group.createMergedImage()
    group.pasteTiles(image)
    out_name = _output_stem(group.origin) + ".png"
    image.save(os.path.join(output_dir, out_name))


def main(input_dir: str, output_dir: str, dimension: int = 1) -> None:
    if dimension < 1:
        raise ValueError("dimension must be >= 1")
    if not os.path.isdir(input_dir):
        raise Exception("Input directory does not exist")
    os.makedirs(output_dir, exist_ok=True)

    nameToBounds = readManifest(input_dir)
    if not nameToBounds:
        raise Exception("No images listed in height_info.json")

    # Carry the elevation files reference through to the stitched manifest;
    # the downstream renderer opens them via height_info.json["elevation_files"].
    elevation_files = readElevationFiles(input_dir)

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

    print("Validating tile coverage...")
    _check_touching(grid, nameToTile)  # warn on gaps/misalignment
    _check_overlaps(list(nameToTile.values()), 0.05)  # fail on any area overlap

    print(f"Partitioning into {dimension}x{dimension} groups...")
    groups = partitionGroups(grid, nameToTile, dimension)
    print(
        f"Tile gathering complete: {len(groups)} group(s) from "
        f"{len(nameToTile)} tile(s)"
    )

    out_abs = os.path.abspath(output_dir)
    max_workers = min(8, os.cpu_count() or 1)
    print(f"Stitching {len(groups)} group(s)...")
    with tqdm(total=len(groups), desc="Stitching groups") as pbar:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(process_group, out_abs, group): group
                for group in groups
            }
            for future in as_completed(futures):
                future.result()  # propagate exceptions
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
    files_in_input = [
        os.path.basename(f) for f in get_all_files_in_directory(input_dir)
    ]
    for input in files_in_input:
        if input == "height_info.json":
            continue
        name_no_ext = os.path.splitext(input)[0]
        if name_no_ext not in nameToBounds:
            shutil.copy2(
                os.path.join(input_dir, input), os.path.join(output_dir, input)
            )

    return groups
