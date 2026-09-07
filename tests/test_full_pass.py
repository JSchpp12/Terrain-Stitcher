"""Tests for the single-LOD `process-terrain` orchestration command."""

import json
import types

import pytest

import terrain_stitcher.cli as cli_mod
import terrain_stitcher.functions.FullPass as fullpass_mod
from terrain_stitcher.functions import FullPass


def _shape_file(tmp_path, lon=-149.0, lat=61.0, radius=5.0):
    path = tmp_path / "Shape.json"
    path.write_text(
        json.dumps(
            {
                "boundsType": "POINT",
                "center": {"x": lon, "y": lat},
                "view_distance": radius,
            }
        ),
        encoding="utf-8",
    )
    return str(path)


def _run_cli(monkeypatch, argv):
    monkeypatch.setattr("sys.argv", argv)
    cli_mod.main()


def _patch_pipeline(monkeypatch):
    downloads = []
    gathers = []
    elevations = []

    monkeypatch.setattr(
        fullpass_mod,
        "main_arcgis_downloader",
        lambda **kw: downloads.append(kw),
    )

    def fake_gather(**kw):
        gathers.append(kw)

    monkeypatch.setattr(
        fullpass_mod,
        "main_ortho_arcgis_import_from_download",
        fake_gather,
    )
    monkeypatch.setattr(
        fullpass_mod,
        "main_elevation",
        lambda **kw: elevations.append(kw),
    )
    return downloads, gathers, elevations


def _patch_rmtree(monkeypatch):
    removed = []
    monkeypatch.setattr(
        fullpass_mod,
        "shutil",
        types.SimpleNamespace(
            rmtree=lambda path, ignore_errors=False: removed.append(path)
        ),
    )
    return removed


def test_cli_process_terrain_requires_dimension_ge_2(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "sys.argv",
        [
            "prog",
            "process-terrain",
            "--name",
            "perry",
            "-s",
            _shape_file(tmp_path),
            "-d",
            "1",
            "--lod",
            "16",
        ],
    )
    with pytest.raises(SystemExit):
        cli_mod.main()


def test_cli_process_terrain_dispatches_single_lod(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(
        cli_mod,
        "main_process_terrain",
        lambda **kw: captured.update(kw) or None,
    )

    _run_cli(
        monkeypatch,
        [
            "prog",
            "process-terrain",
            "--name",
            "perry",
            "-s",
            _shape_file(tmp_path),
            "-o",
            str(tmp_path / "out"),
            "-d",
            "8",
            "--lod",
            "16",
            "--with-elevation",
            "--service-index",
            "2",
        ],
    )

    assert captured["name"] == "perry"
    assert captured["shape_file"] == _shape_file(tmp_path)
    assert captured["output"] == str(tmp_path / "out")
    assert captured["dimension"] == 8
    assert captured["lod"] == 16
    assert captured["with_elevation"] is True
    assert captured["keep_tiles"] is False
    assert captured["scale_factor"] == 1.0
    assert captured["workers"] == 32
    assert captured["processes"] == 32
    assert captured["gather_workers"] is None
    assert captured["chunk_px"] == 256
    assert captured["timeout"] == 30
    assert captured["resampling"] == "lanczos"
    assert captured["service_index"] == 2


def test_process_terrain_downloads_and_gathers_one_lod(monkeypatch, tmp_path):
    downloads, gathers, _ = _patch_pipeline(monkeypatch)
    removed = _patch_rmtree(monkeypatch)

    FullPass.main_process_terrain(
        name="perry",
        shape_file=_shape_file(tmp_path),
        output=str(tmp_path),
        dimension=4,
        lod=16,
    )

    assert len(downloads) == 1
    assert downloads[0]["lod"] == 16
    assert downloads[0]["outdir"] == str(tmp_path / "perry_tiles")

    assert len(gathers) == 1
    assert gathers[0]["min_level"] == 16
    assert gathers[0]["max_level"] == 16
    assert gathers[0]["download_dir"] == str(tmp_path / "perry_tiles")
    assert gathers[0]["output_dir"] == str(tmp_path / "perry_16")
    assert gathers[0]["dimension"] == 4

    assert str(tmp_path / "perry_tiles") in removed


def test_process_terrain_keep_tiles(monkeypatch, tmp_path):
    _, gathers, _ = _patch_pipeline(monkeypatch)
    removed = _patch_rmtree(monkeypatch)

    FullPass.main_process_terrain(
        name="perry",
        shape_file=_shape_file(tmp_path),
        output=str(tmp_path),
        dimension=4,
        lod=16,
        keep_tiles=True,
    )

    assert removed == []
    assert len(gathers) == 1


def test_process_terrain_elevation_passed_to_gather(monkeypatch, tmp_path):
    _, gathers, elevations = _patch_pipeline(monkeypatch)
    _patch_rmtree(monkeypatch)

    FullPass.main_process_terrain(
        name="perry",
        shape_file=_shape_file(tmp_path),
        output=str(tmp_path),
        dimension=4,
        lod=16,
        with_elevation=True,
    )

    assert len(elevations) == 1
    assert (
        elevations[0]["outdir"]
        == str(tmp_path / "perry_elevation" / "elevation_merged.tif")
    )
    assert elevations[0]["shape_file"] == _shape_file(tmp_path)

    assert len(gathers) == 1
    assert gathers[0]["elevation_data_dir"] == str(tmp_path / "perry_elevation")


def test_process_terrain_passthrough_options(monkeypatch, tmp_path):
    downloads, gathers, _ = _patch_pipeline(monkeypatch)
    _patch_rmtree(monkeypatch)

    FullPass.main_process_terrain(
        name="perry",
        shape_file=_shape_file(tmp_path),
        output=str(tmp_path),
        dimension=2,
        lod=16,
        scale_factor=0.5,
        workers=4,
        processes=8,
        gather_workers=3,
        chunk_px=512,
        timeout=60,
        resampling="cubic",
        service_index=1,
    )

    assert downloads[0]["num_workers"] == 4
    assert downloads[0]["chunk_px"] == 512
    assert downloads[0]["timeout"] == 60
    assert downloads[0]["resampling"] == "cubic"
    assert downloads[0]["processes"] == 8
    assert downloads[0]["service_index"] == 1

    assert gathers[0]["scale_factor"] == 0.5
    assert gathers[0]["workers"] == 3


def test_process_terrain_dimension_lt_2_raises(monkeypatch, tmp_path):
    _patch_pipeline(monkeypatch)
    with pytest.raises(ValueError, match="dimension must be >= 2"):
        FullPass.main_process_terrain(
            name="perry",
            shape_file=_shape_file(tmp_path),
            output=str(tmp_path),
            dimension=1,
            lod=16,
        )


def test_process_terrain_lod_must_be_positive(monkeypatch, tmp_path):
    _patch_pipeline(monkeypatch)
    with pytest.raises(ValueError, match="lod must be greater than zero"):
        FullPass.main_process_terrain(
            name="perry",
            shape_file=_shape_file(tmp_path),
            output=str(tmp_path),
            dimension=4,
            lod=0,
        )


def test_process_terrain_retries_missing_lod_with_dedicated_download(
    monkeypatch, tmp_path
):
    downloads, gathers, _ = _patch_pipeline(monkeypatch)
    removed = _patch_rmtree(monkeypatch)

    shared_tiles = str(tmp_path / "perry_tiles")
    fallback_tiles = str(tmp_path / "perry_16_tiles")

    def fake_gather(**kw):
        if kw["download_dir"] == shared_tiles:
            raise ValueError(
                "No surviving tiles at LOD 16; available LODs: []"
            )
        gathers.append(kw)

    monkeypatch.setattr(
        fullpass_mod,
        "main_ortho_arcgis_import_from_download",
        fake_gather,
    )

    FullPass.main_process_terrain(
        name="perry",
        shape_file=_shape_file(tmp_path),
        output=str(tmp_path),
        dimension=4,
        lod=16,
    )

    assert [download["lod"] for download in downloads] == [16, 16]
    assert [download["outdir"] for download in downloads] == [
        shared_tiles,
        fallback_tiles,
    ]
    assert len(gathers) == 1
    assert gathers[0]["download_dir"] == fallback_tiles
    assert gathers[0]["min_level"] == 16
    assert gathers[0]["max_level"] == 16

    assert shared_tiles in removed
    assert fallback_tiles in removed


def test_process_terrain_non_missing_lod_error_propagates(monkeypatch, tmp_path):
    _patch_pipeline(monkeypatch)
    _patch_rmtree(monkeypatch)

    def fake_gather(**kw):
        raise ValueError("scale_factor must be in (0.0, 1.0]")

    monkeypatch.setattr(
        fullpass_mod,
        "main_ortho_arcgis_import_from_download",
        fake_gather,
    )

    with pytest.raises(ValueError, match="scale_factor"):
        FullPass.main_process_terrain(
            name="perry",
            shape_file=_shape_file(tmp_path),
            output=str(tmp_path),
            dimension=4,
            lod=16,
        )
