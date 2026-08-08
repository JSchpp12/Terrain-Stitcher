"""Tests for TileSchemeInfo (the cache_xml.py replacement).

cache_xml.py (ArcGisCacheInfo / CacheSpatialReference) was replaced by the
source-independent TileSchemeInfo in arcgis.tile_scheme, parsed from the same
ArcGIS Pro conf.xml. These tests pin the conf.xml -> TileSchemeInfo parsing
against the perryville fixture (24 Web Mercator levels 0..23).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from terrain_stitcher.arcgis.tile_scheme import (
    LevelOfDetailInfo,
    TileSchemeInfo,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "arcgis_cache"
CONF_XML = FIXTURE_DIR / "conf.xml"


@pytest.fixture
def scheme() -> TileSchemeInfo:
    return TileSchemeInfo.from_arcgis_conf_xml(str(CONF_XML))


# --- LevelOfDetailInfo dataclass -------------------------------------------


def test_level_of_detail_info_fields():
    lod = LevelOfDetailInfo(
        level_id=3,
        scale=73957190.948944,
        resolution=19567.879240999901,
    )
    assert lod.level_id == 3
    assert lod.scale == 73957190.948944
    assert lod.resolution == 19567.879240999901


# --- TileSchemeInfo.from_arcgis_conf_xml (perryville fixture) ---------------


def test_from_xml_returns_dataclass(scheme):
    assert isinstance(scheme, TileSchemeInfo)


def test_spatial_reference(scheme):
    # conf.xml declares WKID 102100 / LatestWKID 3857 (Web Mercator).
    assert scheme.wkid == 102100
    assert scheme.latest_wkid == 3857
    assert scheme.pyproj_epsg == 3857


def test_tile_origin(scheme):
    assert scheme.tile_origin_x == pytest.approx(-20037508.342787)
    assert scheme.tile_origin_y == pytest.approx(20037508.342787)


def test_tile_grid(scheme):
    assert scheme.tile_cols == 256
    assert scheme.tile_rows == 256
    assert scheme.dpi == 96
    assert scheme.precise_dpi == 96


def test_image_and_storage_info(scheme):
    # TileSchemeInfo lowercases the cache tile format.
    assert scheme.cache_tile_format == "png"
    assert scheme.storage_format == "esriMapCacheStorageModeExploded"


def test_levels_count_and_type(scheme):
    assert len(scheme.levels) == 24
    assert all(isinstance(l, LevelOfDetailInfo) for l in scheme.levels)


def test_levels_ordered_by_id(scheme):
    assert [l.level_id for l in scheme.levels] == list(range(24))


def test_levels_scale_and_resolution_monotonic(scheme):
    scales = [l.scale for l in scheme.levels]
    resolutions = [l.resolution for l in scheme.levels]
    assert scales == sorted(scales, reverse=True)
    assert resolutions == sorted(resolutions, reverse=True)


def test_first_and_last_level_values(scheme):
    first = scheme.levels[0]
    assert first.level_id == 0
    assert first.scale == pytest.approx(591657527.591555)
    assert first.resolution == pytest.approx(156543.033928)

    last = scheme.levels[23]
    assert last.level_id == 23
    assert last.resolution == pytest.approx(0.01866138385297604)


def test_fixture_conf_declares_level_matching_real_tiles(scheme):
    """The fixture tiles live under _alllayers/L23, so conf.xml must declare
    level 23 - keeps the fixture cache internally consistent."""
    assert any(l.level_id == 23 for l in scheme.levels)


def test_level_by_id_returns_matching_level(scheme):
    lvl = scheme.level_by_id(23)
    assert lvl.level_id == 23
    with pytest.raises(KeyError):
        scheme.level_by_id(99)


# --- error paths -----------------------------------------------------------


def test_from_xml_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        TileSchemeInfo.from_arcgis_conf_xml(str(tmp_path / "does_not_exist.xml"))


def test_from_xml_missing_tile_cache_info_raises(tmp_path):
    bad = tmp_path / "bad.xml"
    bad.write_text("<?xml version='1.0'?><CacheInfo></CacheInfo>", encoding="utf-8")
    with pytest.raises(ValueError):
        TileSchemeInfo.from_arcgis_conf_xml(str(bad))