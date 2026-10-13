import json

import pytest

from terrain_stitcher.arcgis.level_of_detail import (
    WEB_MERCATOR_TILE_PX,
    WEB_MERCATOR_WORLD_X_MIN,
    WEB_MERCATOR_WORLD_Y_MAX,
    native_lod_for_service,
    pixel_size_for_zoom,
)
from terrain_stitcher.arcgis.services import (
    ImageryService,
    bbox_latlon_from_radius,
)
from terrain_stitcher.common.ElevationFileInfo import elevation_info_path
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

    def fake_build_mosaic(chunk_paths, td, grid_shape=None, vrt_workers=None):
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
        lod=12,
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
    # A twin info sidecar records the projection system.
    info = elevation_info_path(out)
    assert info.is_file()
    assert json.loads(info.read_text())["projection"] == "EPSG:3857"


def test_download_elevation_default_lod_uses_service_native(monkeypatch, tmp_path):
    """With no --lod, the fetch LOD defaults to the one closest to the
    service's registered native pixel size."""
    service = _elev_service()
    monkeypatch.setattr(ElevationDownloader, "load_services", lambda: {"3dep": service})
    fake = _FakeSession()
    monkeypatch.setattr(requests, "Session", lambda: fake)

    # Capture the LOD passed to build_tile_aligned_chunk_grid.
    captured = {}

    def fake_grid(xmin, ymin, xmax, ymax, lod, chunk_px):
        captured["lod"] = lod
        # single 1x1 chunk
        return [
            {
                "row": 0,
                "col": 0,
                "w": 1,
                "h": 1,
                "xmin": xmin,
                "ymin": ymin,
                "xmax": xmin + pixel_size_for_zoom(lod),
                "ymax": ymin + pixel_size_for_zoom(lod),
            }
        ]

    monkeypatch.setattr(ElevationDownloader, "build_tile_aligned_chunk_grid", fake_grid)

    def fake_mosaic(chunk_paths, td, grid_shape=None, vrt_workers=None):
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
    assert captured["lod"] == native_lod_for_service(service) == 14


def test_download_elevation_lod_grid_is_tile_aligned(monkeypatch, tmp_path):
    """Every exportImage request rectangle must sit on the standard Web
    Mercator tile grid, so the merged GeoTIFF is 1:1 aligned with ortho
    tiles at the same LOD (no bbox-corner-anchored pixels anywhere)."""
    service = _elev_service()
    monkeypatch.setattr(ElevationDownloader, "load_services", lambda: {"3dep": service})
    fake = _FakeSession()
    monkeypatch.setattr(requests, "Session", lambda: fake)

    monkeypatch.setattr(
        ElevationDownloader,
        "build_mosaic",
        lambda chunk_paths, td, grid_shape=None, vrt_workers=None: (
            (td / "mosaic.vrt").write_text("vrt") or (td / "mosaic.vrt")
        ),
    )
    monkeypatch.setattr(
        ElevationDownloader,
        "_translate_to_geotiff",
        lambda v, o: o.write_bytes(b"geotiff") or o,
    )

    lod = 12
    out = tmp_path / "elevation_merged.tif"
    ElevationDownloader.download_elevation(
        shapefile_path=str(_shape(tmp_path)),
        outdir=str(out),
        lod=lod,
        num_workers=1,
    )

    assert fake.calls, "no exportImage requests were made"
    res = pixel_size_for_zoom(lod)
    tile_m = WEB_MERCATOR_TILE_PX * res

    xmins, ymins, xmaxs, ymaxs = [], [], [], []
    for params in fake.calls:
        bbox = [float(v) for v in params["bbox"].split(",")]
        w, h = (int(v) for v in params["size"].split(","))
        cxmin, cymin, cxmax, cymax = bbox

        def _on_grid(dist):
            # Float modulo of a near-multiple can wrap to tile_m - eps.
            return min(dist % tile_m, tile_m - dist % tile_m)

        # Every edge lands on a tile-grid line (world origin + n tiles).
        assert _on_grid(cxmin - WEB_MERCATOR_WORLD_X_MIN) == pytest.approx(0, abs=1e-6)
        assert _on_grid(cxmax - WEB_MERCATOR_WORLD_X_MIN) == pytest.approx(0, abs=1e-6)
        assert _on_grid(WEB_MERCATOR_WORLD_Y_MAX - cymax) == pytest.approx(0, abs=1e-6)
        assert _on_grid(WEB_MERCATOR_WORLD_Y_MAX - cymin) == pytest.approx(0, abs=1e-6)
        # ...and the request size matches the rectangle exactly, so the
        # server's pixels land on the tile grid with no resampling.
        assert (cxmax - cxmin) / w == pytest.approx(res)
        assert (cymax - cymin) / h == pytest.approx(res)
        xmins.append(cxmin)
        ymins.append(cymin)
        xmaxs.append(cxmax)
        ymaxs.append(cymax)

    # The tile-aligned envelope must cover the projected AOI bbox.
    min_lat, min_lon, max_lat, max_lon = bbox_latlon_from_radius(40.0, -90.0, 5.0)
    aoi_xmin, aoi_ymin = DownloaderBase.WGS84_TO_WEBMERC.transform(min_lon, min_lat)
    aoi_xmax, aoi_ymax = DownloaderBase.WGS84_TO_WEBMERC.transform(max_lon, max_lat)
    assert min(xmins) <= aoi_xmin and max(xmaxs) >= aoi_xmax
    assert min(ymins) <= aoi_ymin and max(ymaxs) >= aoi_ymax

    assert out.is_file()


def test_download_elevation_lod_finer_than_native_warns(monkeypatch, tmp_path, capsys):
    """An LOD finer than the service's native ~LOD warns (no hard fail),
    matching the elevation semantics; a coarser LOD stays silent."""
    service = _elev_service()  # native 10 m/px -> ~LOD 14
    monkeypatch.setattr(ElevationDownloader, "load_services", lambda: {"3dep": service})
    monkeypatch.setattr(requests, "Session", lambda: _FakeSession())

    monkeypatch.setattr(
        ElevationDownloader,
        "build_tile_aligned_chunk_grid",
        lambda xmin, ymin, xmax, ymax, lod, chunk_px: [
            {
                "row": 0,
                "col": 0,
                "w": 1,
                "h": 1,
                "xmin": xmin,
                "ymin": ymin,
                "xmax": xmax,
                "ymax": ymax,
            }
        ],
    )
    monkeypatch.setattr(
        ElevationDownloader,
        "build_mosaic",
        lambda chunk_paths, td, grid_shape=None, vrt_workers=None: (
            (td / "mosaic.vrt").write_text("vrt") or (td / "mosaic.vrt")
        ),
    )
    monkeypatch.setattr(
        ElevationDownloader,
        "_translate_to_geotiff",
        lambda v, o: o.write_bytes(b"geotiff") or o,
    )

    out = tmp_path / "finer.tif"
    ElevationDownloader.download_elevation(
        shapefile_path=str(_shape(tmp_path)), outdir=str(out), lod=15, num_workers=1
    )
    out_text = capsys.readouterr().out
    assert "LOD 15" in out_text
    assert "finer than" in out_text
    assert "interpolated" in out_text

    out2 = tmp_path / "coarser.tif"
    ElevationDownloader.download_elevation(
        shapefile_path=str(_shape(tmp_path)), outdir=str(out2), lod=12, num_workers=1
    )
    assert "finer than" not in capsys.readouterr().out


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
        OrthoDownloader, "load_services", lambda: {"dem": _elev_service("dem")}
    )

    with pytest.raises(ValueError) as ei:
        OrthoDownloader.download_from_arcgis(
            shapefile_path=str(_shape(tmp_path)),
            outdir=str(tmp_path / "out"),
            zoom=15,
            xyz=True,
            resampling="lanczos",
            timeout=30,
            num_workers=1,
            chunk_px=256,
        )
    assert "imagery" in str(ei.value)
    assert "elevation" not in str(ei.value)
