"""Integration tests for the folded ArcGIS cache import path
(import_from_arcgis_dir).

This path merges the old gather-ortho --source arcgis -> prep-ortho ->
stitch-ortho chain into one pass over the cache: the cache native (row, col)
grid is composited straight from the source PNGs in _alllayers, with no
per-tile zip / unzip round-trip. The tests exercise it against the real
ArcGIS exploded-cache fixture (3 PNGs under _alllayers/L23/R0027e3a0/).
"""
from __future__ import annotations

import concurrent.futures
import json
import os
from pathlib import Path

import pytest
from PIL import Image as pImage

from terrain_stitcher.functions.ArcGisImporter import import_from_arcgis_dir

FIXTURE = Path(__file__).parent / "fixtures" / "arcgis_cache"
ALL_LAYERS = FIXTURE / "_alllayers"
CONF_XML = FIXTURE / "conf.xml"


def test_stitch_arcgis_import_dimension_one_emits_one_image_per_tile(tmp_path):
    out = tmp_path / "out"
    groups = import_from_arcgis_dir(
        shape_file=None,
        cache_dir=str(FIXTURE),
        output_dir=str(out),
        dimension=1,
        workers=1,
    )
    # 3 tiles, dimension=1 -> 3 one-tile groups.
    assert len(groups) == 3

    manifest = json.loads((out / "height_info.json").read_text())
    assert len(manifest["images"]) == 3
    assert {img["name"] for img in manifest["images"]} == {
        "gathered_r0_c0",
        "gathered_r0_c1",
        "gathered_r0_c2",
    }
    # Every manifest entry has a matching, decodable PNG on disk.
    for img in manifest["images"]:
        png = out / (img["name"] + ".png")
        assert png.is_file()
        with pImage.open(png) as im:
            assert im.size[0] > 0 and im.size[1] > 0


def test_stitch_one_group_strip_path_writes_group_png(tmp_path, monkeypatch):
    """The strip path must publish to the group's PNG, not the output dir.

    Regression test for the Windows failure where ``_save_canvas`` wrote
    ``<output_dir>.tmp`` and then tried to replace ``<output_dir>`` itself.
    The output-dir path is a directory, so that rename fails with WinError 5.
    """
    import terrain_stitcher.functions.ArcGisImporter as arcgis_importer

    class _Group:
        origin = (2, 3)
        cell_width = 1
        cell_height = 1

        def canvas_meta(self):
            return "RGB", 1, 1

        def get_traversal(self):
            return {(0, 0)}

    class _Pool:
        def submit(self, function, spec):
            future = concurrent.futures.Future()
            future.set_result(pImage.new("RGB", (1, 1)))
            return future

    out = tmp_path / "out"
    out.mkdir()

    monkeypatch.setattr(
        arcgis_importer,
        "_plan_strips",
        lambda group, mode, workers: [(0, "strip-spec")],
    )

    arcgis_importer._stitch_one_group(
        _Group(), str(out), _Pool(), num_workers=2
    )

    expected = out / "gathered_r2_c3.png"
    assert expected.is_file()
    assert not (out / "gathered_r2_c3.png.tmp").exists()
    with pImage.open(expected) as image:
        assert image.size == (1, 1)


def test_stitch_arcgis_import_dimension_two_partitions_windows(tmp_path):
    out = tmp_path / "out"
    groups = import_from_arcgis_dir(
        shape_file=None,
        cache_dir=str(FIXTURE),
        output_dir=str(out),
        dimension=2,
        workers=1,
    )
    # 3 tiles in one row: window cols 0-1 (2 tiles) + window cols 2-3 (1 tile).
    assert len(groups) == 2
    assert sorted(g.n_tiles for g in groups) == [1, 2]


def test_stitch_arcgis_import_lod_selects_requested_level(tmp_path):
    out = tmp_path / "out"
    groups = import_from_arcgis_dir(
        shape_file=None,
        cache_dir=str(FIXTURE),
        output_dir=str(out),
        dimension=1,
        lod=23,
        workers=1,
    )
    assert len(groups) == 3


def test_stitch_arcgis_import_lod_missing_raises(tmp_path):
    out = tmp_path / "out"
    with pytest.raises(ValueError, match="No surviving tiles at LOD 99"):
        import_from_arcgis_dir(
            shape_file=None,
            cache_dir=str(FIXTURE),
            output_dir=str(out),
            dimension=1,
            lod=99,
            workers=1,
        )
