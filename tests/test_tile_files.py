from __future__ import annotations

from pathlib import Path

import pytest

from terrain_stitcher.arcgis.tile_files import (
    ALL_LAYERS_DIR,
    SUPPORTED_TILE_FORMAT,
    TILE_EXTENSION,
    all_layers_path,
    gather_tile_files,
)

REAL_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "arcgis_cache"
REAL_ALL_LAYERS = REAL_FIXTURE_DIR / ALL_LAYERS_DIR


def _make_alllayers(base: Path) -> Path:
    al = base / ALL_LAYERS_DIR
    (al / "L00" / "00000000").mkdir(parents=True)
    (al / "L01" / "00000010").mkdir(parents=True)
    (al / "L02" / "00000020").mkdir(parents=True)
    # PNG tiles (the ones we want)
    (al / "L00" / "00000000" / "00000000.png").write_bytes(b"")
    (al / "L00" / "00000000" / "00000001.png").write_bytes(b"")
    (al / "L01" / "00000010" / "00000005.png").write_bytes(b"")
    # mixed-case extension should still match
    (al / "L02" / "00000020" / "Tile.PNG").write_bytes(b"")
    # non-tile files that must be ignored
    (al / "L01" / "00000010" / "notatile.txt").write_bytes(b"")
    (al / "L01" / "00000010" / "00000009.jpg").write_bytes(b"")
    return al


def test_supported_tile_format_constants():
    assert SUPPORTED_TILE_FORMAT == "PNG"
    assert TILE_EXTENSION == ".png"


def test_gather_tile_files_collects_only_pngs(tmp_path):
    al = _make_alllayers(tmp_path)
    files = gather_tile_files(str(al), "PNG")
    assert len(files) == 4
    assert all(f.lower().endswith(".png") for f in files)


def test_gather_tile_files_format_is_case_insensitive(tmp_path):
    al = _make_alllayers(tmp_path)
    assert len(gather_tile_files(str(al), "png")) == 4
    assert len(gather_tile_files(str(al), "pNg")) == 4


def test_gather_tile_files_unsupported_format_raises(tmp_path):
    al = _make_alllayers(tmp_path)
    with pytest.raises(ValueError):
        gather_tile_files(str(al), "JPEG")


def test_gather_tile_files_nonexistent_dir_returns_empty(tmp_path):
    assert gather_tile_files(str(tmp_path / "does_not_exist"), "PNG") == []


def test_gather_tile_files_preserves_full_paths(tmp_path):
    al = _make_alllayers(tmp_path)
    files = gather_tile_files(str(al), "PNG")
    expected = al / "L00" / "00000000" / "00000000.png"
    assert any(Path(f) == expected for f in files)


def test_all_layers_path():
    p = all_layers_path(str(Path("C") / "cache"))
    assert p.endswith(ALL_LAYERS_DIR)


def test_gather_tile_files_against_real_fixture():
    """Exercises gather_tile_files against real ArcGIS exploded-cache tiles
    checked into the repo (3 PNGs under _alllayers/L##/R<hex>/C<hex>.png)."""
    files = gather_tile_files(str(REAL_ALL_LAYERS), "PNG")
    assert len(files) == 3
    assert all(f.lower().endswith(".png") for f in files)
    # Real ArcGIS naming convention: <level L##>/<row R<hex>>/<col C<hex>.png>
    for f in files:
        rel = Path(f).relative_to(REAL_ALL_LAYERS)
        assert rel.parts[0].startswith("L")
        assert rel.parts[1].startswith("R")
        assert rel.parts[2].startswith("C")
        assert rel.parts[2].lower().endswith(".png")
