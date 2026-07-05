from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from terrain_stitcher.arcgis.tile_info import TileInfo
from terrain_stitcher.arcgis.tile_zip import compress_tile_to_zip, tile_chunk_name

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "arcgis_cache"
ALL_LAYERS = FIXTURE_DIR / "_alllayers"


def _real_tile_path(name: str) -> Path:
    return ALL_LAYERS / "L23" / "R0027e3a0" / name


def test_tile_chunk_name_format_round_trips_arcgis_encoding():
    tile = TileInfo(
        path=Path("L23/R0027e3a0/C00075f6b.png"),
        layer_number=23,
        row_number=int("0027e3a0", 16),
        col_number=int("00075f6b", 16),
    )
    assert tile_chunk_name(tile) == "L23_R0027e3a0_C00075f6b"


def test_compress_real_fixture_tile_creates_valid_zip(tmp_path):
    src = _real_tile_path("C00075f6b.png")
    tile = TileInfo.from_path(src, ALL_LAYERS)

    out = tmp_path / "out"
    zip_path = compress_tile_to_zip(tile, ALL_LAYERS, out)

    assert zip_path == out / "L23_R0027e3a0_C00075f6b.zip"
    assert zip_path.is_file()

    with zipfile.ZipFile(zip_path) as zf:
        assert zf.testzip() is None  # archive is valid
        assert zf.namelist() == ["C00075f6b.png"]
        # archived bytes match the source file exactly
        assert zf.read("C00075f6b.png") == src.read_bytes()


def test_compress_creates_nested_output_dir(tmp_path):
    src = _real_tile_path("C00075f6b.png")
    tile = TileInfo.from_path(src, ALL_LAYERS)
    out = tmp_path / "nested" / "out"
    zip_path = compress_tile_to_zip(tile, ALL_LAYERS, out)
    assert zip_path.is_file()


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
