from __future__ import annotations

from pathlib import Path

import pytest

from terrain_stitcher.arcgis.acquisition import ArcGisProAcquisitionSource
from terrain_stitcher.arcgis.cache_xml import (
    ArcGisCacheInfo,
    CacheSpatialReference,
    LevelOfDetailInfo,
)
from terrain_stitcher.sources.acquisition import (
    AcquisitionSource,
    get_acquisition_source,
)

# The fixture is a coherent perryville ArcGIS cache: conf.xml plus a small
# _alllayers/L23/R0027e3a0/ tile tree (3 real PNGs).
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "arcgis_cache"
CONF_XML = FIXTURE_DIR / "conf.xml"
ALL_LAYERS = FIXTURE_DIR / "_alllayers"


@pytest.fixture
def cache_info() -> ArcGisCacheInfo:
    return ArcGisCacheInfo.from_xml(str(CONF_XML))


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


# --- CacheSpatialReference enum --------------------------------------------


def test_cache_spatial_reference_enum_single_member():
    assert CacheSpatialReference.WGS_1984_Web_Mercator_Auxiliary_Sphere.value == 3857


# --- ArcGisCacheInfo.from_xml (perryville fixture) -------------------------


def test_from_xml_returns_dataclass(cache_info):
    assert isinstance(cache_info, ArcGisCacheInfo)


def test_spatial_reference(cache_info):
    assert (
        cache_info.spatial_reference
        is CacheSpatialReference.WGS_1984_Web_Mercator_Auxiliary_Sphere
    )


def test_tile_origin(cache_info):
    assert cache_info.tile_origin_x == pytest.approx(-20037508.342787)
    assert cache_info.tile_origin_y == pytest.approx(20037508.342787)


def test_tile_grid(cache_info):
    assert cache_info.tile_cols == 256
    assert cache_info.tile_rows == 256
    assert cache_info.dpi == 96
    assert cache_info.precise_dpi == 96


def test_image_and_storage_info(cache_info):
    assert cache_info.cache_tile_format == "PNG"
    assert cache_info.storage_format == "esriMapCacheStorageModeExploded"


def test_levels_count_and_type(cache_info):
    assert len(cache_info.levels) == 24
    assert all(isinstance(l, LevelOfDetailInfo) for l in cache_info.levels)


def test_levels_ordered_by_id(cache_info):
    assert [l.level_id for l in cache_info.levels] == list(range(24))


def test_levels_scale_and_resolution_monotonic(cache_info):
    scales = [l.scale for l in cache_info.levels]
    resolutions = [l.resolution for l in cache_info.levels]
    assert scales == sorted(scales, reverse=True)
    assert resolutions == sorted(resolutions, reverse=True)


def test_first_and_last_level_values(cache_info):
    first = cache_info.levels[0]
    assert first.level_id == 0
    assert first.scale == pytest.approx(591657527.591555)
    assert first.resolution == pytest.approx(156543.033928)

    last = cache_info.levels[23]
    assert last.level_id == 23
    assert last.resolution == pytest.approx(0.01866138385297604)


def test_fixture_conf_declares_level_matching_real_tiles(cache_info):
    """The fixture tiles live under _alllayers/L23, so conf.xml must declare
    level 23 - keeps the fixture cache internally consistent."""
    assert any(l.level_id == 23 for l in cache_info.levels)


# --- error paths -----------------------------------------------------------


def test_from_xml_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        ArcGisCacheInfo.from_xml(str(tmp_path / "does_not_exist.xml"))


def test_from_xml_missing_tile_cache_info_raises(tmp_path):
    bad = tmp_path / "bad.xml"
    bad.write_text("<?xml version='1.0'?><CacheInfo></CacheInfo>", encoding="utf-8")
    with pytest.raises(ValueError):
        ArcGisCacheInfo.from_xml(str(bad))


def test_from_xml_unsupported_spatial_reference_raises(tmp_path):
    bad = tmp_path / "unsupported.xml"
    bad.write_text(
        '<?xml version="1.0"?><CacheInfo><TileCacheInfo>'
        '<SpatialReference><WKT>GEOGCS["GCS_WGS_1984"]</WKT>'
        "<WKID>4326</WKID></SpatialReference>"
        "</TileCacheInfo></CacheInfo>",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        ArcGisCacheInfo.from_xml(str(bad))


# --- ArcGisProAcquisitionSource --------------------------------------------


def test_acquisition_source_is_an_acquisition_source():
    src = ArcGisProAcquisitionSource(str(CONF_XML))
    assert isinstance(src, AcquisitionSource)


def test_acquisition_source_from_xml_path():
    src = ArcGisProAcquisitionSource(str(CONF_XML))
    assert isinstance(src.cache_info, ArcGisCacheInfo)
    assert len(src.levels) == 24
    assert all(isinstance(l, LevelOfDetailInfo) for l in src.levels)


def test_acquisition_source_from_cache_dir():
    src = ArcGisProAcquisitionSource.from_cache_dir(str(FIXTURE_DIR))
    assert len(src.levels) == 24


def test_acquisition_source_from_cache_dir_missing_conf(tmp_path):
    with pytest.raises(FileNotFoundError):
        ArcGisProAcquisitionSource.from_cache_dir(str(tmp_path))


def test_factory_constructs_empty_arcgis_source():
    src = get_acquisition_source("arcgis")
    assert isinstance(src, ArcGisProAcquisitionSource)
    assert src.levels == []
    assert src.cache_info is None


def test_acquire_loads_cache_then_raises_not_implemented():
    src = get_acquisition_source("arcgis")
    with pytest.raises(NotImplementedError) as exc:
        src.acquire("shape.json", "out", str(FIXTURE_DIR))
    assert "24 levels loaded" in str(exc.value)
    # cache metadata is loaded before the not-implemented raise
    assert len(src.levels) == 24


def test_acquire_without_input_dir_raises_value_error():
    src = get_acquisition_source("arcgis")
    with pytest.raises(ValueError):
        src.acquire("shape.json", "out", None)


def test_usgs_factory_path_unaffected():
    from terrain_stitcher.usgs_acquisition import UsgsAcquisitionSource

    assert isinstance(get_acquisition_source("usgs"), UsgsAcquisitionSource)
