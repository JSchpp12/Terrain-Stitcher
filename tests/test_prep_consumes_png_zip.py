"""Tests for the ortho prep stage (OrthoPrep) and its zip-in-memory handling.

Covers ImageExtensionType / compareExtension, find_file over an extracted dir,
and the end-to-end processOrthoImage path that reads a PNG straight from a
zip archive in memory (no disk extraction) and writes the output PNG +
sidecar JSON. The acquisition side (compress_tile_to_zip) feeds it a real
fixture tile zipped under its original filename.
"""
from __future__ import annotations

from pathlib import Path

from terrain_stitcher.arcgis.tile_info import TileInfo
from terrain_stitcher.arcgis.tile_zip import compress_tile_to_zip
from terrain_stitcher.common import Bounds, World_Coordinates
from terrain_stitcher.functions.OrthoPrep import (
    ImageExtensionType,
    OrthoTask,
    compareExtension,
    processOrthoImage,
)
from terrain_stitcher.sources import ImageDataWriter
from terrain_stitcher.util import find_file

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "arcgis_cache"
ALL_LAYERS = FIXTURE_DIR / "_alllayers"


def _real_tile_path(name: str) -> Path:
    return ALL_LAYERS / "L23" / "R0027e3a0" / name


# --- ImageExtensionType enum ----------------------------------------------


def test_image_extension_type_values():
    assert ImageExtensionType.TIF.value == ".tif"
    assert ImageExtensionType.TIFF.value == ".tiff"
    assert ImageExtensionType.PNG.value == ".png"


# --- compareExtension ------------------------------------------------------


def test_compare_extension_accepts_enum_class_png_tif_tiff():
    for ext in (".png", ".tif", ".tiff"):
        assert compareExtension(f"foo{ext}", ImageExtensionType) is True


def test_compare_extension_accepts_single_enum_member():
    assert compareExtension("foo.png", ImageExtensionType.PNG) is True
    assert compareExtension("foo.tif", ImageExtensionType.PNG) is False


def test_compare_extension_rejects_non_image():
    assert compareExtension("foo.txt", ImageExtensionType) is False
    assert compareExtension("foo", ImageExtensionType) is False


def test_compare_extension_is_case_insensitive():
    assert compareExtension("Foo.PNG", ImageExtensionType) is True
    assert compareExtension("Foo.TIF", ImageExtensionType) is True


def test_compare_extension_single_string_key_still_works():
    # backwards compatible with the old single-string usage
    assert compareExtension("foo.tif", ".tif") is True
    assert compareExtension("foo.png", ".tif") is False


# --- find_file with ImageExtensionType --------------------------------------


def test_find_file_locates_png_in_extracted_dir(tmp_path):
    extracted = tmp_path / "chunk"
    extracted.mkdir()
    (extracted / "C00075f6b.png").write_bytes(b"")
    found = find_file(str(extracted), ImageExtensionType, compareExtension)
    assert found is not None and Path(found).name == "C00075f6b.png"


def test_find_file_locates_tif_in_extracted_dir(tmp_path):
    extracted = tmp_path / "chunk"
    extracted.mkdir()
    (extracted / "scene.tif").write_bytes(b"")
    found = find_file(str(extracted), ImageExtensionType, compareExtension)
    assert found is not None and Path(found).name == "scene.tif"


def test_find_file_returns_none_when_only_non_image(tmp_path):
    extracted = tmp_path / "chunk"
    extracted.mkdir()
    (extracted / "notes.txt").write_bytes(b"")
    assert find_file(str(extracted), ImageExtensionType, compareExtension) is None


# --- integration: arcgis png-in-zip flows through processOrthoImage ----------


def _write_sidecar(zip_path: Path) -> Path:
    """Write a valid ImageDataWriter sidecar JSON next to a zip (same base name)."""
    c = World_Coordinates(lat="40.0", lon="-100.0")
    bounds = Bounds(c, c, c, c, c)  # NE, SE, SW, NW, center
    chunk = zip_path.stem
    image_file_name = f"{chunk}.zip"
    ImageDataWriter(bounds, imageFileName=image_file_name).writeFileContents(
        str(zip_path.parent), image_file_name, f"{chunk}.json"
    )
    return zip_path.parent / f"{chunk}.json"


def test_processOrthoImage_reads_png_from_zip_in_memory(tmp_path):
    # 1. acquisition: compress a real fixture tile into a zip
    src = _real_tile_path("C00075f6b.png")
    tile = TileInfo.from_path(src, ALL_LAYERS)
    zip_path = compress_tile_to_zip(tile, ALL_LAYERS, tmp_path / "acq")
    assert zip_path.is_file()

    # 2. write a valid metadata sidecar next to the zip (same base name)
    sidecar = _write_sidecar(zip_path)
    assert sidecar.is_file()

    # 3. prep: processOrthoImage reads the PNG from the zip in memory and
    #    writes the output PNG + sidecar JSON to the output dir
    prep_out = tmp_path / "prep"
    prep_out.mkdir()
    chunk_name, bounds_json = processOrthoImage(
        OrthoTask(str(zip_path), str(prep_out), 1.0)
    )

    assert chunk_name == zip_path.stem
    out_png = prep_out / f"{zip_path.stem}.png"
    assert out_png.is_file()
    assert out_png.stat().st_size > 0
    # sidecar json is also written
    assert (prep_out / f"{zip_path.stem}.json").is_file()
    # bounds are returned for the aggregate manifest
    assert "center" in bounds_json