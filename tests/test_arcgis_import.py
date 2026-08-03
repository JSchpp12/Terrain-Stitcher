"""Integration tests for the folded ArcGIS import path (stitch_arcgis_import).

This path merges the old gather-ortho --source arcgis -> prep-ortho ->
stitch-ortho chain into one pass over the cache: the cache native (row, col)
grid is composited straight from the source PNGs in _alllayers, with no
per-tile zip / unzip round-trip. The tests exercise it against the real
ArcGIS exploded-cache fixture (3 PNGs under _alllayers/L23/R0027e3a0/).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from PIL import Image as pImage

from terrain_stitcher.functions.ArcGisImporter import (
    import_from_arcgis,
)

FIXTURE = Path(__file__).parent / "fixtures" / "arcgis_cache"
ALL_LAYERS = FIXTURE / "_alllayers"
CONF_XML = FIXTURE / "conf.xml"


def test_stitch_arcgis_import_dimension_one_emits_one_image_per_tile(tmp_path):
    out = tmp_path / "out"
    groups = import_from_arcgis(
        shape_file=None,
        cache_dir=str(FIXTURE),
        output_dir=str(out),
        dimension=1,
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


def test_stitch_arcgis_import_dimension_two_partitions_windows(tmp_path):
    out = tmp_path / "out"
    groups = import_from_arcgis(
        shape_file=None,
        cache_dir=str(FIXTURE),
        output_dir=str(out),
        dimension=2,
    )
    # 3 tiles in one row: window cols 0-1 (2 tiles) + window cols 2-3 (1 tile).
    assert len(groups) == 2
    assert sorted(g.n_tiles for g in groups) == [1, 2]


def test_stitch_arcgis_import_lod_selects_requested_level(tmp_path):
    out = tmp_path / "out"
    groups = import_from_arcgis(
        shape_file=None,
        cache_dir=str(FIXTURE),
        output_dir=str(out),
        dimension=1,
        lod=23,
    )
    assert len(groups) == 3


def test_stitch_arcgis_import_lod_missing_raises(tmp_path):
    out = tmp_path / "out"
    with pytest.raises(ValueError, match="No surviving tiles at LOD 99"):
        import_from_arcgis(
            shape_file=None,
            cache_dir=str(FIXTURE),
            output_dir=str(out),
            dimension=1,
            lod=99,
        )


def test_stitch_arcgis_import_resume_is_idempotent(tmp_path):
    out = tmp_path / "out"
    import_from_arcgis(
        shape_file=None, cache_dir=str(FIXTURE), output_dir=str(out), dimension=1
    )
    first = {p.name: p.stat().st_size for p in out.iterdir() if p.suffix == ".png"}
    # Re-run with resume=True: existing groups are skipped, output unchanged.
    import_from_arcgis(
        shape_file=None,
        cache_dir=str(FIXTURE),
        output_dir=str(out),
        dimension=1,
        resume=True,
    )
    second = {p.name: p.stat().st_size for p in out.iterdir() if p.suffix == ".png"}
    assert first == second
