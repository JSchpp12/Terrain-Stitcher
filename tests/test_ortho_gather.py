from __future__ import annotations

import json
from pathlib import Path

import pytest

from terrain_stitcher.functions.OrthoGather import (
    GatheredTiles,
    Tile,
    buildTileGrid,
    main as stitch_main,
    partitionGroups,
    readManifest,
)


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


def _write_manifest(tmp_path: Path, images):
    (tmp_path / "height_info.json").write_text(json.dumps({"images": images}))


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


def _write_dummy_pngs(tmp_path: Path, names, size=(10, 10), mode="RGB"):
    from PIL import Image as pImage
    for n in names:
        pImage.new(mode, size).save(tmp_path / (n + ".png"))


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
    from PIL import Image as pImage

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
    from PIL import Image as pImage

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
import json, os

from terrain_stitcher.functions.OrthoGather import main as stitch_main


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
    (tmp_path / "height_info.json").write_text(
        json.dumps({"images": images, "elevation_files": elevation_files})
    )
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
    from PIL import Image as pImage
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


def test_stitch_scale_factor_rejects_zero_and_negative(tmp_path):
    _write_manifest(tmp_path, NO_OVERLAP_IMAGES)
    _write_dummy_pngs(tmp_path, ["tile_0_0", "tile_0_1", "tile_1_0", "tile_1_1"])
    out = tmp_path / "out"
    for bad in (0.0, -0.5):
        with pytest.raises(ValueError, match="scale_factor must be in"):
            stitch_main(str(tmp_path), str(out), dimension=2, scale_factor=bad)


def test_stitch_scale_factor_passthrough_dimension_one(tmp_path):
    """dimension=1 passthrough still honors scale_factor: each 1-tile group
    is a single downscaled tile."""
    _write_manifest(tmp_path, NO_OVERLAP_IMAGES)
    _write_dummy_pngs(tmp_path, ["tile_0_0", "tile_0_1", "tile_1_0", "tile_1_1"],
                      size=(10, 10), mode="RGB")
    out = tmp_path / "out"
    groups = stitch_main(str(tmp_path), str(out), dimension=1, scale_factor=0.25)
    for gt in groups:
        canvas = gt.createMergedImage()
        assert canvas.size == (2, 2)  # 10 * 0.25 = 2 (floored)


# --- pre-flight tile image validation --------------------------------------

def test_stitch_raises_clear_error_on_missing_tile_png(tmp_path):
    """A manifest entry whose .png is absent must fail BEFORE the threaded
    stitch starts, naming the missing tile. Previously this surfaced hours
    into the run as a bare PIL traceback from ``fp.read(16)``."""
    _write_manifest(tmp_path, NO_OVERLAP_IMAGES)
    # only write 3 of the 4 tile pngs
    _write_dummy_pngs(tmp_path, ["tile_0_0", "tile_0_1", "tile_1_0"])
    out = tmp_path / "out"
    with pytest.raises(FileNotFoundError, match="missing tile image files"):
        stitch_main(str(tmp_path), str(out), dimension=2)
    # the missing tile is named in the error
    try:
        stitch_main(str(tmp_path), str(out), dimension=2)
    except FileNotFoundError as exc:
        assert "tile_1_1" in str(exc)
    # nothing should have been stitched
    assert not out.exists() or not any(out.iterdir())


def test_stitch_raises_clear_error_on_empty_tile_png(tmp_path):
    """A 0-byte tile png must fail up front rather than mid-stitch."""
    _write_manifest(tmp_path, NO_OVERLAP_IMAGES)
    _write_dummy_pngs(tmp_path, ["tile_0_0", "tile_0_1", "tile_1_1"])
    # write an empty file for the remaining tile
    (tmp_path / "tile_1_0.png").write_bytes(b"")
    out = tmp_path / "out"
    with pytest.raises(FileNotFoundError, match="empty tile image files"):
        stitch_main(str(tmp_path), str(out), dimension=2)
    try:
        stitch_main(str(tmp_path), str(out), dimension=2)
    except FileNotFoundError as exc:
        assert "tile_1_0" in str(exc)


def test_open_tile_image_error_names_tile_and_path(tmp_path):
    """_open_tile_image re-raises with the tile name and path so a single bad
    tile in a large stitch is diagnosable."""
    from terrain_stitcher.functions.OrthoGather import _open_tile_image
    from terrain_stitcher.sources import Bounds
    from terrain_stitcher.common import World_Coordinates

    bogus = tmp_path / "does_not_exist.png"
    b = Bounds(
        coords_northEast=World_Coordinates(2, 1),
        coords_southEast=World_Coordinates(1, 1),
        coords_southWest=World_Coordinates(1, 0),
        coords_northWest=World_Coordinates(2, 0),
        coords_center=World_Coordinates(1.5, 0.5),
    )
    tile = Tile(name="bad_tile", image_path=str(bogus), bounds=b)
    with pytest.raises(OSError) as excinfo:
        _open_tile_image(tile)
    assert "bad_tile" in str(excinfo.value)
    # path is rendered via repr (backslashes escaped), so compare the basename
    # which contains no separators, plus confirm the full path is referenced.
    assert bogus.name in str(excinfo.value)


# --- resume (--resume skips already-stitched groups) -----------------------

def test_stitch_resume_skips_existing_groups(tmp_path):
    """With resume=True, groups whose output already exists are skipped and
    left untouched, while missing groups are still stitched. Lets an
    interrupted stitch continue without re-doing finished work."""
    from PIL import Image as pImage

    _write_manifest(tmp_path, NO_OVERLAP_IMAGES)
    # dimension=1 -> one group per tile, four outputs gathered_r0_c0..r1_c1
    # pre-write two of the four expected outputs so resume should skip them
    out = tmp_path / "out"
    out.mkdir()
    pImage.new("RGB", (10, 10), (1, 2, 3)).save(out / "gathered_r0_c0.png")
    marker = out / "gathered_r0_c0.png"
    mtime_before = marker.stat().st_mtime_ns

    _write_dummy_pngs(tmp_path, ["tile_0_0", "tile_0_1", "tile_1_0", "tile_1_1"])
    stitch_main(str(tmp_path), str(out), dimension=1, resume=True)

    out_files = sorted(f.name for f in out.iterdir() if f.suffix == ".png")
    assert "gathered_r0_c0.png" in out_files
    # the pre-existing file was NOT rewritten (same mtime)
    assert marker.stat().st_mtime_ns == mtime_before
    # the other three were freshly produced
    assert "gathered_r0_c1.png" in out_files
    assert "gathered_r1_c0.png" in out_files
    assert "gathered_r1_c1.png" in out_files
    # no leftover temp files
    assert not any(f.endswith(".tmp") for f in out_files)


def test_stitch_without_resume_overwrites_existing_groups(tmp_path):
    """Without resume (default), existing outputs are regenerated so a stale
    or truncated file from a prior run gets replaced with a fresh write."""
    from PIL import Image as pImage

    _write_manifest(tmp_path, NO_OVERLAP_IMAGES)
    out = tmp_path / "out"
    out.mkdir()
    # a truncated/placeholder file for one group
    (out / "gathered_r0_c0.png").write_bytes(b"NOT A PNG")
    _write_dummy_pngs(tmp_path, ["tile_0_0", "tile_0_1", "tile_1_0", "tile_1_1"])
    stitch_main(str(tmp_path), str(out), dimension=1)
    # the bad placeholder was overwritten with a real image
    with pImage.open(out / "gathered_r0_c0.png") as img:
        assert img.size == (10, 10)


# --- parallel process-pool stitch path -------------------------------------


def _nxn_grid_images(n):
    """An n x n tile grid (n**2 tiles), edges touching, NW at row 0."""
    return [
        _tile_entry(f"tile_{r}_{c}", n - r, n - r - 1, c, c + 1)
        for r in range(n)
        for c in range(n)
    ]


def _cell_color(r, c):
    return ((r * 28) % 256, (c * 28) % 256, 128)

# --- ratio-gated downscale resample ----------------------------------------


def test_resize_tile_small_downscale_uses_plain_lanczos(monkeypatch):
    """scale_factor above the progressive-reduction threshold uses a single
    LANCZOS pass (no reducing_gap) -- sharpest, and no large-ratio aliasing to
    fix."""
    import terrain_stitcher.functions.OrthoGather as og
    from PIL import Image as pImage

    calls = []

    def fake_resize(self, size, resample=None, box=None, reducing_gap=None, **kw):
        calls.append(reducing_gap)
        return pImage.new("RGB", size)

    monkeypatch.setattr(pImage.Image, "resize", fake_resize)
    og._resize_tile(pImage.new("RGB", (100, 100)), 50, 50, 0.5)
    assert calls == [None]


def test_resize_tile_large_downscale_uses_progressive_reduction(monkeypatch):
    """scale_factor at/below the threshold uses LANCZOS with reducing_gap, which
    pre-shrinks via area averaging then runs a final LANCZOS pass -- avoids the
    moire a plain LANCZOS produces at big downscale factors and is faster."""
    import terrain_stitcher.functions.OrthoGather as og
    from PIL import Image as pImage

    calls = []

    def fake_resize(self, size, resample=None, box=None, reducing_gap=None, **kw):
        calls.append(reducing_gap)
        return pImage.new("RGB", size)

    monkeypatch.setattr(pImage.Image, "resize", fake_resize)
    og._resize_tile(pImage.new("RGB", (100, 100)), 5, 5, 0.05)
    assert calls == [og._REDUCING_GAP]


@pytest.mark.parametrize("scale", [1.0, 0.5, 0.05])
def test_stitch_pool_matches_reference(tmp_path, scale):
    """The per-group process-pool worker (canvas allocated in the worker) must
    produce a merged image identical to an in-process serial pasteTiles
    reference, across no-scale, small, and large downscale. Covers the
    canvas-in-worker path and the ratio-gated resample end to end."""
    from PIL import Image as pImage

    n = 9  # 81 tiles -> process pool, one group
    images = _nxn_grid_images(n)
    _write_manifest(tmp_path, images)
    for r in range(n):
        for c in range(n):
            pImage.new("RGB", (100, 100), _cell_color(r, c)).save(
                tmp_path / f"tile_{r}_{c}.png"
            )

    out = tmp_path / "out"
    groups = stitch_main(str(tmp_path), str(out), dimension=n,
                         scale_factor=scale)
    assert len(groups) == 1
    cell = int(100 * scale)

    # Reference: serial in-process pasteTiles on the returned group.
    ref = groups[0].createMergedImage()
    groups[0].pasteTiles(ref)

    with pImage.open(out / "gathered_r0_c0.png") as produced:
        assert produced.size == ref.size == (n * cell, n * cell)
        assert produced.tobytes() == ref.tobytes()

    # Each tile's color lands at its grid cell (top-left pixel).
    with pImage.open(out / "gathered_r0_c0.png") as img:
        px = img.load()
        for r in range(n):
            for c in range(n):
                assert px[c * cell, r * cell] == _cell_color(r, c)



def _group_for_grid(tmp_path, n, scale=1.0, size=10):
    from PIL import Image as pImage
    images = _nxn_grid_images(n)
    _write_manifest(tmp_path, images)
    for r in range(n):
        for c in range(n):
            pImage.new("RGB", (size, size)).save(tmp_path / f"tile_{r}_{c}.png")
    grid = buildTileGrid(readManifest(str(tmp_path)))
    nb = readManifest(str(tmp_path))
    nameToTile = {
        name: Tile(name=name, image_path=str(tmp_path / (name + ".png")),
                   bounds=b)
        for name, b in nb.items()
    }
    groups = partitionGroups(grid, nameToTile, n, scale)
    return groups


def test_plan_strips_routes_tiny_and_giant_to_whole_group(tmp_path, monkeypatch):
    """_plan_strips returns [] (-> one whole-group pool task) for groups that
    are too small or whose canvas is too big to strip-book, and non-empty for a
    normal big group."""
    import terrain_stitcher.functions.OrthoGather as og

    # tiny group (4 tiles < _MIN_TILES_FOR_STRIPS) -> whole-group
    g_small = _group_for_grid(tmp_path, 2)
    mode, _, _ = g_small[0].canvas_meta()
    assert og._plan_strips(g_small[0], mode, 4) == []

    # normal big group (81 tiles) -> strips
    tmp2 = tmp_path / "g2"
    tmp2.mkdir()
    g_big = _group_for_grid(tmp2, 9)
    mode, _, _ = g_big[0].canvas_meta()
    assert og._plan_strips(g_big[0], mode, 4), "big group should be strip-eligible"

    # forcing a tiny budget makes the big group "giant" -> whole-group
    monkeypatch.setattr(og, "_STRIP_CANVAS_BUDGET", 1)
    assert og._plan_strips(g_big[0], mode, 4) == []


def test_stitch_whole_group_path_matches_reference(tmp_path, monkeypatch):
    """Forcing the whole-group path (canvas allocated in the worker, one pool
    task) still produces output identical to a serial pasteTiles reference."""
    import terrain_stitcher.functions.OrthoGather as og
    from PIL import Image as pImage

    n = 9
    images = _nxn_grid_images(n)
    _write_manifest(tmp_path, images)
    for r in range(n):
        for c in range(n):
            pImage.new("RGB", (100, 100), _cell_color(r, c)).save(
                tmp_path / f"tile_{r}_{c}.png"
            )

    monkeypatch.setattr(og, "_STRIP_CANVAS_BUDGET", 1)  # force whole-group path

    out = tmp_path / "out"
    groups = stitch_main(str(tmp_path), str(out), dimension=n, scale_factor=1.0)
    assert len(groups) == 1

    ref = groups[0].createMergedImage()
    groups[0].pasteTiles(ref)

    with pImage.open(out / "gathered_r0_c0.png") as produced:
        assert produced.size == ref.size == (n * 100, n * 100)
        assert produced.tobytes() == ref.tobytes()
