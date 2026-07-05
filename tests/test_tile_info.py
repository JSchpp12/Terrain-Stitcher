from __future__ import annotations

from pathlib import Path

import pytest

from terrain_stitcher.arcgis.tile_info import TileInfo

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "arcgis_cache"
ALL_LAYERS = FIXTURE_DIR / "_alllayers"


# --- real fixture tiles ----------------------------------------------------


def _real_tile(name: str) -> Path:
    return ALL_LAYERS / "L23" / "R0027e3a0" / name


def test_from_path_real_fixture_tile():
    tile = TileInfo.from_path(_real_tile("C00075f6b.png"), ALL_LAYERS)
    assert isinstance(tile, TileInfo)
    assert tile.layer_number == 23
    assert tile.row_number == int("0027e3a0", 16)
    assert tile.col_number == int("00075f6b", 16)
    assert tile.path.parts == ("L23", "R0027e3a0", "C00075f6b.png")


def test_from_path_real_fixture_each_of_the_three_tiles():
    names = ["C00075f6b.png", "C00075f6c.png", "C00075f6d.png"]
    for name in names:
        tile = TileInfo.from_path(_real_tile(name), ALL_LAYERS)
        assert tile.layer_number == 23
        assert tile.row_number == int("0027e3a0", 16)
        assert tile.col_number == int(name.split(".")[0][1:], 16)
        assert tile.path.parts == ("L23", "R0027e3a0", name)


# --- synthetic structure ---------------------------------------------------


def _make_tile(base: Path, level: str, row: str, col: str) -> Path:
    p = base / level / row / f"{col}.png"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"")
    return p


def test_from_path_synthetic_decimal_layer_hex_row_col(tmp_path):
    al = tmp_path / "_alllayers"
    p = _make_tile(al, "L05", "R00000010", "C000000ff")
    tile = TileInfo.from_path(p, al)
    assert tile.layer_number == 5
    assert tile.row_number == 0x10  # 16
    assert tile.col_number == 0xFF  # 255
    assert tile.path.parts == ("L05", "R00000010", "C000000ff.png")


def test_from_path_uppercase_hex_is_accepted(tmp_path):
    al = tmp_path / "_alllayers"
    p = _make_tile(al, "L03", "R0000000A", "C0000000B")
    tile = TileInfo.from_path(p, al)
    assert tile.row_number == 0xA
    assert tile.col_number == 0xB


def test_from_path_two_digit_level(tmp_path):
    al = tmp_path / "_alllayers"
    p = _make_tile(al, "L23", "R00000000", "C00000000")
    assert TileInfo.from_path(p, al).layer_number == 23


# --- error paths -----------------------------------------------------------


def test_from_path_not_under_base_raises(tmp_path):
    al = tmp_path / "_alllayers"
    other = tmp_path / "elsewhere" / "L23" / "R00000000"
    other.mkdir(parents=True)
    f = other / "C00000000.png"
    f.write_bytes(b"")
    with pytest.raises(ValueError):
        TileInfo.from_path(f, al)


def test_from_path_wrong_structure_raises(tmp_path):
    al = tmp_path / "_alllayers"
    # tile sitting directly under _alllayers (no level/row folders)
    f = al / "C00000000.png"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"")
    with pytest.raises(ValueError):
        TileInfo.from_path(f, al)


def test_from_path_bad_level_prefix_raises(tmp_path):
    al = tmp_path / "_alllayers"
    p = _make_tile(al, "23", "R00000000", "C00000000")  # missing 'L'
    with pytest.raises(ValueError):
        TileInfo.from_path(p, al)


def test_from_path_bad_row_prefix_raises(tmp_path):
    al = tmp_path / "_alllayers"
    p = _make_tile(al, "L23", "00000000", "C00000000")  # missing 'R'
    with pytest.raises(ValueError):
        TileInfo.from_path(p, al)


def test_from_path_bad_col_prefix_raises(tmp_path):
    al = tmp_path / "_alllayers"
    p = _make_tile(al, "L23", "R00000000", "00000000")  # missing 'C'
    with pytest.raises(ValueError):
        TileInfo.from_path(p, al)


def test_from_path_non_hex_row_raises(tmp_path):
    al = tmp_path / "_alllayers"
    p = _make_tile(al, "L23", "R000000zz", "C00000000")  # 'zz' is not hex
    with pytest.raises(ValueError):
        TileInfo.from_path(p, al)


def test_from_path_non_hex_col_raises(tmp_path):
    al = tmp_path / "_alllayers"
    p = _make_tile(al, "L23", "R00000000", "C000000zz")  # 'zz' is not hex
    with pytest.raises(ValueError):
        TileInfo.from_path(p, al)
