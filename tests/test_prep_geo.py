import os
from terrain_stitcher.common import World_Bounding_Box, World_Coordinates
from terrain_stitcher.functions.ElevationGeoPrep import (
    ElevationData,
    findIntersectingFiles,
    findContinuousRegions,
    isFullyCovered,
    lonIntervalsCover,
)


def _box(min_lat, min_lon, max_lat, max_lon):
    return World_Bounding_Box(
        World_Coordinates(lat=str(min_lat), lon=str(min_lon)),
        World_Coordinates(lat=str(max_lat), lon=str(max_lon)),
    )


def _ed(name, min_lat, min_lon, max_lat, max_lon):
    return ElevationData(name, _box(min_lat, min_lon, max_lat, max_lon))


def test_find_intersecting_strict_overlap_excludes_edge_touchers():
    target = _box(40.0, -100.0, 41.0, -99.0)
    inside = _ed("a", 40.2, -100.1, 40.8, -99.2)
    touching_edge = _ed("b", 40.0, -99.0, 41.0, -98.0)  # shares east edge only
    far = _ed("c", 50.0, -120.0, 51.0, -119.0)

    result = findIntersectingFiles([inside, touching_edge, far], target)
    assert [e.srcFilePath for e in result] == ["a"]


def test_find_continuous_regions_groups_touching_tiles():
    a = _ed("a", 40.0, -100.0, 41.0, -99.0)
    b = _ed("b", 40.0, -99.0, 41.0, -98.0)  # touches a on east edge
    c = _ed("c", 50.0, -100.0, 51.0, -99.0)  # disjoint

    groups = findContinuousRegions([a, b, c])
    groups.sort(key=lambda g: len(g))
    assert len(groups) == 2
    assert {a.srcFilePath, b.srcFilePath} == {e.srcFilePath for e in groups[1]}
    assert [c.srcFilePath] == [e.srcFilePath for e in groups[0]]


def test_is_fully_covered_true_for_complete_tiles():
    target = _box(40.0, -100.0, 41.0, -99.0)
    region = [
        _ed("a", 39.9, -100.1, 40.5, -99.5),
        _ed("b", 39.9, -99.5, 40.5, -98.9),
        _ed("c", 40.5, -100.1, 41.1, -99.5),
        _ed("d", 40.5, -99.5, 41.1, -98.9),
    ]
    assert isFullyCovered(target, region) is True


def test_is_fully_covered_false_with_gap():
    target = _box(40.0, -100.0, 41.0, -99.0)
    # gap between -99.6 and -99.4 in the middle strip
    region = [
        _ed("a", 39.9, -100.1, 41.1, -99.6),
        _ed("b", 39.9, -99.4, 41.1, -98.9),
    ]
    assert isFullyCovered(target, region) is False


def test_lon_intervals_cover_detects_gap():
    a = _ed("a", 0.0, -100.0, 1.0, -99.6)
    b = _ed("b", 0.0, -99.4, 1.0, -99.0)
    assert lonIntervalsCover([a, b], -100.0, -99.0) is False
    assert lonIntervalsCover([a, b], -100.0, -99.6) is True


def test_lon_intervals_cover_empty_returns_false():
    assert lonIntervalsCover([], -100.0, -99.0) is False
