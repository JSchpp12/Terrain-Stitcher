"""CLI argument forwarding tests.

The `--service-index` option must reach the downloaders; an earlier bug
dropped it for `download-arcgis`, so even when the user selected an index the
ambiguous-services error still fired and the process exited. These tests
pin the wiring from cli.main -> downloader entrypoints.
"""

import json

import pytest

import terrain_stitcher.cli as cli_mod
from terrain_stitcher.functions import OrthoDownloader, ElevationDownloader


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
    return p


def _run_cli(monkeypatch, argv):
    """Invoke cli.main() with a synthesized argv, returning the captured
    kwargs of the (patched) entrypoint call."""
    import sys

    monkeypatch.setattr(sys, "argv", argv)
    captured = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        raise SystemExit(0)

    return captured, _capture


def test_cli_download_arcgis_forwards_service_index(monkeypatch, tmp_path):
    shape = _shape_file(tmp_path)
    captured, _capture = _run_cli(monkeypatch, ["prog"])
    monkeypatch.setattr(cli_mod, "main_arcgis_downloader", _capture)

    argv = [
        "prog",
        "download-arcgis",
        "-s",
        str(shape),
        "-o",
        str(tmp_path / "out"),
        "--lod",
        "18",
        "--service-index",
        "2",
    ]
    monkeypatch.setattr("sys.argv", argv)
    with pytest.raises(SystemExit):
        cli_mod.main()

    assert captured.get("service_index") == 2


def test_cli_download_arcgis_service_index_defaults_none(monkeypatch, tmp_path):
    shape = _shape_file(tmp_path)
    captured = {}
    monkeypatch.setattr(
        cli_mod, "main_arcgis_downloader", lambda **kw: captured.update(kw) or None
    )

    argv = [
        "prog",
        "download-arcgis",
        "-s",
        str(shape),
        "-o",
        str(tmp_path / "out"),
        "--lod",
        "18",
    ]
    monkeypatch.setattr("sys.argv", argv)
    cli_mod.main()

    assert captured.get("service_index") is None


def test_cli_download_elevation_forwards_service_index(monkeypatch, tmp_path):
    shape = _shape_file(tmp_path)
    captured = {}
    monkeypatch.setattr(
        cli_mod, "main_elevation", lambda **kw: captured.update(kw) or None
    )

    argv = [
        "prog",
        "download-elevation",
        "-s",
        str(shape),
        "-o",
        str(tmp_path / "elev.tif"),
        "--service-index",
        "1",
    ]
    monkeypatch.setattr("sys.argv", argv)
    cli_mod.main()

    assert captured.get("service_index") == 1
