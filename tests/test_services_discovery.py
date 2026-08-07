import json

import pytest
import requests as _requests

from terrain_stitcher.arcgis import services as services_mod
from terrain_stitcher.arcgis.services import (
    _endpoint_url,
    harvest_from_file,
    harvest_from_items,
    load_services,
    refresh_services,
)

ROOT = "https://example.test/nrcs/rest/services"
ALASKA_URL = f"{ROOT}/ortho_imagery/alaska_vivid_2023_30cm/ImageServer"
NAIP_URL = f"{ROOT}/naip/naip_2023_tx/ImageServer"

META = {
    "fullExtent": {
        "xmin": -10000000.0,
        "ymin": 5000000.0,
        "xmax": -9000000.0,
        "ymax": 6000000.0,
        "spatialReference": {"wkid": 102100, "latestWkid": 3857},
    },
    "tileInfo": {"lods": [{"resolution": 0.2985}, {"resolution": 1.19}]},
}


class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


def _fake_get(url, params=None, timeout=None):
    # Any ImageServer endpoint returns metadata.
    return _FakeResp(META)


def test_endpoint_url_normalizes_exportimage():
    assert _endpoint_url(f"{ALASKA_URL}/exportImage") == ALASKA_URL
    assert _endpoint_url(ALASKA_URL) == ALASKA_URL
    assert _endpoint_url({"url": f"{ALASKA_URL}/exportImage"}) == ALASKA_URL


def test_endpoint_url_rejects_bad_entries():
    with pytest.raises(ValueError, match="missing 'url'"):
        _endpoint_url({"recursive": True})
    with pytest.raises(ValueError, match="unsupported endpoint entry type"):
        _endpoint_url(42)


def test_harvest_from_items_fetches_each_endpoint(monkeypatch):
    monkeypatch.setattr(_requests, "get", _fake_get)
    data = harvest_from_items([ALASKA_URL, NAIP_URL])
    bases = sorted(s["base_url"] for s in data["services"])
    assert bases == sorted([f"{ALASKA_URL}/exportImage", f"{NAIP_URL}/exportImage"])
    # coverage reprojected to a WGS84 4-tuple
    assert len(data["services"][0]["coverage"]) == 4
    assert len(data["services"]) == 2


def test_harvest_from_items_dedupes(monkeypatch):
    monkeypatch.setattr(_requests, "get", _fake_get)
    data = harvest_from_items([ALASKA_URL, ALASKA_URL])
    assert len(data["services"]) == 1


def test_harvest_from_file_array(monkeypatch, tmp_path):
    monkeypatch.setattr(_requests, "get", _fake_get)
    f = tmp_path / "endpoints.json"
    f.write_text(json.dumps([ALASKA_URL]))
    data = harvest_from_file(str(f))
    assert len(data["services"]) == 1
    assert data["services"][0]["base_url"] == f"{ALASKA_URL}/exportImage"


def test_harvest_from_file_folders_wrapper(monkeypatch, tmp_path):
    monkeypatch.setattr(_requests, "get", _fake_get)
    f = tmp_path / "endpoints.json"
    f.write_text(json.dumps({"folders": [ALASKA_URL]}))
    data = harvest_from_file(str(f))
    assert len(data["services"]) == 1


def test_refresh_services_default_uses_shipped_scan_list(monkeypatch, tmp_path):
    monkeypatch.setattr(_requests, "get", _fake_get)
    monkeypatch.setattr(services_mod, "_shipped_scan_items", lambda: [ALASKA_URL])
    cache = tmp_path / "services.json"
    monkeypatch.setattr(services_mod, "_cache_path", lambda: cache)
    refresh_services()
    assert cache.is_file()
    assert len(load_services()) == 1


def test_refresh_services_from_file(monkeypatch, tmp_path):
    monkeypatch.setattr(_requests, "get", _fake_get)
    scan = tmp_path / "scan.json"
    scan.write_text(json.dumps([ALASKA_URL, NAIP_URL]))
    cache = tmp_path / "services.json"
    monkeypatch.setattr(services_mod, "_cache_path", lambda: cache)
    refresh_services(from_file=str(scan))
    assert cache.is_file()
    assert len(load_services()) == 2


# --- pixel_size declared on scan-list entries -------------------------------


def test_item_pixel_size_returns_declared_value():
    from terrain_stitcher.arcgis.services import _item_pixel_size

    assert _item_pixel_size({"url": "http://x", "pixel_size": 0.3}) == 0.3
    assert _item_pixel_size({"url": "http://x"}) is None
    assert _item_pixel_size("http://x") is None


def test_harvest_uses_declared_pixel_size(monkeypatch):
    """A scan-list object may declare pixel_size; it overrides metadata even
    when the service exposes no tileInfo (e.g. USGSNAIPPlus)."""
    import requests as _requests
    from terrain_stitcher.arcgis.services import harvest_from_items

    # USGSNAIPPlus-like metadata: no tileInfo, minPixelSize 0, pixelSizeX 0.3.
    mosaic_meta = {
        "fullExtent": {
            "xmin": -10000000.0,
            "ymin": 5000000.0,
            "xmax": -9000000.0,
            "ymax": 6000000.0,
            "spatialReference": {"wkid": 102100, "latestWkid": 3857},
        },
        "tileInfo": None,
        "minPixelSize": 0,
        "pixelSizeX": 0.3,
    }

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return mosaic_meta

    monkeypatch.setattr(_requests, "get", lambda *a, **k: _Resp())
    data = harvest_from_items(
        [{"url": "https://example.test/svc/ImageServer", "pixel_size": 0.3}]
    )
    assert len(data["services"]) == 1
    assert data["services"][0]["native_pixel_size_m"] == 0.3


def test_native_pixel_size_falls_back_to_pixel_size_x():
    """When no pixel_size is declared and there is no tileInfo, the metadata
    fallback uses pixelSizeX (fixing the previous 0.0 bug)."""
    from terrain_stitcher.arcgis.services import _native_pixel_size

    assert (
        _native_pixel_size({"tileInfo": None, "minPixelSize": 0, "pixelSizeX": 0.3})
        == 0.3
    )
    import pytest

    with pytest.raises(ValueError):
        _native_pixel_size({"tileInfo": None, "minPixelSize": 0})


# --- capability kinds stamped from scan-list entries ------------------------


def test_item_kinds_defaults_to_imagery():
    from terrain_stitcher.arcgis.services import _item_kinds

    assert _item_kinds("http://x") == ["imagery"]
    assert _item_kinds({"url": "http://x"}) == ["imagery"]
    assert _item_kinds({"url": "http://x", "kinds": ["elevation"]}) == ["elevation"]
    # unknown values are filtered out, degrading to the default
    assert _item_kinds({"url": "http://x", "kinds": ["bogus"]}) == ["imagery"]


def test_harvest_stamps_kinds_from_scan_entry(monkeypatch):
    """An object scan entry declaring kinds=["elevation"] must register the
    service with that capability, and one without kinds stays imagery."""
    import requests as _requests
    from terrain_stitcher.arcgis.services import harvest_from_items

    monkeypatch.setattr(_requests, "get", _fake_get)
    data = harvest_from_items(
        [
            {"url": f"{ALASKA_URL}", "kinds": ["elevation"]},
            {"url": f"{NAIP_URL}"},
        ]
    )
    by_key = {s["key"]: s for s in data["services"]}
    assert len(data["services"]) == 2
    elev_keys = [k for k, s in by_key.items() if s["kinds"] == ["elevation"]]
    imagery_keys = [k for k, s in by_key.items() if s["kinds"] == ["imagery"]]
    assert len(elev_keys) == 1
    assert len(imagery_keys) == 1
