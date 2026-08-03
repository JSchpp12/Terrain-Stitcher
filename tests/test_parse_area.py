from __future__ import annotations

import json
from pathlib import Path

import pytest

from terrain_stitcher.common import (
    ParseArea,
    TerrainBoundsCalculateType,
    World_Coordinates,
)


def _make_area(lat="55.9096", lon="-159.1595", view_distance=5):
    return ParseArea(
        TerrainBoundsCalculateType.POINT,
        World_Coordinates(lat, lon),
        view_distance,
    )


# --- toJSON: dual lat/lon + x/y center format ------------------------------


def test_to_json_emits_both_legacy_and_new_center_keys():
    data = _make_area().toJSON()
    center = data["center"]
    assert set(center.keys()) == {"lat", "lon", "x", "y"}


def test_to_json_keeps_legacy_lat_lon_as_original_string_values():
    data = _make_area(lat="55.9096", lon="-159.1595").toJSON()
    center = data["center"]
    # legacy keys keep the original string values verbatim
    assert center["lat"] == "55.9096"
    assert center["lon"] == "-159.1595"


def test_to_json_writes_x_y_as_floats_mirroring_lon_lat():
    data = _make_area(lat="55.9096", lon="-159.1595").toJSON()
    center = data["center"]
    # x mirrors longitude, y mirrors latitude, both as floats
    assert isinstance(center["x"], float)
    assert isinstance(center["y"], float)
    assert center["x"] == pytest.approx(-159.1595)
    assert center["y"] == pytest.approx(55.9096)


def test_to_json_view_distance_is_a_float():
    data = _make_area(view_distance=5).toJSON()
    assert isinstance(data["view_distance"], float)
    assert data["view_distance"] == 5.0


def test_to_json_bounds_type_string_round_trips():
    data = _make_area().toJSON()
    assert data["boundsType"] == "POINT"


# --- fromJSONFile: accepts new x/y and legacy lat/lon ----------------------


def _write_shape(tmp_path: Path, center: dict, view_distance=5) -> Path:
    doc = {
        "boundsType": "POINT",
        "center": center,
        "view_distance": view_distance,
    }
    p = tmp_path / "Shape.json"
    p.write_text(json.dumps(doc))
    return p


def test_from_json_file_reads_new_x_y_format(tmp_path):
    p = _write_shape(tmp_path, {"lat": "55.9096", "lon": "-159.1595",
                                "x": -159.1595, "y": 55.9096})
    area = ParseArea.fromJSONFile(str(p))
    assert area.center.get_lat() == pytest.approx(55.9096)
    assert area.center.get_lon() == pytest.approx(-159.1595)


def test_from_json_file_reads_legacy_lat_lon_format(tmp_path):
    p = _write_shape(tmp_path, {"lat": "55.9096", "lon": "-159.1595"})
    area = ParseArea.fromJSONFile(str(p))
    assert area.center.get_lat() == pytest.approx(55.9096)
    assert area.center.get_lon() == pytest.approx(-159.1595)


def test_from_json_file_prefers_x_y_over_lat_lon(tmp_path):
    # x/y present but disagree with lat/lon -> x/y wins (x=lon, y=lat)
    p = _write_shape(tmp_path, {"lat": "0.0", "lon": "0.0",
                                "x": -159.1595, "y": 55.9096})
    area = ParseArea.fromJSONFile(str(p))
    assert area.center.get_lat() == pytest.approx(55.9096)
    assert area.center.get_lon() == pytest.approx(-159.1595)


# --- round trip ------------------------------------------------------------


def test_to_json_then_from_json_file_round_trips(tmp_path):
    area = _make_area(lat="40.0", lon="-100.0", view_distance=2)
    p = tmp_path / "Shape.json"
    p.write_text(json.dumps(area.toJSON(), indent=4))

    restored = ParseArea.fromJSONFile(str(p))
    assert restored.center.get_lat() == pytest.approx(40.0)
    assert restored.center.get_lon() == pytest.approx(-100.0)
    assert restored.view_distance == 2.0


def test_round_tripped_area_produces_same_bounding_region(tmp_path):
    original = _make_area(lat="40.0", lon="-100.0", view_distance=2)
    p = tmp_path / "Shape.json"
    p.write_text(json.dumps(original.toJSON()))

    restored = ParseArea.fromJSONFile(str(p))
    orig_box = original.getTotalRegion()
    rest_box = restored.getTotalRegion()
    assert orig_box.get_lower_left().get_lat() == pytest.approx(
        rest_box.get_lower_left().get_lat()
    )
    assert orig_box.get_lower_left().get_lon() == pytest.approx(
        rest_box.get_lower_left().get_lon()
    )
    assert orig_box.get_upper_right().get_lat() == pytest.approx(
        rest_box.get_upper_right().get_lat()
    )
    assert orig_box.get_upper_right().get_lon() == pytest.approx(
        rest_box.get_upper_right().get_lon()
    )