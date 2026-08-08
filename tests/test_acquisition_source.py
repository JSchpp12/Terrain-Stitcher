"""Tests for ArcGisProAcquisitionSource construction (not acquire()).

The legacy acquire() (zip + sidecar per tile) path is unused production code --
the folded import_from_arcgis_dir / import_from_download paths replaced it --
so these tests only cover the cheap construction surface: building a source
from a cache dir (conf.xml -> TileSchemeInfo), the exposed cache_info/levels,
and the missing-conf.xml error. The per-tile zip/sidecar primitives it used
are covered in test_tile_zip.py.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from terrain_stitcher.arcgis.acquisition_source import ArcGisProAcquisitionSource
from terrain_stitcher.arcgis.tile_info import TileInfo
from terrain_stitcher.sources.acquisition import AcquisitionSource

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "arcgis_cache"
CONF_XML = FIXTURE_DIR / "conf.xml"


def test_acquisition_source_is_an_acquisition_source():
    src = ArcGisProAcquisitionSource.from_cache_dir(str(FIXTURE_DIR), TileInfo.from_paths)
    assert isinstance(src, AcquisitionSource)


def test_acquisition_source_from_cache_dir_exposes_scheme():
    src = ArcGisProAcquisitionSource.from_cache_dir(str(FIXTURE_DIR), TileInfo.from_paths)
    # cache_info is the parsed TileSchemeInfo; conf.xml declares 24 levels.
    assert len(src.cache_info.levels) == 24
    assert all(l.level_id < 24 for l in src.cache_info.levels)


def test_acquisition_source_from_cache_dir_missing_conf_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        ArcGisProAcquisitionSource.from_cache_dir(str(tmp_path), TileInfo.from_paths)


def test_from_tile_scheme_builds_web_mercator_pyramid():
    # The from-download (gdal2tiles XYZ) path builds a synthetic Web Mercator
    # scheme over the requested zoom range rather than reading conf.xml.
    src = ArcGisProAcquisitionSource.from_tile_scheme(
        min_level=17, max_level=19, extract_function=TileInfo.from_xyz_paths
    )
    assert [l.level_id for l in src.cache_info.levels] == [17, 18, 19]
    assert src.cache_info.latest_wkid == 3857

# --- acquire() end-to-end (zip + sidecar per tile) -------------------------
# acquire() is the legacy per-tile zip/sidecar path. It was broken by a stale
# worker-state binding (acquisition_source imported _WORKER_CACHE as a name,
# which stayed None after the worker's _init_worker rebound the module global);
# that is now fixed, so the end-to-end path is covered again.
import zipfile

from terrain_stitcher.arcgis.tile_zip import tile_chunk_name

ALL_LAYERS = FIXTURE_DIR / "_alllayers"


def _real_tile_path(name):
    return ALL_LAYERS / "L23" / "R0027e3a0" / name


def _tile(name):
    return TileInfo.from_path(_real_tile_path(name), ALL_LAYERS)


def test_acquire_writes_zips_and_sidecars(tmp_path):
    src = ArcGisProAcquisitionSource.from_cache_dir(str(FIXTURE_DIR), TileInfo.from_paths)
    out = tmp_path / "out"

    src.acquire(None, str(out), str(FIXTURE_DIR))

    names = ["C00075f6b.png", "C00075f6c.png", "C00075f6d.png"]
    for n in names:
        chunk = tile_chunk_name(_tile(n))
        assert (out / f"{chunk}.zip").is_file()
        assert (out / f"{chunk}.json").is_file()
        with zipfile.ZipFile(out / f"{chunk}.zip") as zf:
            assert zf.namelist() == [n]
