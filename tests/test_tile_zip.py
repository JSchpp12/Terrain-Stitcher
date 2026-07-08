from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from terrain_stitcher.arcgis.cache_xml import ArcGisCacheInfo
from terrain_stitcher.arcgis.tile_bounds import TileBoundsCalculator
from terrain_stitcher.arcgis.tile_info import BoundedTileInfo, TileInfo
from terrain_stitcher.arcgis.tile_zip import (
    compress_tile_to_zip,
    process_tile,
    process_tiles,
    tile_chunk_name,
    write_tile_sidecar,
)
from terrain_stitcher.sources import Bounds, ImageDataWriter

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "arcgis_cache"
CONF_XML = FIXTURE_DIR / "conf.xml"
ALL_LAYERS = FIXTURE_DIR / "_alllayers"


def _real_tile_path(name: str) -> Path:
    return ALL_LAYERS / "L23" / "R0027e3a0" / name


def _tile(name: str) -> TileInfo:
    return TileInfo.from_path(_real_tile_path(name), ALL_LAYERS)


@pytest.fixture
def calculator():
    return TileBoundsCalculator(ArcGisCacheInfo.from_xml(str(CONF_XML)))


# --- tile_chunk_name -------------------------------------------------------


def test_tile_chunk_name_format_round_trips_arcgis_encoding():
    tile = TileInfo(
        path=Path("L23/R0027e3a0/C00075f6b.png"),
        layer_number=23,
        row_number=int("0027e3a0", 16),
        col_number=int("00075f6b", 16),
    )
    assert tile_chunk_name(tile) == "L23_R0027e3a0_C00075f6b"


# --- compress_tile_to_zip -------------------------------------------------


def test_compress_real_fixture_tile_creates_valid_zip(tmp_path):
    src = _real_tile_path("C00075f6b.png")
    tile = TileInfo.from_path(src, ALL_LAYERS)
    out = tmp_path / "out"
    zip_path = compress_tile_to_zip(tile, ALL_LAYERS, out)

    assert zip_path == out / "L23_R0027e3a0_C00075f6b.zip"
    assert zip_path.is_file()

    with zipfile.ZipFile(zip_path) as zf:
        assert zf.testzip() is None
        assert zf.namelist() == ["C00075f6b.png"]
        assert zf.read("C00075f6b.png") == src.read_bytes()


def test_compress_creates_nested_output_dir(tmp_path):
    src = _real_tile_path("C00075f6b.png")
    tile = TileInfo.from_path(src, ALL_LAYERS)
    out = tmp_path / "nested" / "out"
    assert compress_tile_to_zip(tile, ALL_LAYERS, out).is_file()


def test_compress_each_real_fixture_tile(tmp_path):
    names = ["C00075f6b.png", "C00075f6c.png", "C00075f6d.png"]
    for name in names:
        src = _real_tile_path(name)
        tile = TileInfo.from_path(src, ALL_LAYERS)
        zip_path = compress_tile_to_zip(tile, ALL_LAYERS, tmp_path)
        assert zip_path.is_file()
        assert zip_path.name == f"{tile_chunk_name(tile)}.zip"


def test_compress_missing_source_raises(tmp_path):
    tile = TileInfo(
        path=Path("L23/R0027e3a0/Cdeadbeef.png"),
        layer_number=23,
        row_number=0,
        col_number=0,
    )
    with pytest.raises(FileNotFoundError):
        compress_tile_to_zip(tile, ALL_LAYERS, tmp_path)


# --- write_tile_sidecar ----------------------------------------------------


def test_write_tile_sidecar_creates_valid_json(tmp_path, calculator):
    tile = _tile("C00075f6b.png")
    bounds = calculator.bounds_for(tile)
    out = tmp_path / "out"

    sidecar = write_tile_sidecar(tile, bounds, out)

    chunk = tile_chunk_name(tile)
    assert sidecar == out / f"{chunk}.json"
    assert sidecar.is_file()

    data = json.loads(sidecar.read_text())
    assert data["imageFileName"] == f"{chunk}.zip"
    writer = ImageDataWriter.fromDict(data)
    assert writer.imageFileName == f"{chunk}.zip"
    assert isinstance(writer.bounds, Bounds)
    assert writer.bounds.isValid()
    # bounds corners are present
    assert set(data["bounds"].keys()) == {
        "center",
        "northEast",
        "southEast",
        "southWest",
        "northWest",
    }


# --- process_tile ----------------------------------------------------------


def test_process_tile_creates_zip_and_sidecar(tmp_path, calculator):
    tile = _tile("C00075f6b.png")
    bounds = calculator.bounds_for(tile)
    out = tmp_path / "out"

    bounded = BoundedTileInfo(tile=tile, bounds=bounds)
    sidecar = process_tile(bounded, ALL_LAYERS, out)

    chunk = tile_chunk_name(tile)
    assert sidecar == out / f"{chunk}.json"
    assert (out / f"{chunk}.zip").is_file()
    assert (out / f"{chunk}.json").is_file()
    with zipfile.ZipFile(out / f"{chunk}.zip") as zf:
        assert zf.namelist() == ["C00075f6b.png"]


# --- process_tiles (parallel) ----------------------------------------------


def test_process_tiles_parallel_creates_all(tmp_path, calculator):
    names = ["C00075f6b.png", "C00075f6c.png", "C00075f6d.png"]
    tiles = [_tile(n) for n in names]
    bounded_tiles = calculator.bounds_for_all(tiles)
    out = tmp_path / "out"

    sidecars = process_tiles(bounded_tiles, ALL_LAYERS, out, num_workers=3)

    assert len(sidecars) == 3
    for n in names:
        chunk = tile_chunk_name(_tile(n))
        assert (out / f"{chunk}.zip").is_file()
        assert (out / f"{chunk}.json").is_file()
    for sc in sidecars:
        data = json.loads(sc.read_text())
        assert "bounds" in data
        assert data["imageFileName"].endswith(".zip")


def test_process_tiles_empty(tmp_path):
    assert process_tiles([], ALL_LAYERS, tmp_path) == []


# --- end-to-end via ArcGisProAcquisitionSource.acquire ----------------------


def test_acquire_writes_zips_and_sidecars(tmp_path):
    from terrain_stitcher.arcgis.acquisition import ArcGisProAcquisitionSource

    src = ArcGisProAcquisitionSource.from_cache_dir(str(FIXTURE_DIR))
    out = tmp_path / "out"

    src.acquire(None, str(out), str(FIXTURE_DIR))

    names = ["C00075f6b.png", "C00075f6c.png", "C00075f6d.png"]
    for n in names:
        chunk = tile_chunk_name(_tile(n))
        assert (out / f"{chunk}.zip").is_file()
        assert (out / f"{chunk}.json").is_file()
