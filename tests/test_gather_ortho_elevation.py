"""Tests for gather-ortho's elevation GeoTIFF handling (import_from_arcgis_dir
with -e / elevation_data_dir).

gather-ortho --source arcgis -e reuses the prep-geo merge. Sources produced
by download-elevation sit on the Web Mercator tile grid; those are composited
without reprojection so the merged file stays EPSG:3857 and 1:1 aligned with
the ortho tiles. Heterogeneous sources fall back to the legacy clip+composite
to a continuous EPSG:4326 GeoTIFF. Either way that single merged file is
recorded under height_info.json["elevation_files"] and carries a twin
<name>_info.json sidecar recording the projection. An empty elevation
directory, or a region no tile intersects, must fail loudly so the user is
forced to supply coverage.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from rasterio.warp import transform_bounds

from terrain_stitcher.arcgis.level_of_detail import (
    pixel_size_for_zoom,
    tile_grid_envelope,
)
from terrain_stitcher.common.ElevationFileInfo import elevation_info_path
from terrain_stitcher.functions.ArcGisImporter import (
    _merge_elevation_into_output,
    import_from_arcgis_dir,
)
from terrain_stitcher.util import write_star_ignore_marker

FIXTURE = Path(__file__).parent / "fixtures" / "arcgis_cache"
SHAPE_JSON = FIXTURE / "shape.json"


def _write_tif(path, west, north, east, south, value, res=0.005):
    width = int(round((east - west) / res))
    height = int(round((north - south) / res))
    transform = from_origin(west, north, res, res)
    data = np.full((1, height, width), float(value), dtype="float32")
    with rasterio.open(
        path, "w", driver="GTiff", height=height, width=width, count=1,
        dtype="float32", crs="EPSG:4326", transform=transform,
        nodata=float("nan"),
    ) as dst:
        dst.write(data)


def _sample(path, lon, lat):
    """Median valid pixel in a 5x5 neighborhood (robust to nodata edges)."""
    with rasterio.open(path) as ds:
        row, col = ds.index(lon, lat)
        r, c = int(row), int(col)
        arr = ds.read(
            1, window=((r - 2, r + 3), (c - 2, c + 3)),
            boundless=True, fill_value=float("nan"),
        )
        return float(np.nanmedian(arr))


# The fixture shape.json AOI (Alaska / Perryville): lat ~[55.81, 56.10], lon
# ~[-159.52, -159.01]. Split coverage at lat 55.95 so the two tiles carry
# distinct values we can verify after the merge.
def _write_perryville_elevation(elev_dir, bottom_value=100.0, top_value=200.0):
    elev_dir.mkdir(parents=True, exist_ok=True)
    _write_tif(elev_dir / "bottom.tif", -159.53, 55.95, -159.00, 55.80, bottom_value)
    _write_tif(elev_dir / "top.tif", -159.53, 56.10, -159.00, 55.95, top_value)
    return elev_dir


# Padded degree bounds are derived from the fixture shape AOI plus the
# 0.1-degree padding _merge_elevation_into_output passes, so aligned sources
# cover the merged frame exactly.
def _padded_aoi_bounds(padding_deg):
    from terrain_stitcher.common.ParseArea import ParseArea

    region = ParseArea.fromJSONFile(SHAPE_JSON).getTotalRegion()
    west = region.get_lower_left().get_lon() - padding_deg
    south = region.get_lower_left().get_lat() - padding_deg
    east = region.get_upper_right().get_lon() + padding_deg
    north = region.get_upper_right().get_lat() + padding_deg
    return west, south, east, north


def _write_perryville_elevation_aligned(elev_dir, lod=12, padding_deg=0.1):
    """Two EPSG:3857 chunks on the Web Mercator tile grid covering the padded
    AOI, like download-elevation produces (left half / right half of the
    tile-aligned envelope, with a pixel-gradient pattern)."""
    elev_dir.mkdir(parents=True, exist_ok=True)
    west, south, east, north = _padded_aoi_bounds(padding_deg)
    xmin, ymin, xmax, ymax = transform_bounds(
        "EPSG:4326", "EPSG:3857", west, south, east, north
    )
    (
        env_xmin,
        env_ymin,
        env_xmax,
        env_ymax,
        _col0,
        _row0,
        tile_cols,
        tile_rows,
    ) = tile_grid_envelope(xmin, ymin, xmax, ymax, lod)
    res = pixel_size_for_zoom(lod)

    # Split the envelope into left/right halves at a whole tile column.
    left_tiles = tile_cols // 2
    left_w = left_tiles * 256
    right_w = (tile_cols - left_tiles) * 256
    height = tile_rows * 256

    def _pattern(base_value, width):
        # Per-pixel gradient: any resampling would smear it, so an exact
        # match proves the aligned path copies pixels 1:1.
        rows = np.arange(height, dtype="float32")[:, None]
        cols = np.arange(width, dtype="float32")[None, :]
        return (base_value + 0.01 * (rows + cols)).astype("float32")

    def _write(path, chunk_xmin, width, data):
        transform = from_origin(chunk_xmin, env_ymax, res, res)
        with rasterio.open(
            path,
            "w",
            driver="GTiff",
            height=height,
            width=width,
            count=1,
            dtype="float32",
            crs="EPSG:3857",
            transform=transform,
            nodata=float("nan"),
        ) as dst:
            dst.write(data[None, :, :])

    left = _pattern(100.0, left_w)
    right = _pattern(200.0, right_w)
    _write(elev_dir / "left.tif", env_xmin, left_w, left)
    _write(
        elev_dir / "right.tif", env_xmin + left_w * res, right_w, right
    )
    expected = np.concatenate([left, right], axis=1)
    return elev_dir, {
        "lod": lod,
        "env": (env_xmin, env_ymin, env_xmax, env_ymax),
        "res": res,
        "expected": expected,
    }


def test_gather_ortho_elevation_merges_into_single_tif(tmp_path):
    elev_dir = _write_perryville_elevation(tmp_path / "elevation")
    out = tmp_path / "out"

    groups = import_from_arcgis_dir(
        shape_file=str(SHAPE_JSON),
        cache_dir=str(FIXTURE),
        output_dir=str(out),
        elevation_data_dir=str(elev_dir),
        dimension=1,
        workers=1,
    )

    # ortho stitching still produced its groups
    assert len(groups) == 3

    out_files = set(os.listdir(out))
    assert "elevation_merged.tif" in out_files
    # Shape.json is still placed in the output (old copy-elevation behavior)
    assert os.path.basename(SHAPE_JSON) in out_files
    assert "height_info.json" in out_files
    # the merged tif carries a twin info sidecar recording the projection
    assert "elevation_merged_info.json" in out_files
    assert json.loads((out / "elevation_merged_info.json").read_text()) == {
        "projection": "EPSG:4326"
    }

    manifest = json.loads((out / "height_info.json").read_text())
    assert manifest["elevation_files"] == ["elevation_merged.tif"]
    assert len(manifest["images"]) == 3

    # the merged tif is a single-band EPSG:4326 GeoTIFF covering the AOI
    with rasterio.open(out / "elevation_merged.tif") as ds:
        assert ds.count == 1
        assert ds.crs.to_string() == "EPSG:4326"
        b = ds.bounds
        assert b.left <= -159.52 and b.right >= -159.01
        assert b.bottom <= 55.81 and b.top >= 56.10

    # compositing: bottom half carries bottom_value, top half top_value
    assert _sample(out / "elevation_merged.tif", -159.25, 55.85) == pytest.approx(100.0, abs=0.01)
    assert _sample(out / "elevation_merged.tif", -159.25, 56.00) == pytest.approx(200.0, abs=0.01)

    # the created GeoTIFF is paired with an empty .star_ignore_<name> marker
    marker = out / ".star_ignore_elevation_merged"
    assert marker.is_file()
    assert marker.stat().st_size == 0


def test_merge_aligned_elevation_stays_web_mercator(tmp_path):
    """Grid-aligned 3857 sources composite without reprojection: the output
    stays EPSG:3857 on the Web Mercator tile grid and the pixels are copied
    1:1 (the gradient pattern survives exactly, so no resampling ran)."""
    elev_dir, meta = _write_perryville_elevation_aligned(tmp_path / "elevation")
    out = tmp_path / "out"
    out.mkdir()

    _merge_elevation_into_output(str(SHAPE_JSON), str(elev_dir), str(out), 0.1)

    merged = out / "elevation_merged.tif"
    env_xmin, env_ymin, env_xmax, env_ymax = meta["env"]
    with rasterio.open(merged) as ds:
        assert ds.crs.to_string() == "EPSG:3857"
        assert ds.res[0] == meta["res"] and ds.res[1] == meta["res"]
        # The transform origin + res are stored exactly; the far bounds are
        # recomputed as origin + n*res and carry float ulp noise.
        assert ds.bounds.left == env_xmin
        assert ds.bounds.top == env_ymax
        assert ds.bounds.right == pytest.approx(env_xmax, abs=1e-6)
        assert ds.bounds.bottom == pytest.approx(env_ymin, abs=1e-6)
        assert (ds.height, ds.width) == meta["expected"].shape
        # 1:1 copy, no resampling: the gradient pattern survives exactly.
        assert np.array_equal(ds.read(1), meta["expected"])

    # twin sidecar records the projection system
    info = json.loads(elevation_info_path(merged).read_text())
    assert info == {"projection": "EPSG:3857"}
    # star marker still written next to the merged tif
    assert (out / ".star_ignore_elevation_merged").is_file()


def test_merge_mixed_crs_elevation_falls_back_to_4326(tmp_path):
    """A heterogeneous mix (legacy 4326 + aligned 3857) must NOT take the
    aligned path; it falls back to the legacy warp to a continuous EPSG:4326
    GeoTIFF, with the sidecar recording EPSG:4326."""
    elev_dir, _meta = _write_perryville_elevation_aligned(tmp_path / "elevation")
    # add a legacy geographic tile overlapping the AOI
    _write_tif(elev_dir / "legacy.tif", -159.6, 56.2, -158.9, 55.7, 300.0)
    out = tmp_path / "out"
    out.mkdir()

    _merge_elevation_into_output(str(SHAPE_JSON), str(elev_dir), str(out), 0.1)

    with rasterio.open(out / "elevation_merged.tif") as ds:
        assert ds.crs.to_string() == "EPSG:4326"
    info = json.loads(elevation_info_path(out / "elevation_merged.tif").read_text())
    assert info == {"projection": "EPSG:4326"}


def test_gather_ortho_elevation_requires_shape(tmp_path):
    elev_dir = _write_perryville_elevation(tmp_path / "elevation")
    out = tmp_path / "out"
    with pytest.raises(Exception, match="requires --shape/-s"):
        import_from_arcgis_dir(
            shape_file=None,
            cache_dir=str(FIXTURE),
            output_dir=str(out),
            elevation_data_dir=str(elev_dir),
            dimension=1,
            workers=1,
        )


def test_merge_elevation_raises_on_empty_dir(tmp_path):
    elev_dir = tmp_path / "empty"
    elev_dir.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(Exception, match="No .tif elevation files found"):
        _merge_elevation_into_output(str(SHAPE_JSON), str(elev_dir), str(out), 0.1)


def test_merge_elevation_raises_when_no_tile_intersects(tmp_path):
    elev_dir = tmp_path / "far"
    elev_dir.mkdir()
    # a tile around (0,0) -- nowhere near the Alaska AOI
    _write_tif(elev_dir / "far.tif", 0.0, 1.0, 1.0, 0.0, 5.0)
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(Exception, match="nothing to merge"):
        _merge_elevation_into_output(str(SHAPE_JSON), str(elev_dir), str(out), 0.1)


def test_write_star_ignore_marker_creates_empty_dotfile(tmp_path):
    geotiff = tmp_path / "sub" / "elevation_merged.tif"
    geotiff.parent.mkdir(parents=True)
    geotiff.write_bytes(b"FAKE")
    marker = write_star_ignore_marker(str(geotiff))
    assert os.path.basename(marker) == ".star_ignore_elevation_merged"
    assert os.path.dirname(marker) == str(geotiff.parent)
    assert os.path.isfile(marker)
    assert os.path.getsize(marker) == 0
    # idempotent: re-running truncates rather than failing
    (geotiff.parent / ".star_ignore_elevation_merged").write_text("x")
    write_star_ignore_marker(str(geotiff))
    assert os.path.getsize(marker) == 0