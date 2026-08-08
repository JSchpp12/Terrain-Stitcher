"""Tests for gather-ortho's elevation GeoTIFF handling (import_from_arcgis_dir
with -e / elevation_data_dir).

gather-ortho --source arcgis -e reuses the prep-geo merge -- clip each tile
to the shape AOI and composite the intersecting tiles into one continuous
EPSG:4326 GeoTIFF -- and records that single merged file under
height_info.json["elevation_files"]. An empty elevation directory, or a
region no tile intersects, must fail loudly so the user is forced to supply
coverage.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

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