"""Tests for the prep-ortho -> stitch-ortho path (OrthoStitcher.main).

This is the manifest-driven USGS stitch path (not the folded ArcGIS import):
tiles are placed on their geographic grid from a height_info.json manifest,
partitioned into N x N windows, and composited. Covers the grid build,
partitioning, the merged-image canvas sizing / paste placement / alpha, the
overlap-coverage guard, the pass-through of non-tile files (elevation TIFs /
sidecars / Shape.json), and scale_factor downscaling.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from PIL import Image as pImage

from terrain_stitcher.functions.ArcGisImporter import readManifest
from terrain_stitcher.functions.OrthoStitcher import (
    main as stitch_main,
    partitionGroups,
)
from terrain_stitcher.stitching.grid_functions import GatheredTiles, buildTileGrid
from terrain_stitcher.common import Tile


def _coord(lat, lon):
    return {"lat": lat, "lon": lon}


def _bounds(lat_n, lat_s, lon_w, lon_e):
    return {
        "center": _coord((lat_n + lat_s) / 2, (lon_w + lon_e) / 2),
        "northEast": _coord(lat_n, lon_e),
        "southEast": _coord(lat_s, lon_e),
        "southWest": _coord(lat_s, lon_w),
        "northWest": _coord(lat_n, lon_w),
    }


def _tile_entry(name, lat_n, lat_s, lon_w, lon_e):
    return {"name": name, "bounds": _bounds(lat_n, lat_s, lon_w, lon_e)}


def _write_manifest(tmp_path: Path, images, elevation_files=None):
    data = {"images": images}
    if elevation_files is not None:
        data["elevation_files"] = elevation_files
    (tmp_path / "height_info.json").write_text(json.dumps(data))


def _write_dummy_pngs(tmp_path: Path, names, size=(10, 10), mode="RGB"):
    for n in names:
        pImage.new(mode, size).save(tmp_path / (n + ".png"))


# 2x2 grid, edges touch exactly, no area overlap.
NO_OVERLAP_IMAGES = [
    _tile_entry("tile_0_0", 2, 1, 0, 1),
    _tile_entry("tile_0_1", 2, 1, 1, 2),
    _tile_entry("tile_1_0", 1, 0, 0, 1),
    _tile_entry("tile_1_1", 1, 0, 1, 2),
]

# Same grid but tile_0_1 shifted west so it overlaps tile_0_0 in lon 0.5..1.
OVERLAP_IMAGES = [
    _tile_entry("tile_0_0", 2, 1, 0, 1),
    _tile_entry("tile_0_1", 2, 1, 0.5, 1.5),
    _tile_entry("tile_1_0", 1, 0, 0, 1),
    _tile_entry("tile_1_1", 1, 0, 1, 2),
]


def test_read_manifest(tmp_path):
    _write_manifest(tmp_path, NO_OVERLAP_IMAGES)
    nameToBounds = readManifest(str(tmp_path))
    assert set(nameToBounds.keys()) == {
        "tile_0_0", "tile_0_1", "tile_1_0", "tile_1_1",
    }
    assert nameToBounds["tile_0_0"].coords_northWest.get_lon() == 0


def test_build_tile_grid_2x2_nw_origin(tmp_path):
    _write_manifest(tmp_path, NO_OVERLAP_IMAGES)
    grid = buildTileGrid(readManifest(str(tmp_path)))
    # Sparse {(row, col): name}; NW tile at (0, 0), rows north->south,
    # cols west->east. Absent cells are simply not present.
    assert grid == {
        (0, 0): "tile_0_0",
        (0, 1): "tile_0_1",
        (1, 0): "tile_1_0",
        (1, 1): "tile_1_1",
    }


def test_stitch_no_overlap_one_complete_group(tmp_path):
    _write_manifest(tmp_path, NO_OVERLAP_IMAGES)
    _write_dummy_pngs(tmp_path, ["tile_0_0", "tile_0_1", "tile_1_0", "tile_1_1"])
    out = tmp_path / "out"
    groups = stitch_main(str(tmp_path), str(out), dimension=2)

    # 2x2 complete block -> exactly one GatheredTiles with 4 tiles
    assert len(groups) == 1
    gt = groups[0]
    assert isinstance(gt, GatheredTiles)
    assert len(gt.tiles) == 4
    assert gt.origin == (0, 0)
    # root is the NW tile
    assert gt.root.name == "tile_0_0"
    # traversal reproduces the 2x2 layout
    pos = gt.get_traversal()
    assert set(pos.keys()) == {(0, 0), (0, 1), (1, 0), (1, 1)}
    assert pos[(0, 0)].name == "tile_0_0"
    assert pos[(1, 1)].name == "tile_1_1"


def test_stitch_dimension_one_is_passthrough(tmp_path):
    _write_manifest(tmp_path, NO_OVERLAP_IMAGES)
    _write_dummy_pngs(tmp_path, ["tile_0_0", "tile_0_1", "tile_1_0", "tile_1_1"])
    out = tmp_path / "out"
    groups = stitch_main(str(tmp_path), str(out), dimension=1)
    # dimension=1 -> every tile is its own 1-tile group
    assert len(groups) == 4
    for gt in groups:
        assert isinstance(gt, GatheredTiles)
        assert len(gt.tiles) == 1
        assert gt.get_traversal() == {(0, 0): gt.root}


def test_stitch_with_overlaps_raises(tmp_path):
    _write_manifest(tmp_path, OVERLAP_IMAGES)
    out = tmp_path / "out"
    with pytest.raises(RuntimeError, match="overlapping tile coverage"):
        stitch_main(str(tmp_path), str(out), dimension=2)


def test_partition_partial_windows_for_incomplete_block(tmp_path):
    # 3x3 grid (9 tiles), dimension=2 -> four 2x2 windows. The top-left is a
    # complete 2x2 (4 tiles); the other three are partial edge windows that
    # are each stitched into one group rather than split into single-tile
    # leftovers.
    images = [
        _tile_entry(f"t_{r}_{c}", 3 - r, 2 - r, c, c + 1)
        for r in range(3) for c in range(3)
    ]
    _write_manifest(tmp_path, images)
    nameToBounds = readManifest(str(tmp_path))
    grid = buildTileGrid(nameToBounds)
    nameToTile = {
        n: Tile(name=n, image_path=str(tmp_path / (n + ".png")), bounds=b)
        for n, b in nameToBounds.items()
    }
    groups = partitionGroups(grid, nameToTile, 2)
    # one complete 2x2 (4 tiles) + three partial windows (2, 2, 1 tiles) = 4
    assert len(groups) == 4
    complete = [g for g in groups if len(g.tiles) == 4]
    partials = [g for g in groups if len(g.tiles) < 4]
    assert len(complete) == 1
    assert len(partials) == 3
    assert sorted(len(g.tiles) for g in partials) == [1, 2, 2]
    assert complete[0].origin == (0, 0)


def test_create_merged_image_sized_to_group(tmp_path):
    _write_manifest(tmp_path, NO_OVERLAP_IMAGES)
    _write_dummy_pngs(tmp_path, ["tile_0_0", "tile_0_1", "tile_1_0", "tile_1_1"],
                      size=(10, 10), mode="RGB")
    out = tmp_path / "out"
    groups = stitch_main(str(tmp_path), str(out), dimension=2)

    gt = groups[0]
    canvas = gt.createMergedImage()
    # 2 cols x 10px wide, 2 rows x 10px tall
    assert canvas.size == (20, 20)
    assert canvas.mode == "RGB"
    # cell size populated for the paste step
    assert gt.cell_width == 10 and gt.cell_height == 10


def test_create_merged_image_leftover_is_single_tile_size(tmp_path):
    # dimension=1 -> each tile is its own 1x1 group; canvas matches one tile.
    _write_manifest(tmp_path, NO_OVERLAP_IMAGES)
    _write_dummy_pngs(tmp_path, ["tile_0_0", "tile_0_1", "tile_1_0", "tile_1_1"],
                      size=(12, 8), mode="RGBA")
    out = tmp_path / "out"
    groups = stitch_main(str(tmp_path), str(out), dimension=1)

    for gt in groups:
        canvas = gt.createMergedImage()
        assert canvas.size == (12, 8)
        assert canvas.mode == "RGBA"


def test_paste_tiles_places_each_tile_at_its_grid_position(tmp_path):
    _write_manifest(tmp_path, NO_OVERLAP_IMAGES)

    colors = {
        "tile_0_0": (255, 0, 0),     # NW  -> top-left
        "tile_0_1": (0, 255, 0),     # NE  -> top-right
        "tile_1_0": (0, 0, 255),     # SW  -> bottom-left
        "tile_1_1": (255, 255, 0),   # SE  -> bottom-right
    }
    for name, color in colors.items():
        pImage.new("RGB", (10, 10), color).save(tmp_path / (name + ".png"))

    out = tmp_path / "out"
    groups = stitch_main(str(tmp_path), str(out), dimension=2)
    gt = groups[0]

    canvas = gt.createMergedImage()
    gt.pasteTiles(canvas)

    assert canvas.size == (20, 20)
    # top-left quadrant -> tile_0_0 (red)
    assert canvas.getpixel((0, 0)) == (255, 0, 0)
    assert canvas.getpixel((9, 9)) == (255, 0, 0)
    # top-right quadrant -> tile_0_1 (green)
    assert canvas.getpixel((10, 0)) == (0, 255, 0)
    assert canvas.getpixel((19, 9)) == (0, 255, 0)
    # bottom-left quadrant -> tile_1_0 (blue)
    assert canvas.getpixel((0, 10)) == (0, 0, 255)
    assert canvas.getpixel((9, 19)) == (0, 0, 255)
    # bottom-right quadrant -> tile_1_1 (yellow)
    assert canvas.getpixel((10, 10)) == (255, 255, 0)
    assert canvas.getpixel((19, 19)) == (255, 255, 0)


def test_paste_tiles_preserves_alpha(tmp_path):
    _write_manifest(tmp_path, NO_OVERLAP_IMAGES)
    # one fully-transparent tile, three opaque
    transparent = pImage.new("RGBA", (10, 10), (0, 0, 0, 0))
    transparent.save(tmp_path / "tile_0_0.png")
    for name in ("tile_0_1", "tile_1_0", "tile_1_1"):
        pImage.new("RGBA", (10, 10), (200, 100, 50, 255)).save(
            tmp_path / (name + ".png")
        )

    out = tmp_path / "out"
    groups = stitch_main(str(tmp_path), str(out), dimension=2)
    gt = groups[0]

    canvas = gt.createMergedImage()
    gt.pasteTiles(canvas)

    # the transparent tile's quadrant should remain the canvas default (0,0,0,0)
    assert canvas.getpixel((0, 0)) == (0, 0, 0, 0)
    # an opaque tile's quadrant keeps its color and full alpha
    assert canvas.getpixel((10, 0)) == (200, 100, 50, 255)


def test_stitch_passes_through_elevation_tif_sharing_tile_stem(tmp_path):
    """Elevation GeoTIFFs that share a stem with an ortho tile must survive
    stitch-ortho. The pass-through used to skip any file whose stem matched a
    tile name, which dropped elevation TIFs named after the same grid cell as
    their ortho counterpart (and left height_info.json pointing at files that
    never reached the output directory)."""
    images = [
        _tile_entry("tile_0_0", 2, 1, 0, 1),
        _tile_entry("tile_0_1", 2, 1, 1, 2),
        _tile_entry("tile_1_0", 1, 0, 0, 1),
        _tile_entry("tile_1_1", 1, 0, 1, 2),
    ]
    elevation_files = ["tile_0_0.tif", "elev_unique.tif"]
    _write_manifest(tmp_path, images, elevation_files=elevation_files)
    _write_dummy_pngs(tmp_path, [e["name"] for e in images])

    # elevation TIFs: one shares a stem with a tile, one does not
    (tmp_path / "tile_0_0.tif").write_bytes(b"FAKE-ELEV")
    (tmp_path / "elev_unique.tif").write_bytes(b"FAKE-ELEV")
    # sidecar json sharing a tile stem should also pass through
    (tmp_path / "tile_0_0.json").write_text(json.dumps({"meta": 1}))
    # an unrelated non-tile file
    (tmp_path / "Shape.json").write_text("{}")

    out = tmp_path / "out"
    stitch_main(str(tmp_path), str(out), dimension=2)

    out_files = set(os.listdir(out))
    # both elevation TIFs survive, including the one sharing a tile stem
    assert "tile_0_0.tif" in out_files
    assert "elev_unique.tif" in out_files
    # sidecar sharing a tile stem survives too
    assert "tile_0_0.json" in out_files
    assert "Shape.json" in out_files
    # the stitched merged image + manifest are present
    assert "gathered_r0_c0.png" in out_files
    assert "height_info.json" in out_files
    # source per-tile PNGs are not copied through (they are consumed by stitch)
    assert "tile_0_0.png" not in out_files
    # the stitched manifest still advertises the elevation files, and they
    # actually exist on disk next to it
    manifest = json.loads((out / "height_info.json").read_text())
    assert manifest["elevation_files"] == elevation_files
    for f in manifest["elevation_files"]:
        assert (out / f).is_file()


# --- stitch-ortho downscale (scale_factor) ---------------------------------


def test_stitch_scale_factor_downscales_merged_image(tmp_path):
    """A 2x2 grid of 10x10 tiles stitched with scale_factor=0.5 produces a
    10x10 merged image (5x5 cells), with each tile resampled to 5x5 before
    paste. Bounds in the manifest are geographic and unaffected by pixel
    scaling, so the merged image still maps to the original coverage."""
    _write_manifest(tmp_path, NO_OVERLAP_IMAGES)
    _write_dummy_pngs(tmp_path, ["tile_0_0", "tile_0_1", "tile_1_0", "tile_1_1"],
                      size=(10, 10), mode="RGB")
    out = tmp_path / "out"
    groups = stitch_main(str(tmp_path), str(out), dimension=2, scale_factor=0.5)

    gt = groups[0]
    assert gt.scale_factor == 0.5
    canvas = gt.createMergedImage()
    # 2 cols x 5px, 2 rows x 5px -> 10x10 (half of the unscaled 20x20)
    assert canvas.size == (10, 10)
    assert gt.cell_width == 5 and gt.cell_height == 5
    gt.pasteTiles(canvas)
    # the saved output on disk is the scaled size
    with pImage.open(out / "gathered_r0_c0.png") as im:
        assert im.size == (10, 10)


def test_stitch_scale_factor_one_matches_unscaled(tmp_path):
    """scale_factor=1.0 is the fast path: no resample, identical to the
    default behavior (20x20 for a 2x2 of 10x10 tiles)."""
    _write_manifest(tmp_path, NO_OVERLAP_IMAGES)
    _write_dummy_pngs(tmp_path, ["tile_0_0", "tile_0_1", "tile_1_0", "tile_1_1"],
                      size=(10, 10), mode="RGB")
    out = tmp_path / "out"
    groups = stitch_main(str(tmp_path), str(out), dimension=2, scale_factor=1.0)
    canvas = groups[0].createMergedImage()
    assert canvas.size == (20, 20)
    assert groups[0].cell_width == 10 and groups[0].cell_height == 10


def test_stitch_scale_factor_rejects_upscale(tmp_path):
    _write_manifest(tmp_path, NO_OVERLAP_IMAGES)
    _write_dummy_pngs(tmp_path, ["tile_0_0", "tile_0_1", "tile_1_0", "tile_1_1"])
    out = tmp_path / "out"
    with pytest.raises(ValueError, match="only downscaling is supported"):
        stitch_main(str(tmp_path), str(out), dimension=2, scale_factor=1.5)