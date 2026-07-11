from __future__ import annotations

from pathlib import Path

import pytest

from terrain_stitcher.arcgis.acquisition import ArcGisProAcquisitionSource
from terrain_stitcher.arcgis.cache_xml import ArcGisCacheInfo
from terrain_stitcher.arcgis.tile_filter import ShapeTileFilter
from terrain_stitcher.arcgis.tile_info import TileInfo
from terrain_stitcher.arcgis.tile_zip import tile_chunk_name
from terrain_stitcher.common import ParseArea, World_Bounding_Box, World_Coordinates

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "arcgis_cache"
CONF_XML = FIXTURE_DIR / "conf.xml"
SHAPE_JSON = FIXTURE_DIR / "shape.json"
ALL_LAYERS = FIXTURE_DIR / "_alllayers"


@pytest.fixture
def cache_info():
    return ArcGisCacheInfo.from_xml(str(CONF_XML))


def _tile(name: str) -> TileInfo:
    return TileInfo.from_path(ALL_LAYERS / "L23" / "R0027e3a0" / name, ALL_LAYERS)


def _tiles():
    return [_tile(n) for n in ["C00075f6b.png", "C00075f6c.png", "C00075f6d.png"]]


# --- ShapeTileFilter -------------------------------------------------------


def test_filter_is_callable(cache_info):
    region = ParseArea.fromJSONFile(str(SHAPE_JSON)).getTotalRegion()
    f = ShapeTileFilter(cache_info, region)
    assert callable(f)
    assert f(_tile("C00075f6b.png")) is True


def test_filter_includes_tiles_inside_shape_region(cache_info):
    region = ParseArea.fromJSONFile(str(SHAPE_JSON)).getTotalRegion()
    f = ShapeTileFilter(cache_info, region)
    included = [t for t in _tiles() if f(t)]
    assert len(included) == 3


def test_filter_excludes_tiles_outside_shape_region(cache_info):
    # a region around (0,0) -- nowhere near the perryville (Alaska) tiles
    far = World_Bounding_Box(
        World_Coordinates(lat="-1.0", lon="-1.0"),
        World_Coordinates(lat="1.0", lon="1.0"),
    )
    f = ShapeTileFilter(cache_info, far)
    assert [t for t in _tiles() if f(t)] == []


def test_filter_unknown_level_raises(cache_info):
    region = ParseArea.fromJSONFile(str(SHAPE_JSON)).getTotalRegion()
    f = ShapeTileFilter(cache_info, region)
    bad = TileInfo(
        path=Path("L99/R00000000/C00000000.png"),
        layer_number=99,
        row_number=0,
        col_number=0,
    )
    with pytest.raises(ValueError):
        f(bad)


# --- acquire wiring --------------------------------------------------------


def test_acquire_with_shape_includes_only_overlapping_tiles(tmp_path):
    src = ArcGisProAcquisitionSource.from_cache_dir(str(FIXTURE_DIR))
    out = tmp_path / "out"
    src.acquire(str(SHAPE_JSON), str(out), str(FIXTURE_DIR))

    for n in ["C00075f6b.png", "C00075f6c.png", "C00075f6d.png"]:
        chunk = tile_chunk_name(_tile(n))
        assert (out / f"{chunk}.zip").is_file()
        assert (out / f"{chunk}.json").is_file()


def test_acquire_with_far_shape_excludes_all(tmp_path):
    far_shape = tmp_path / "far.json"
    # New Shape.json format: x/y (floats) plus legacy lat/lon strings.
    far_shape.write_text('{"boundsType":"POINT","center":{"lat":"0.0","lon":"0.0","x":0.0,"y":0.0}}')

    src = ArcGisProAcquisitionSource.from_cache_dir(str(FIXTURE_DIR))
    out = tmp_path / "out"
    src.acquire(str(far_shape), str(out), str(FIXTURE_DIR))

    assert not list(out.glob("*.zip"))
    assert not list(out.glob("*.json"))


def test_acquire_without_shape_processes_all(tmp_path):
    src = ArcGisProAcquisitionSource.from_cache_dir(str(FIXTURE_DIR))
    out = tmp_path / "out"
    src.acquire(None, str(out), str(FIXTURE_DIR))

    for n in ["C00075f6b.png", "C00075f6c.png", "C00075f6d.png"]:
        chunk = tile_chunk_name(_tile(n))
        assert (out / f"{chunk}.zip").is_file()
        assert (out / f"{chunk}.json").is_file()
from terrain_stitcher.arcgis.tile_bounds import TileBoundsCalculator


@pytest.fixture
def calculator(cache_info):
    return TileBoundsCalculator(cache_info)


def _box(ll_lat, ll_lon, ur_lat, ur_lon):
    return World_Bounding_Box(
        World_Coordinates(lat=str(ll_lat), lon=str(ll_lon)),
        World_Coordinates(lat=str(ur_lat), lon=str(ur_lon)),
    )


def _corners(calculator, name):
    b = calculator.bounds_for(_tile(name))
    return b.coords_southWest, b.coords_northEast, b.coords_center


def test_filter_includes_partially_overlapping_tile(cache_info, calculator):
    # Shape covers only the eastern portion of the tile (from just east of
    # the center to just east of the east edge) -> partial overlap, NOT full
    # containment. Must still be included.
    sw, ne, c = _corners(calculator, "C00075f6b.png")
    shape = _box(sw.get_lat(), c.get_lon() + 1e-6, ne.get_lat(), ne.get_lon() + 1e-6)
    f = ShapeTileFilter(cache_info, shape)
    assert f(_tile("C00075f6b.png")) is True


def test_filter_includes_tile_fully_inside_shape(cache_info, calculator):
    sw, ne, c = _corners(calculator, "C00075f6b.png")
    shape = _box(sw.get_lat() - 1.0, sw.get_lon() - 1.0, ne.get_lat() + 1.0, ne.get_lon() + 1.0)
    f = ShapeTileFilter(cache_info, shape)
    assert f(_tile("C00075f6b.png")) is True


def test_filter_excludes_tile_separated_by_gap(cache_info, calculator):
    # Shape sits east of the tile with a ~0.001 deg gap -> no overlap.
    sw, ne, c = _corners(calculator, "C00075f6b.png")
    shape = _box(sw.get_lat(), ne.get_lon() + 0.001, ne.get_lat(), ne.get_lon() + 0.002)
    f = ShapeTileFilter(cache_info, shape)
    assert f(_tile("C00075f6b.png")) is False

