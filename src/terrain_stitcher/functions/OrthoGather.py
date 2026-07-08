from __future__ import annotations

import enum
import os
import json
import warnings
from dataclasses import dataclass
from typing import Optional

from PIL import Image as pImage

from terrain_stitcher.common.tile_overlap import FindOverlappingChunks
from terrain_stitcher.sources import Bounds


# --- direction helpers ------------------------------------------------------

class TileSide(enum.Enum):
    NORTH = "north"
    EAST = "east"
    SOUTH = "south"
    WEST = "west"


# (drow, dcol) used when traversing a group's tree from its root.
_SIDE_OFFSET = {
    TileSide.NORTH: (-1, 0),
    TileSide.EAST: (0, 1),
    TileSide.SOUTH: (1, 0),
    TileSide.WEST: (0, -1),
}

_OPPOSITE = {
    TileSide.NORTH: TileSide.SOUTH,
    TileSide.SOUTH: TileSide.NORTH,
    TileSide.EAST: TileSide.WEST,
    TileSide.WEST: TileSide.EAST,
}


# --- data structures --------------------------------------------------------

@dataclass
class Tile:
    """A single ortho tile and its links to neighbors within its group.

    Links are nullable: a tile on the edge of a group, or next to a hole,
    has None on that side. Links only ever connect tiles inside the same
    GatheredTiles group.
    """
    name: str
    image_path: str
    bounds: Bounds
    north: Optional["Tile"] = None
    east: Optional["Tile"] = None
    south: Optional["Tile"] = None
    west: Optional["Tile"] = None

    def neighbor(self, side: TileSide) -> Optional["Tile"]:
        return getattr(self, side.value)

    def link(self, side: TileSide, other: "Tile") -> None:
        """Set the link on this side and the opposite link on the neighbor."""
        setattr(self, side.value, other)
        setattr(other, _OPPOSITE[side].value, self)

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
    """One output image's worth of tiles (1..N**2), arranged as a tree.

    Each GatheredTiles produces exactly one output image:
      - a complete N x N block -> N**2 tiles linked into a tree
      - a leftover tile from an incomplete block -> a 1-tile group (root only)

    `origin` is the (row, col) of the block's NW cell in the global grid,
    used for naming the output image and for computing merged bounds.
    """
    root: Tile
    tiles: list[Tile]
    origin: tuple[int, int] = (0, 0)
    cell_width: int = 0
    cell_height: int = 0

    def traverse(self) -> dict[tuple[int, int], Tile]:
        """BFS from root, assigning each tile a relative (row, col) offset.

        Back-links (neighbor.<opposite> == self) are handled via a visited set.
        Positions are normalized to a 0-based grid. This is what the paste
        step uses to lay tiles out in the combined image.
        """
        positions: dict[int, tuple[int, int]] = {id(self.root): (0, 0)}
        result: dict[tuple[int, int], Tile] = {(0, 0): self.root}
        queue: list[Tile] = [self.root]

        while queue:
            cur = queue.pop(0)
            cr, cc = positions[id(cur)]
            for side in TileSide:
                nb = cur.neighbor(side)
                if nb is None or id(nb) in positions:
                    continue
                dr, dc = _SIDE_OFFSET[side]
                pos = (cr + dr, cc + dc)
                positions[id(nb)] = pos
                result[pos] = nb
                queue.append(nb)

        min_r = min(r for r, _ in result)
        min_c = min(c for _, c in result)
        return {(r - min_r, c - min_c): t for (r, c), t in result.items()}

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
        positions = self.traverse()
        if not positions:
            raise ValueError("cannot create a merged image for an empty group")

        rows = max(r for r, _ in positions) + 1
        cols = max(c for _, c in positions) + 1

        with pImage.open(self.root.image_path) as src:
            mode = src.mode
            self.cell_width, self.cell_height = src.size

        return pImage.new(
            mode, (cols * self.cell_width, rows * self.cell_height)
        )

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

        for (row, col), tile in self.traverse().items():
            with pImage.open(tile.image_path) as src:
                src = src.convert(canvas.mode)
                mask = src if "A" in src.getbands() else None
                canvas.paste(
                    src,
                    (col * self.cell_width, row * self.cell_height),
                    mask,
                )
        return canvas


# --- grid reconstruction from centers ---------------------------------------

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
        n: (nameToBounds[n].getCenter().get_lat(),
            nameToBounds[n].getCenter().get_lon())
        for n in names
    }

    lats = [centers[n][0] for n in names]
    lons = [centers[n][1] for n in names]

    row_centers = sorted(_cluster(lats, _cluster_tol(lats)), reverse=True)
    col_centers = sorted(_cluster(lons, _cluster_tol(lons)))

    grid: list[list[Optional[str]]] = [
        [None] * len(col_centers) for _ in row_centers
    ]
    for n in names:
        r = _nearest_index(row_centers, centers[n][0])
        c = _nearest_index(col_centers, centers[n][1])
        grid[r][c] = n
    return grid


# --- partitioning into per-group GatheredTiles ------------------------------

def _buildGroup(
    positional_tiles: list[tuple[tuple[int, int], Tile]],
    origin: tuple[int, int],
    link: bool,
) -> GatheredTiles:
    """Build a GatheredTiles from a list of ((row, col), Tile) entries.

    When `link` is True (a complete N x N block), within-group neighbor links
    are wired so the group forms a tree rooted at its NW tile. When False
    (a leftover), the single tile is the root with no links.
    """
    by_pos: dict[tuple[int, int], Tile] = {
        (r, c): t for (r, c), t in positional_tiles
    }
    tiles = [t for _, t in positional_tiles]
    root = by_pos[min(by_pos)]  # min (r, c) lexicographically == NW-most

    if link:
        for (r, c), tile in positional_tiles:
            east = by_pos.get((r, c + 1))
            if east is not None:
                tile.link(TileSide.EAST, east)
            south = by_pos.get((r + 1, c))
            if south is not None:
                tile.link(TileSide.SOUTH, south)

    return GatheredTiles(root=root, tiles=tiles, origin=origin)


def partitionGroups(
    grid: list[list[Optional[str]]],
    nameToTile: dict,
    dimension: int,
) -> list[GatheredTiles]:
    """Walk the grid in N x N windows and emit one GatheredTiles per output.

    A window is gathered into a single group only if it is a complete N x N
    block (all cells present and within bounds). Otherwise every present tile
    in that window becomes its own 1-tile leftover group.
    """
    rows = len(grid)
    cols = len(grid[0]) if rows else 0
    groups: list[GatheredTiles] = []

    for r0 in range(0, rows, dimension):
        for c0 in range(0, cols, dimension):
            r_end = r0 + dimension
            c_end = c0 + dimension
            full_window = r_end <= rows and c_end <= cols

            present: list[tuple[tuple[int, int], Tile]] = []
            for r in range(r0, min(r_end, rows)):
                for c in range(c0, min(c_end, cols)):
                    name = grid[r][c]
                    if name is not None:
                        present.append(((r, c), nameToTile[name]))

            if full_window and len(present) == dimension * dimension:
                groups.append(_buildGroup(present, (r0, c0), link=True))
            else:
                for (r, c), tile in present:
                    groups.append(_buildGroup([((r, c), tile)], (r, c), link=False))

    return groups


# --- validation (global, before partitioning) -------------------------------

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
                f'edges do not touch (lon gap {gap:.6g}).'
            )
        if abs(a.north_lat() - b.north_lat()) > tol_h or \
           abs(a.south_lat() - b.south_lat()) > tol_h:
            warnings.warn(
                f'Tiles "{a.name}" and "{b.name}" (east-neighbors) are '
                f'vertically misaligned; seam may be offset.'
            )
    else:  # SOUTH
        gap = abs(a.south_lat() - b.north_lat())
        if gap > tol_h:
            warnings.warn(
                f'Tiles "{a.name}" and "{b.name}" are south-neighbors but their '
                f'edges do not touch (lat gap {gap:.6g}).'
            )
        if abs(a.west_lon() - b.west_lon()) > tol_w or \
           abs(a.east_lon() - b.east_lon()) > tol_w:
            warnings.warn(
                f'Tiles "{a.name}" and "{b.name}" (south-neighbors) are '
                f'horizontally misaligned; seam may be offset.'
            )


def _check_touching(
    grid: list[list[Optional[str]]], nameToTile: dict
) -> None:
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
        "coverage would be drawn multiple times. Conflicts: "
        + "; ".join(conflicts)
    )


# --- manifest reading -------------------------------------------------------

def readManifest(inputDir: str) -> dict:
    """Read the prep-ortho manifest (height_info.json) -> {name: Bounds}.

    The manifest lists each tile as {"name", "bounds"}; that is all the graph
    layer needs (no image files are opened in this layer).
    """
    manifestPath = os.path.join(inputDir, "height_info.json")
    if not os.path.isfile(manifestPath):
        raise Exception("height_info.json not found in input directory")

    with open(manifestPath) as f:
        data = json.load(f)

    nameToBounds: dict = {}
    for entry in data.get("images", []):
        name = entry["name"]
        nameToBounds[name] = Bounds.fromDict(entry["bounds"])
    return nameToBounds


# --- entrypoint -------------------------------------------------------------

def main(
    inputDir: str, outputDir: str, dimension: int = 1
) -> list[GatheredTiles]:
    if dimension < 1:
        raise ValueError("dimension must be >= 1")
    if not os.path.isdir(inputDir):
        raise Exception("Input directory does not exist")
    os.makedirs(outputDir, exist_ok=True)

    nameToBounds = readManifest(inputDir)
    if not nameToBounds:
        raise Exception("No images listed in height_info.json")

    print("Building tile grid...")
    grid = buildTileGrid(nameToBounds)
    nameToTile = {
        name: Tile(
            name=name,
            image_path=os.path.join(inputDir, name + ".png"),
            bounds=bounds,
        )
        for name, bounds in nameToBounds.items()
    }

    print("Validating tile coverage...")
    _check_touching(grid, nameToTile)                  # warn on gaps/misalignment
    _check_overlaps(list(nameToTile.values()), 0.05)   # fail on any area overlap

    print(f"Partitioning into {dimension}x{dimension} groups...")
    groups = partitionGroups(grid, nameToTile, dimension)

    # NEXT STEP (not in this layer):
    #   - for each GatheredTiles, paste traverse() into one output image
    #     (mode preserved from the first tile; leftover 1-tile groups -> copy)
    #   - write gathered_r<origin.r>_c<origin.c>.png + sidecar per group
    #   - write height_info.json listing exactly the surviving output images

    print(
        f"Tile gathering complete: {len(groups)} group(s) from "
        f"{len(nameToTile)} tile(s)"
    )
    return groups
