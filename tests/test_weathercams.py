"""Tests for the minimal FAA WeatherCams batch workflow."""

import argparse
import json
import sys
from pathlib import Path

from terrain_stitcher.weathercams.client import WeathercamSite
from terrain_stitcher.weathercams.ledger import (
    export_weathercam_ledger,
    import_weathercam_ledger,
    load_weathercam_ledger,
    mark_site_complete,
    mark_site_pending,
    summarize_weathercam_ledger,
    synchronize_weathercam_ledger,
)
from terrain_stitcher.weathercams.pipeline import (
    ProcessTerrainOptions,
    run_weathercams,
)
from terrain_stitcher.weathercams.shapes import prepare_weathercam_shapes


def _site(site_id, lat=55.0, lon=-131.0):
    return WeathercamSite(
        site_id=site_id,
        latitude=lat,
        longitude=lon,
        state="AK",
    )


def _write_shape(directory: Path, site_id: int):
    path = directory / f"{site_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "boundsType": "POINT",
                "center": {"lat": "55.0", "lon": "-131.0"},
                "view_distance": 5.0,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_ledger_sync_preserves_completed_sites(tmp_path):
    ledger_path = tmp_path / "ledger.json"

    first = synchronize_weathercam_ledger(ledger_path, [133, 134, 135])
    assert (first.added_sites, first.preserved_sites) == (3, 0)

    mark_site_complete(ledger_path, 133)
    second = synchronize_weathercam_ledger(ledger_path, [133, 134, 136])

    assert (second.added_sites, second.preserved_sites) == (1, 2)
    ledger = load_weathercam_ledger(ledger_path)
    assert ledger == {"133": True, "134": False, "135": False, "136": False}


def test_export_and_import_only_apply_completed_sites(tmp_path):
    source_ledger = tmp_path / "source.json"
    destination_ledger = tmp_path / "destination.json"
    export_path = tmp_path / "export.json"

    synchronize_weathercam_ledger(source_ledger, [133, 134, 135])
    synchronize_weathercam_ledger(destination_ledger, [133, 134, 135])
    mark_site_complete(source_ledger, 133)
    mark_site_complete(source_ledger, 135)

    exported_count = export_weathercam_ledger(source_ledger, export_path)
    assert exported_count == 2
    assert load_weathercam_ledger(export_path) == {"133": True, "135": True}

    result = import_weathercam_ledger(
        destination_ledger,
        load_weathercam_ledger(export_path),
    )
    assert result.marked_complete == 2
    assert result.already_complete == 0
    assert load_weathercam_ledger(destination_ledger) == {
        "133": True,
        "134": False,
        "135": True,
    }


def test_prepare_shapes_only_for_pending_sites(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    shape_dir = tmp_path / "shapes"
    synchronize_weathercam_ledger(ledger_path, [133, 134])
    mark_site_complete(ledger_path, 134)

    result = prepare_weathercam_shapes(
        [_site(133), _site(134)],
        ledger_path=ledger_path,
        shape_dir=shape_dir,
        view_distance=5.0,
    )

    assert result.created_site_ids == [133]
    assert result.skipped_site_ids == [134]
    assert (shape_dir / "133.json").is_file()
    assert not (shape_dir / "134.json").exists()

    shape = json.loads((shape_dir / "133.json").read_text(encoding="utf-8"))
    assert shape["boundsType"] == "POINT"
    assert shape["center"]["lat"] == "55.0"
    assert shape["center"]["lon"] == "-131.0"
    assert shape["view_distance"] == 5.0


def test_prepare_shapes_limit_does_not_count_completed_sites(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    shape_dir = tmp_path / "shapes"
    synchronize_weathercam_ledger(ledger_path, [133, 134, 135])
    mark_site_complete(ledger_path, 133)

    result = prepare_weathercam_shapes(
        [_site(133), _site(134), _site(135)],
        ledger_path=ledger_path,
        shape_dir=shape_dir,
        view_distance=5.0,
        limit=1,
    )

    assert result.created_site_ids == [134]
    assert result.skipped_site_ids == [133]
    assert (shape_dir / "134.json").is_file()
    assert not (shape_dir / "135.json").exists()


def _fake_process_terrain(**kwargs):
    name = kwargs["name"]
    output_root = Path(kwargs["output"])
    tier_dir = output_root / f"{name}_{kwargs['lod']}"
    tier_dir.mkdir(parents=True, exist_ok=True)
    (tier_dir / "height_info.json").write_text(
        json.dumps({"images": [{"name": "gathered_r0_c0"}]}),
        encoding="utf-8",
    )
    (tier_dir / "gathered_r0_c0.png").write_bytes(b"fake png")


def test_run_weathercams_processes_pending_site_and_marks_complete(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    shape_dir = tmp_path / "shapes"
    output_root = tmp_path / "outputs"

    synchronize_weathercam_ledger(ledger_path, [133, 134])
    mark_site_complete(ledger_path, 134)
    _write_shape(shape_dir, 133)

    result = run_weathercams(
        ledger_path=ledger_path,
        shape_dir=shape_dir,
        output_root=output_root,
        options=ProcessTerrainOptions(dimension=2, lod=12),
        process_terrain=_fake_process_terrain,
    )

    assert result.processed_site_ids == [133]
    assert result.failed_site_ids == []
    assert load_weathercam_ledger(ledger_path) == {"133": True, "134": True}
    assert (
        output_root / "weathercam_133_12" / "height_info.json"
    ).is_file()
    assert (
        output_root / "weathercam_133_12" / "gathered_r0_c0.png"
    ).is_file()


def test_manual_complete_and_retry(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    synchronize_weathercam_ledger(ledger_path, [133])

    mark_site_complete(ledger_path, 133)
    summary = summarize_weathercam_ledger(load_weathercam_ledger(ledger_path))
    assert summary.complete_sites == 1

    mark_site_pending(ledger_path, 133)
    summary = summarize_weathercam_ledger(load_weathercam_ledger(ledger_path))
    assert summary.pending_sites == 1


def test_cli_dispatches_weathercams_sync(monkeypatch, tmp_path):
    import terrain_stitcher.cli as cli_mod
    import terrain_stitcher.weathercams.cli as weathercams_cli

    ledger_path = tmp_path / "ledger.json"

    def fake_fetch_weathercam_sites(state, *, timeout):
        assert state == "AK"
        assert timeout == 12
        return [_site(133), _site(134)]

    monkeypatch.setattr(
        weathercams_cli,
        "fetch_weathercam_sites",
        fake_fetch_weathercam_sites,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "terrain_stitcher",
            "weathercams-sync",
            "--ledger",
            str(ledger_path),
            "--timeout",
            "12",
        ],
    )

    cli_mod.main()

    assert load_weathercam_ledger(ledger_path) == {"133": False, "134": False}


class _FakeApiResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeApiSession:
    def __init__(self, payload):
        self.payload = payload
        self.request_kwargs = None

    def get(self, url, *, headers, timeout):
        self.request_kwargs = {
            "url": url,
            "headers": headers,
            "timeout": timeout,
        }
        return _FakeApiResponse(self.payload)


def test_client_fetches_and_filters_alaska_sites():
    from terrain_stitcher.weathercams.client import fetch_weathercam_sites

    session = _FakeApiSession(
        {
            "payload": [
                {
                    "siteId": 133,
                    "latitude": "55.25",
                    "longitude": "-131.75",
                    "state": "AK",
                },
                {
                    "siteId": 200,
                    "latitude": "40.0",
                    "longitude": "-100.0",
                    "state": "NE",
                },
            ]
        }
    )

    sites = fetch_weathercam_sites("AK", session=session, timeout=17)

    assert sites == [
        WeathercamSite(
            site_id=133,
            latitude=55.25,
            longitude=-131.75,
            state="AK",
        )
    ]
    assert session.request_kwargs["url"] == "https://weathercams.faa.gov/api/sites"
    assert session.request_kwargs["timeout"] == 17


def test_cli_prepare_limit_creates_one_shape(tmp_path, monkeypatch):
    import terrain_stitcher.weathercams.cli as weathercams_cli

    ledger_path = tmp_path / "ledger.json"
    shape_dir = tmp_path / "shapes"

    synchronize_weathercam_ledger(ledger_path, [133, 134, 135])
    monkeypatch.setattr(
        weathercams_cli,
        "fetch_weathercam_sites",
        lambda state, *, timeout: [
            _site(133),
            _site(134),
            _site(135),
        ],
    )

    args = argparse.Namespace(
        command="weathercams-prepare",
        ledger=ledger_path,
        shape_dir=shape_dir,
        view_distance=5.0,
        limit=1,
        state="AK",
        timeout=30,
    )

    assert weathercams_cli.handle_weathercams_command(args)
    assert (shape_dir / "133.json").is_file()
    assert not (shape_dir / "134.json").exists()
    assert not (shape_dir / "135.json").exists()
