from __future__ import annotations

import zipfile
from pathlib import Path

from terrain_stitcher.arcgis.tile_info import TileInfo
from terrain_stitcher.arcgis.tile_zip import compress_tile_to_zip
from terrain_stitcher.functions.OrthoPrep import (
    CopyInfo,
    ImageExtensionType,
    compareExtension,
    copyOrthoImage,
)
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


# --- integration: arcgis png-in-zip flows through copyOrthoImage ------------


class _StubInfo:
    def toJSON(self):
        return {"bounds": {}, "imageFileName": "stub.zip"}


def test_copyOrthoImage_consumes_arcgis_png_zip(tmp_path):
    # 1. acquisition: compress a real fixture tile into a zip
    src = _real_tile_path("C00075f6b.png")
    tile = TileInfo.from_path(src, ALL_LAYERS)
    zip_path = compress_tile_to_zip(tile, ALL_LAYERS, tmp_path / "acq")
    assert zip_path.is_file()

    # 2. prep: extract the zip (as prep's extractAll would) into a tmp dir
    extracted = tmp_path / "extracted" / "chunk"
    extracted.mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extracted)

    # 3. prep: copyOrthoImage now finds the .png inside and writes chunk.png
    prep_out = tmp_path / "prep"
    prep_out.mkdir()
    cd = CopyInfo(str(extracted), str(prep_out), "chunk", 1.0, _StubInfo())
    result = copyOrthoImage(cd)

    assert Path(result) == prep_out / "chunk.png"
    assert (prep_out / "chunk.png").is_file()
    assert (prep_out / "chunk.png").stat().st_size > 0
    # sidecar json is also written
    assert (prep_out / "chunk.json").is_file()
