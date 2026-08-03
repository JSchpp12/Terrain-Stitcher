from __future__ import annotations

from pathlib import Path

import pytest

from terrain_stitcher.arcgis.cache_xml import ArcGisCacheInfo
from terrain_stitcher.arcgis.tile_bounds import TileBoundsCalculator, TileFootprints
from terrain_stitcher.arcgis.tile_info import BoundedTileInfo, TileInfo
from terrain_stitcher.sources import Bounds

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "arcgis_cache"
CONF_XML = FIXTURE_DIR / "conf.xml"
ALL_LAYERS = FIXTURE_DIR / "_alllayers"


@pytest.fixture
def calculator():
    return TileBoundsCalculator(ArcGisCacheInfo.from_xml(str(CONF_XML)))


def _tile(name: str) -> TileInfo:
    p = ALL_LAYERS / "L23" / "R0027e3a0" / name
    return TileInfo.from_path(p, ALL_LAYERS)


def test_bounds_for_single_tile_is_valid(calculator):
    b = calculator.bounds_for(_tile("C00075f6b.png"))
    assert isinstance(b, Bounds)
    assert b.isValid()


def test_bounds_compass_orientation(calculator):
    # validates the row/col sign conventions: north is north, west is west
    b = calculator.bounds_for(_tile("C00075f6b.png"))
    assert b.coords_northWest.get_lat() > b.coords_southEast.get_lat()
    assert b.coords_northWest.get_lon() < b.coords_northEast.get_lon()

    clat = b.coords_center.get_lat()
    clon = b.coords_center.get_lon()
    assert b.coords_southEast.get_lat() < clat < b.coords_northWest.get_lat()
    assert b.coords_northWest.get_lon() < clon < b.coords_northEast.get_lon()


def test_bounds_land_near_perryville_alaska(calculator):
    # perryville, AK is ~55.91N, 159.6W; a sign error would put lat/lon
    # out of this range (or yield NaN past the Web Mercator north limit).
    b = calculator.bounds_for(_tile("C00075f6b.png"))
    lat = b.coords_center.get_lat()
    lon = b.coords_center.get_lon()
    assert 50.0 < lat < 60.0
    assert -162.0 < lon < -155.0


def test_bounds_for_all_matches_single(calculator):
    names = ["C00075f6b.png", "C00075f6c.png", "C00075f6d.png"]
    tiles = [_tile(n) for n in names]

    bulk = calculator.bounds_for_all(tiles)
    assert len(bulk) == 3
    assert all(isinstance(bt, BoundedTileInfo) for bt in bulk)
    assert all(bt.bounds.isValid() for bt in bulk)

    for tile, bt in zip(tiles, bulk):
        single = calculator.bounds_for(tile)
        assert bt.tile == tile
        assert bt.bounds.coords_center.get_lat() == pytest.approx(
            single.coords_center.get_lat()
        )
        assert bt.bounds.coords_center.get_lon() == pytest.approx(
            single.coords_center.get_lon()
        )


def test_bounds_for_all_empty(calculator):
    assert calculator.bounds_for_all([]) == []


def test_bounds_for_all_accepts_precomputed_footprints(calculator):
    # The acquire() path computes footprints once (for the shape filter) and
    # hands the surviving arrays back into bounds_for_all. Feeding the same
    # footprints in must yield identical BoundedTileInfo to recomputing them.
    names = ["C00075f6b.png", "C00075f6c.png", "C00075f6d.png"]
    tiles = [_tile(n) for n in names]

    footprints = calculator.projected_footprints(tiles)
    via_footprints = calculator.bounds_for_all(tiles, footprints=footprints)
    recomputed = calculator.bounds_for_all(tiles)

    assert len(via_footprints) == len(recomputed) == 3
    for a, b in zip(via_footprints, recomputed):
        assert a.tile == b.tile
        assert a.bounds.coords_center.get_lat() == pytest.approx(
            b.bounds.coords_center.get_lat()
        )
        assert a.bounds.coords_center.get_lon() == pytest.approx(
            b.bounds.coords_center.get_lon()
        )


def test_bounds_for_all_footprints_length_mismatch_raises(calculator):
    tiles = [_tile("C00075f6b.png")]
    fps = calculator.projected_footprints(tiles)
    # footprints for zero tiles, tiles for one -> mismatch
    empty = TileFootprints(
        west_x=fps.west_x[:0],
        east_x=fps.east_x[:0],
        south_y=fps.south_y[:0],
        north_y=fps.north_y[:0],
    )
    with pytest.raises(ValueError):
        calculator.bounds_for_all(tiles, footprints=empty)


def test_bounds_for_unknown_level_raises(calculator):
    bad = TileInfo(
        path=Path("L99/R00000000/C00000000.png"),
        layer_number=99,
        row_number=0,
        col_number=0,
    )
    with pytest.raises(ValueError):
        calculator.bounds_for(bad)
