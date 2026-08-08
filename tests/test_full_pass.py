"""Tests for the `process-terrain` full-pass orchestration command.

The command is pure orchestration: it calls the already-wired
download-arcgis / gather-ortho --from-download / download-elevation
entrypoints in sequence. These tests monkeypatch those entrypoints (on the
FullPass module, where main_process_terrain looks them up) and pin the
sequence of calls: one download at the max requested LOD, one gather per
tier into <name>_<lod>, mandatory dimension >= 2, optional elevation, and
the per-LOD download fallback when a tier's LOD is missing from the shared
pyramid.
"""

import json
import types

import pytest

import terrain_stitcher.cli as cli_mod
import terrain_stitcher.functions.FullPass as fullpass_mod
from terrain_stitcher.functions import FullPass


def _shape_file(tmp_path, lon=-149.0, lat=61.0, radius=5.0):
    p = tmp_path / "Shape.json"
    p.write_text(
        json.dumps(
            {
                "boundsType": "POINT",
                "center": {"x": lon, "y": lat},
                "view_distance": radius,
            }
        )
    )
    return str(p)


def _run_cli(monkeypatch, argv):
    monkeypatch.setattr("sys.argv", argv)
    cli_mod.main()


# ---------------------------------------------------------------------------
# CLI dispatch: argument wiring + dimension guard
# ---------------------------------------------------------------------------


def test_cli_process_terrain_requires_dimension_ge_2(monkeypatch, tmp_path):
    shape = _shape_file(tmp_path)
    argv = [
        "prog",
        "process-terrain",
        "--name",
        "perry",
        "-s",
        shape,
        "-d",
        "1",
    ]
    monkeypatch.setattr("sys.argv", argv)
    with pytest.raises(SystemExit):
        cli_mod.main()


def test_cli_process_terrain_dispatches_default(monkeypatch, tmp_path):
    shape = _shape_file(tmp_path)
    captured = {}
    monkeypatch.setattr(
        cli_mod, "main_process_terrain", lambda **kw: captured.update(kw) or None
    )
    argv = [
        "prog",
        "process-terrain",
        "--name",
        "perry",
        "-s",
        shape,
        "-o",
        str(tmp_path / "out"),
        "-d",
        "8",
        "--ultra",
        "--with-elevation",
        "--service-index",
        "2",
    ]
    _run_cli(monkeypatch, argv)

    assert captured["name"] == "perry"
    assert captured["shape_file"] == shape
    assert captured["output"] == str(tmp_path / "out")
    assert captured["dimension"] == 8
    assert captured["ultra"] is True
    assert captured["with_elevation"] is True
    assert captured["keep_tiles"] is False
    assert captured["resume"] is False
    assert captured["scale_factor"] == 1.0
    assert captured["workers"] == 32
    assert captured["processes"] == 32
    assert captured["gather_workers"] is None
    assert captured["chunk_px"] == 256
    assert captured["timeout"] == 30
    assert captured["resampling"] == "lanczos"
    assert captured["service_index"] == 2


# ---------------------------------------------------------------------------
# Orchestration: download once, gather per tier
# ---------------------------------------------------------------------------


def _patch_pipeline(monkeypatch):
    downloads = []
    gathers = []
    elevations = []

    def fake_download(**kw):
        downloads.append(kw)

    def fake_gather(**kw):
        gathers.append(kw)

    def fake_elevation(**kw):
        elevations.append(kw)

    monkeypatch.setattr(fullpass_mod, "main_arcgis_downloader", fake_download)
    monkeypatch.setattr(
        fullpass_mod, "main_ortho_arcgis_import_from_download", fake_gather
    )
    monkeypatch.setattr(fullpass_mod, "main_elevation", fake_elevation)
    return downloads, gathers, elevations


def _patch_rmtree(monkeypatch):
    removed = []
    fake_shutil = types.SimpleNamespace(
        rmtree=lambda path, ignore_errors=False: removed.append(path)
    )
    monkeypatch.setattr(fullpass_mod, "shutil", fake_shutil)
    return removed


def test_process_terrain_two_tiers_single_download(monkeypatch, tmp_path):
    downloads, gathers, _ = _patch_pipeline(monkeypatch)
    removed = _patch_rmtree(monkeypatch)

    FullPass.main_process_terrain(
        name="perry",
        shape_file=_shape_file(tmp_path),
        output=str(tmp_path),
        dimension=4,
    )

    # one download at the max tier LOD (18 when no ultra)
    assert len(downloads) == 1
    assert downloads[0]["lod"] == 18
    assert downloads[0]["outdir"] == str(tmp_path / "perry_tiles")

    # one gather per tier, into <name>_<lod>, sharing the pyramid
    assert [g["min_level"] for g in gathers] == [17, 18]
    assert [g["max_level"] for g in gathers] == [18, 18]
    assert [g["output_dir"] for g in gathers] == [
        str(tmp_path / "perry_17"),
        str(tmp_path / "perry_18"),
    ]
    assert all(g["download_dir"] == str(tmp_path / "perry_tiles") for g in gathers)
    assert all(g["dimension"] == 4 for g in gathers)
    assert all(g["elevation_data_dir"] is None for g in gathers)

    # intermediate pyramid deleted by default
    assert str(tmp_path / "perry_tiles") in removed


def test_process_terrain_ultra_three_tiers(monkeypatch, tmp_path):
    downloads, gathers, _ = _patch_pipeline(monkeypatch)
    _patch_rmtree(monkeypatch)

    FullPass.main_process_terrain(
        name="perry",
        shape_file=_shape_file(tmp_path),
        output=str(tmp_path),
        dimension=4,
        ultra=True,
    )

    assert len(downloads) == 1
    assert downloads[0]["lod"] == 19
    assert [g["min_level"] for g in gathers] == [17, 18, 19]
    assert all(g["max_level"] == 19 for g in gathers)
    assert [g["output_dir"] for g in gathers] == [
        str(tmp_path / "perry_17"),
        str(tmp_path / "perry_18"),
        str(tmp_path / "perry_19"),
    ]


def test_process_terrain_keep_tiles_keeps_pyramid(monkeypatch, tmp_path):
    downloads, gathers, _ = _patch_pipeline(monkeypatch)
    removed = _patch_rmtree(monkeypatch)

    FullPass.main_process_terrain(
        name="perry",
        shape_file=_shape_file(tmp_path),
        output=str(tmp_path),
        dimension=4,
        keep_tiles=True,
    )

    assert removed == []
    assert len(gathers) == 2


def test_process_terrain_elevation_passed_to_each_gather(monkeypatch, tmp_path):
    downloads, gathers, elevations = _patch_pipeline(monkeypatch)
    _patch_rmtree(monkeypatch)

    FullPass.main_process_terrain(
        name="perry",
        shape_file=_shape_file(tmp_path),
        output=str(tmp_path),
        dimension=4,
        with_elevation=True,
    )

    # elevation downloaded once into <name>_elevation/elevation_merged.tif
    assert len(elevations) == 1
    assert elevations[0]["outdir"] == str(tmp_path / "perry_elevation" / "elevation_merged.tif")
    assert elevations[0]["shape_file"] == _shape_file(tmp_path)

    # every tier gather fed the elevation dir as -e
    assert len(gathers) == 2
    for g in gathers:
        assert g["elevation_data_dir"] == str(tmp_path / "perry_elevation")


def test_process_terrain_passthrough_options(monkeypatch, tmp_path):
    downloads, gathers, _ = _patch_pipeline(monkeypatch)
    _patch_rmtree(monkeypatch)

    FullPass.main_process_terrain(
        name="perry",
        shape_file=_shape_file(tmp_path),
        output=str(tmp_path),
        dimension=2,
        scale_factor=0.5,
        resume=True,
        workers=4,
        processes=8,
        gather_workers=3,
        chunk_px=512,
        timeout=60,
        resampling="cubic",
        service_index=1,
    )

    d = downloads[0]
    assert d["num_workers"] == 4
    assert d["chunk_px"] == 512
    assert d["timeout"] == 60
    assert d["resampling"] == "cubic"
    assert d["processes"] == 8
    assert d["service_index"] == 1

    g = gathers[0]
    assert g["scale_factor"] == 0.5
    assert g["resume"] is True
    assert g["workers"] == 3


def test_process_terrain_dimension_lt_2_raises(monkeypatch, tmp_path):
    _patch_pipeline(monkeypatch)
    with pytest.raises(ValueError, match="dimension must be >= 2"):
        FullPass.main_process_terrain(
            name="perry",
            shape_file=_shape_file(tmp_path),
            output=str(tmp_path),
            dimension=1,
        )


# ---------------------------------------------------------------------------
# Fallback: a tier's LOD missing from the shared pyramid triggers a
# dedicated per-LOD download (robust to gdal2tiles -z semantics).
# ---------------------------------------------------------------------------


def test_process_terrain_fallback_per_lod_download(monkeypatch, tmp_path):
    downloads, gathers, _ = _patch_pipeline(monkeypatch)
    removed = _patch_rmtree(monkeypatch)

    shared_tiles = str(tmp_path / "perry_tiles")
    fallback_tiles_17 = str(tmp_path / "perry_17_tiles")

    state = {"fired_17_shared": False}

    def fake_gather(**kw):
        # First attempt: LOD 17 against the shared pyramid -> LOD missing.
        if kw["min_level"] == 17 and kw["download_dir"] == shared_tiles:
            state["fired_17_shared"] = True
            raise ValueError(
                "No surviving tiles at LOD 17; available LODs: [18]"
            )
        gathers.append(kw)

    monkeypatch.setattr(
        fullpass_mod, "main_ortho_arcgis_import_from_download", fake_gather
    )

    FullPass.main_process_terrain(
        name="perry",
        shape_file=_shape_file(tmp_path),
        output=str(tmp_path),
        dimension=4,
    )

    # 1 shared download (LOD 18) + 1 fallback download (LOD 17)
    assert len(downloads) == 2
    assert downloads[0]["lod"] == 18
    assert downloads[0]["outdir"] == shared_tiles
    assert downloads[1]["lod"] == 17
    assert downloads[1]["outdir"] == fallback_tiles_17

    # gathers: 17 (fallback, from perry_17_tiles) + 18 (shared)
    assert [g["min_level"] for g in gathers] == [17, 18]
    assert gathers[0]["download_dir"] == fallback_tiles_17
    assert gathers[0]["max_level"] == 17
    assert gathers[1]["download_dir"] == shared_tiles
    assert gathers[1]["max_level"] == 18

    # both pyramids cleaned up by default
    assert shared_tiles in removed
    assert fallback_tiles_17 in removed


def test_process_terrain_non_missing_lod_error_propagates(monkeypatch, tmp_path):
    _patch_pipeline(monkeypatch)
    _patch_rmtree(monkeypatch)

    def fake_gather(**kw):
        raise ValueError("scale_factor must be in (0.0, 1.0]")

    monkeypatch.setattr(
        fullpass_mod, "main_ortho_arcgis_import_from_download", fake_gather
    )

    with pytest.raises(ValueError, match="scale_factor"):
        FullPass.main_process_terrain(
            name="perry",
            shape_file=_shape_file(tmp_path),
            output=str(tmp_path),
            dimension=4,
        )