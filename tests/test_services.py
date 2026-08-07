import json

import pytest

from terrain_stitcher.arcgis import services as services_mod
from terrain_stitcher.arcgis.services import (
    ImageryService,
    bbox_latlon_from_radius,
    load_services,
    select_service,
)

ALASKA = ImageryService(
    key="alaska_vivid_2023_30cm",
    label="Alaska Vivid 2023 30cm",
    base_url=(
        "https://apps.geo.fpac.usda.gov/nrcs-imagery/rest/services/"
        "ortho_imagery/alaska_vivid_2023_30cm/ImageServer/exportImage"
    ),
    native_pixel_size_m=0.2985821417389697,
    srs=3857,
    coverage=(51.0, -180.0, 71.5, -129.0),
)


@pytest.fixture()
def services():
    return {"alaska_vivid_2023_30cm": ALASKA}


def test_select_service_for_in_state_aoi(services):
    aoi = bbox_latlon_from_radius(61.0, -149.0, 5.0)
    svc = select_service(services, aoi)
    assert svc.key == "alaska_vivid_2023_30cm"


def test_select_service_rejects_out_of_coverage_center(services):
    # Maine is nowhere near the Alaska layer.
    aoi = bbox_latlon_from_radius(44.3, -68.2, 5.0)
    with pytest.raises(ValueError, match="not inside any registered"):
        select_service(services, aoi)


def test_select_service_rejects_aoi_straddling_coverage(services):
    # Center is inside Alaska, but the radius is huge so the AOI bbox pokes
    # outside the layer footprint -- downloads are limited to one state.
    aoi = bbox_latlon_from_radius(61.0, -149.0, 800.0)
    with pytest.raises(ValueError, match="extends outside"):
        select_service(services, aoi)


def test_select_service_empty_cache_directs_to_refresh():
    with pytest.raises(ValueError, match="refresh-services"):
        select_service({}, bbox_latlon_from_radius(61.0, -149.0, 5.0))


def test_load_services_empty_without_cache(monkeypatch):
    monkeypatch.setattr(services_mod, "_cache_path", lambda: None)
    assert load_services() == {}


def test_load_services_reads_cache(monkeypatch, tmp_path):
    cache = tmp_path / "services.json"
    cache.write_text(json.dumps({"services": [ALASKA.to_dict()]}))
    monkeypatch.setattr(services_mod, "_cache_path", lambda: cache)
    loaded = load_services()
    assert set(loaded) == {"alaska_vivid_2023_30cm"}
    assert loaded["alaska_vivid_2023_30cm"].coverage == ALASKA.coverage


def test_shipped_scan_list_is_present():
    items = services_mod._shipped_scan_items()
    assert isinstance(items, list) and len(items) >= 1


def test_imagery_service_roundtrip():
    svc = ImageryService(
        key="x",
        label="X",
        base_url="http://example/x/ImageServer/exportImage",
        native_pixel_size_m=0.5,
        srs=3857,
        coverage=(10.0, -100.0, 20.0, -90.0),
    )
    d = svc.to_dict()
    assert d["coverage"] == [10.0, -100.0, 20.0, -90.0]
    assert ImageryService.from_dict(d) == svc
    assert svc.contains_point(15.0, -95.0)
    assert not svc.contains_point(25.0, -95.0)
    assert svc.contains_bbox((12.0, -98.0, 18.0, -92.0))
    assert not svc.contains_bbox((12.0, -110.0, 18.0, -92.0))


# --- LOD vs native resolution guard -----------------------------------------


def test_assert_lod_within_native_rejects_finer_lod():
    from terrain_stitcher.functions.OrthoDownloader import assert_lod_within_native

    # Alaska native ~0.2986 m/px == ~LOD 19; LOD 23 is finer.
    import pytest

    with pytest.raises(ValueError, match="native resolution"):
        assert_lod_within_native(23, ALASKA)


def test_assert_lod_within_native_allows_coarser_and_equal_lod():
    from terrain_stitcher.functions.OrthoDownloader import assert_lod_within_native

    # Coarser than native (LOD 17 -> 1.19 m/px) and equal (LOD 19 -> native)
    # must not raise.
    assert assert_lod_within_native(17, ALASKA) is None
    assert assert_lod_within_native(19, ALASKA) is None


def test_download_arcgis_rejects_lod_above_native(monkeypatch, tmp_path):
    """download_from_arcgis must fail fast (before any network/disk) when the
    requested LOD is finer than the service native resolution."""
    import json as _json
    import pytest
    from terrain_stitcher.functions.OrthoDownloader import download_from_arcgis

    shape = tmp_path / "Shape.json"
    shape.write_text(
        _json.dumps(
            {
                "boundsType": "POINT",
                "center": {"x": -149.0, "y": 61.0},
                "view_distance": 5.0,
            }
        )
    )
    # If the guard fails to fire, the next steps hit the network / disk;
    # fail the test loudly if any request is attempted.
    import requests as _requests

    def _boom(*a, **k):
        raise AssertionError("network call attempted before the LOD guard")

    monkeypatch.setattr(_requests, "get", _boom)

    with pytest.raises(ValueError, match="native resolution"):
        download_from_arcgis(
            shapefile_path=str(shape),
            outdir=str(tmp_path / "out"),
            zoom=23,
            xyz=True,
            resampling="lanczos",
            processes=1,
            timeout=30,
            num_workers=1,
            chunk_px=256,
            service=ALASKA,
        )


# --- multi-service index selection -----------------------------------------

from terrain_stitcher.arcgis.services import AmbiguousServiceError

NAIPPLUS = ImageryService(
    key="naipplus",
    label="USGS NAIP Plus",
    base_url="https://imagery.nationalmap.gov/arcgis/rest/services/USGSNAIPPlus/ImageServer/exportImage",
    native_pixel_size_m=0.3,
    srs=3857,
    coverage=(-15.0, -180.0, 72.0, 180.0),
)


@pytest.fixture()
def services_multi():
    return {"alaska_vivid_2023_30cm": ALASKA, "naipplus": NAIPPLUS}


def test_select_service_ambiguous_without_index(services_multi):
    aoi = bbox_latlon_from_radius(61.0, -149.0, 5.0)
    with pytest.raises(AmbiguousServiceError) as ei:
        select_service(services_multi, aoi)
    keys = [s.key for s in ei.value.candidates]
    # ordered by key for stable indices
    assert keys == ["alaska_vivid_2023_30cm", "naipplus"]


def test_select_service_ambiguous_index_selects(services_multi):
    aoi = bbox_latlon_from_radius(61.0, -149.0, 5.0)
    assert select_service(services_multi, aoi, index=0).key == "alaska_vivid_2023_30cm"
    assert select_service(services_multi, aoi, index=1).key == "naipplus"


def test_select_service_index_out_of_range(services_multi):
    aoi = bbox_latlon_from_radius(61.0, -149.0, 5.0)
    with pytest.raises(ValueError, match="out of range"):
        select_service(services_multi, aoi, index=5)


def test_select_service_picks_the_single_fully_covering_service(services_multi):
    # Big AOI: center is inside both, but only the nationwide layer fully
    # contains it -> no disambiguation needed.
    aoi = bbox_latlon_from_radius(61.0, -149.0, 700.0)
    assert select_service(services_multi, aoi).key == "naipplus"


def test_download_arcgis_ambiguous_prints_list_and_exits(monkeypatch, tmp_path, capsys):
    """With no --service-index and multiple covering services, the download
    prints the candidate list with indices and exits (no network)."""
    import json as _json
    import pytest
    from terrain_stitcher.functions import OrthoDownloader

    shape = tmp_path / "Shape.json"
    shape.write_text(
        _json.dumps(
            {
                "boundsType": "POINT",
                "center": {"x": -149.0, "y": 61.0},
                "view_distance": 5.0,
            }
        )
    )
    monkeypatch.setattr(
        OrthoDownloader,
        "load_services",
        lambda: {"alaska_vivid_2023_30cm": ALASKA, "naipplus": NAIPPLUS},
    )
    # Guard against any accidental network call.
    import requests as _requests

    monkeypatch.setattr(
        _requests,
        "get",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")),
    )

    with pytest.raises(SystemExit) as ei:
        OrthoDownloader.download_from_arcgis(
            shapefile_path=str(shape),
            outdir=str(tmp_path / "out"),
            zoom=18,
            xyz=True,
            resampling="lanczos",
            processes=1,
            timeout=30,
            num_workers=1,
            chunk_px=256,
        )
    assert ei.value.code == 2
    out = capsys.readouterr().out
    assert "--service-index" in out
    assert "0:" in out and "1:" in out
    assert "alaska_vivid_2023_30cm" in out and "naipplus" in out


# --- capability kinds -------------------------------------------------------


def test_kinds_default_to_imagery():
    svc = ImageryService(
        key="x",
        label="X",
        base_url="http://example/x/ImageServer/exportImage",
        native_pixel_size_m=0.5,
        srs=3857,
        coverage=(10.0, -100.0, 20.0, -90.0),
    )
    assert svc.kinds == ["imagery"]
    assert svc.supports_kind("imagery")
    assert not svc.supports_kind("elevation")


def test_kinds_roundtrip():
    svc = ImageryService(
        key="x",
        label="X",
        base_url="http://example/x/ImageServer/exportImage",
        native_pixel_size_m=10.0,
        srs=3857,
        coverage=(10.0, -100.0, 60.0, -50.0),
        kinds=["elevation"],
    )
    d = svc.to_dict()
    assert d["kinds"] == ["elevation"]
    assert ImageryService.from_dict(d) == svc


def test_from_dict_defaults_kinds_to_imagery():
    d = {
        "key": "x",
        "label": "X",
        "base_url": "http://example/x/ImageServer/exportImage",
        "native_pixel_size_m": 0.5,
        "srs": 3857,
        "coverage": [10.0, -100.0, 20.0, -90.0],
    }
    svc = ImageryService.from_dict(d)
    assert svc.kinds == ["imagery"]


# --- kind filtering in selection ---------------------------------------------


def _svc(key, kinds):
    return ImageryService(
        key=key,
        label=key,
        base_url=f"http://example/{key}/ImageServer/exportImage",
        native_pixel_size_m=10.0,
        srs=3857,
        coverage=(10.0, -100.0, 60.0, -50.0),
        kinds=kinds,
    )


def test_select_service_filters_by_kind():
    services = {
        "ortho": _svc("ortho", ["imagery"]),
        "dem": _svc("dem", ["elevation"]),
        "both": _svc("both", ["imagery", "elevation"]),
    }
    aoi = bbox_latlon_from_radius(40.0, -90.0, 5.0)
    # imagery selection only considers imagery + both-tagged services; the
    # elevation-only "dem" must not appear among the candidates.
    with pytest.raises(AmbiguousServiceError) as ei:
        select_service(services, aoi, kind="imagery")
    assert [s.key for s in ei.value.candidates] == ["both", "ortho"]
    # elevation selection only considers elevation + both-tagged services;
    # the imagery-only "ortho" must not appear.
    with pytest.raises(AmbiguousServiceError) as ei:
        select_service(services, aoi, kind="elevation")
    assert [s.key for s in ei.value.candidates] == ["both", "dem"]


def test_select_service_elevation_ignores_imagery_only():
    """An imagery-only service must never satisfy an elevation request."""
    services = {
        "ortho": _svc("ortho", ["imagery"]),
        "dem": _svc("dem", ["elevation"]),
    }
    aoi = bbox_latlon_from_radius(40.0, -90.0, 5.0)
    # The imagery-only service alone covers the center, but for elevation the
    # only candidate is the DEM.
    assert select_service(services, aoi, kind="elevation").key == "dem"
    # For imagery, the DEM is not a candidate, so the imagery-only service is
    # selected (proving the elevation-only service is ignored for imagery).
    assert select_service(services, aoi, kind="imagery").key == "ortho"
