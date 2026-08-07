import json

import pytest

from terrain_stitcher.arcgis.services import ImageryService
from terrain_stitcher.functions import (
    DownloaderBase,
    ElevationDownloader,
    OrthoDownloader,
)
import requests


def _elev_service(key="3dep", kinds=("elevation",)):
    return ImageryService(
        key=key,
        label=key,
        base_url=f"https://example/{key}/ImageServer/exportImage",
        native_pixel_size_m=10.0,
        srs=3857,
        coverage=(10.0, -100.0, 60.0, -50.0),
        kinds=list(kinds),
    )


def _shape(tmp_path, lon=-90.0, lat=40.0, radius=5.0):
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


class _FakeResp:
    status_code = 200
    headers = {"Content-Type": "image/tiff"}
    content = b"II*\x00\x08\x00\x00\x00"
    text = ""


class _FakeSession:
    def __init__(self):
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(dict(params))
        return _FakeResp()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_download_elevation_fetches_f32_tiff(monkeypatch, tmp_path):
    """download_elevation must request format=tiff + pixelType=F32, write the
    already-georeferenced chunk TIFFs directly (no georeference pass), and
    produce the merged GeoTIFF via build_mosaic."""
    service = _elev_service()
    monkeypatch.setattr(ElevationDownloader, "load_services", lambda: {"3dep": service})

    fake = _FakeSession()
    monkeypatch.setattr(requests, "Session", lambda: fake)

    # Guard: the direct-write path must NOT run a georeference pass.
    def _boom(*a, **k):
        raise AssertionError("georeference_chunk should not be called for elevation")

    monkeypatch.setattr(DownloaderBase, "georeference_chunk", _boom)

    out = tmp_path / "elevation_merged.tif"

    calls = []

    def fake_build_mosaic(chunk_paths, td):
        calls.append(("build_mosaic", len(chunk_paths)))
        vrt = td / "mosaic.vrt"
        vrt.write_text("vrt")
        return vrt

    monkeypatch.setattr(ElevationDownloader, "build_mosaic", fake_build_mosaic)

    def fake_translate(vrt, out_path):
        calls.append(("translate", str(out_path)))
        out_path.write_bytes(b"geotiff")
        return out_path

    monkeypatch.setattr(ElevationDownloader, "_translate_to_geotiff", fake_translate)

    ElevationDownloader.download_elevation(
        shapefile_path=str(_shape(tmp_path)),
        outdir=str(out),
        res=30.0,
        num_workers=1,
    )

    assert out.is_file()
    assert fake.calls, "no exportImage requests were made"
    for params in fake.calls:
        assert params["format"] == "tiff"
        assert params["pixelType"] == "F32"
    # At least one chunk was written directly and mosaicked.
    mosaic_calls = [c for c in calls if c[0] == "build_mosaic"]
    assert mosaic_calls and mosaic_calls[0][1] >= 1
    assert ("translate", str(out)) in calls


def test_download_elevation_default_res_uses_service_native(monkeypatch, tmp_path):
    """With no --res, the chunk grid resolution defaults to the service's
    registered native pixel size."""
    service = _elev_service()
    monkeypatch.setattr(ElevationDownloader, "load_services", lambda: {"3dep": service})
    fake = _FakeSession()
    monkeypatch.setattr(requests, "Session", lambda: fake)

    # Capture the resolution passed to build_chunk_grid.
    captured = {}

    def fake_grid(xmin, ymin, xmax, ymax, chunk_px, pixel_size_m):
        captured["res"] = pixel_size_m
        # single 1x1 chunk
        return [
            {
                "row": 0,
                "col": 0,
                "w": 1,
                "h": 1,
                "xmin": xmin,
                "ymin": ymin,
                "xmax": xmin + pixel_size_m,
                "ymax": ymin + pixel_size_m,
            }
        ]

    monkeypatch.setattr(ElevationDownloader, "build_chunk_grid", fake_grid)

    def fake_mosaic(chunk_paths, td):
        vrt = td / "mosaic.vrt"
        vrt.write_text("vrt")
        return vrt

    monkeypatch.setattr(ElevationDownloader, "build_mosaic", fake_mosaic)
    monkeypatch.setattr(
        ElevationDownloader,
        "_translate_to_geotiff",
        lambda v, o: o.write_bytes(b"x") or o,
    )

    out = tmp_path / "elevation_merged.tif"
    ElevationDownloader.download_elevation(
        shapefile_path=str(_shape(tmp_path)), outdir=str(out), num_workers=1
    )
    assert captured["res"] == 10.0


def test_download_elevation_ambiguous_prints_list_and_exits(
    monkeypatch, tmp_path, capsys
):
    """Multiple covering elevation services with no --service-index prints the
    candidate list and exits 2 (no network)."""
    monkeypatch.setattr(
        ElevationDownloader,
        "load_services",
        lambda: {"a": _elev_service("a"), "b": _elev_service("b")},
    )

    def _boom(*a, **k):
        raise AssertionError("network call attempted")

    monkeypatch.setattr(requests, "Session", _boom)

    with pytest.raises(SystemExit) as ei:
        ElevationDownloader.download_elevation(
            shapefile_path=str(_shape(tmp_path)),
            outdir=str(tmp_path / "elevation_merged.tif"),
            num_workers=1,
        )
    assert ei.value.code == 2
    out = capsys.readouterr().out
    assert "--service-index" in out
    assert "a" in out and "b" in out


def test_download_arcgis_selects_imagery_not_elevation(monkeypatch, tmp_path, capsys):
    """download_from_arcgis must resolve an imagery service and never pick an
    elevation-only service -- even when it is the only covering layer."""
    # Only an elevation service covers this AOI.
    monkeypatch.setattr(
        ElevationDownloader, "load_services", lambda: {"dem": _elev_service("dem")}
    )

    with pytest.raises(ValueError) as ei:
        OrthoDownloader.download_from_arcgis(
            shapefile_path=str(_shape(tmp_path)),
            outdir=str(tmp_path / "out"),
            zoom=15,
            xyz=True,
            resampling="lanczos",
            processes=1,
            timeout=30,
            num_workers=1,
            chunk_px=256,
        )
    assert "imagery" in str(ei.value)
    assert "elevation" not in str(ei.value)
