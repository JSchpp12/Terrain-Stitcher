"""Tests for the split-image command (ImageSplitter.main).

Covers:
  * Image cropping (no resizing) for vertical and horizontal splits
  * Bounds computation: vertical split uses linear longitude midpoint;
    horizontal split uses Web Mercator Y midpoint (not arithmetic lat mean)
  * Odd-dimension pixel handling (one half gets the extra pixel)
  * Manifest update: original entry replaced by two new entries, other
    entries preserved, elevation_files preserved
  * Sibling-file passthrough (other images + non-image files copied,
    original image excluded)
  * Auto axis detection (wider -> vertical, taller -> horizontal)
  * Error cases: image not in manifest, manifest missing, invalid axis
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pyproj
import pytest
from PIL import Image as pImage

from terrain_stitcher.functions.ImageSplitter import (
    _resolve_axis,
    _split_bounds,
    _split_image,
    _save_image_atomic,
    main,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _coord(lat, lon):
    return {"lat": lat, "lon": lon}


def _bounds(lat_n, lat_s, lon_w, lon_e):
    return {
        "center": _coord((lat_n + lat_s) / 2, (lon_w + lon_e) / 2),
        "northEast": _coord(lat_n, lon_e),
        "southEast": _coord(lat_s, lon_e),
        "southWest": _coord(lat_s, lon_w),
        "northWest": _coord(lat_n, lon_w),
    }


def _entry(name, lat_n, lat_s, lon_w, lon_e):
    return {"name": name, "bounds": _bounds(lat_n, lat_s, lon_w, lon_e)}


def _write_manifest(tmp_path: Path, images, elevation_files=None):
    data = {"images": images}
    if elevation_files is not None:
        data["elevation_files"] = elevation_files
    (tmp_path / "height_info.json").write_text(json.dumps(data))


def _make_terrain_dir(
    tmp_path: Path, entries, image_sizes=None, elevation_files=None, extra_files=None
):
    """Create a mock terrain output directory with images + manifest.

    Parameters
    ----------
    entries : list of manifest image entries (name + bounds dicts)
    image_sizes : dict mapping name -> (width, height); default 20x20
    elevation_files : optional list of filenames to create as dummy files
    extra_files : optional dict mapping filename -> bytes content
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    if image_sizes is None:
        image_sizes = {}
    _write_manifest(tmp_path, entries, elevation_files=elevation_files)
    for entry in entries:
        name = entry["name"]
        w, h = image_sizes.get(name, (20, 20))
        pImage.new("RGB", (w, h), (128, 128, 128)).save(tmp_path / (name + ".png"))
    if elevation_files:
        for fn in elevation_files:
            (tmp_path / fn).write_bytes(b"FAKE-ELEV")
    if extra_files:
        for fn, content in extra_files.items():
            if isinstance(content, str):
                (tmp_path / fn).write_text(content)
            else:
                (tmp_path / fn).write_bytes(content)


# ---------------------------------------------------------------------------
# _resolve_axis
# ---------------------------------------------------------------------------


class TestResolveAxis:
    def test_wider_image_splits_vertical(self):
        assert _resolve_axis(100, 50, "auto") == "vertical"

    def test_taller_image_splits_horizontal(self):
        assert _resolve_axis(50, 100, "auto") == "horizontal"

    def test_square_defaults_vertical(self):
        assert _resolve_axis(50, 50, "auto") == "vertical"

    def test_explicit_vertical(self):
        assert _resolve_axis(50, 100, "vertical") == "vertical"

    def test_explicit_horizontal(self):
        assert _resolve_axis(100, 50, "horizontal") == "horizontal"


# ---------------------------------------------------------------------------
# _split_image (crop, no resize)
# ---------------------------------------------------------------------------


class TestSplitImage:
    def test_vertical_even_width(self):
        img = pImage.new("RGB", (100, 40), (255, 0, 0))
        left, right = _split_image(img, "vertical")
        assert left.size == (50, 40)
        assert right.size == (50, 40)

    def test_vertical_odd_width(self):
        img = pImage.new("RGB", (101, 40), (255, 0, 0))
        left, right = _split_image(img, "vertical")
        # first half gets the extra pixel (floor division)
        assert left.size == (50, 40)
        assert right.size == (51, 40)

    def test_horizontal_even_height(self):
        img = pImage.new("RGB", (40, 100), (255, 0, 0))
        top, bottom = _split_image(img, "horizontal")
        assert top.size == (40, 50)
        assert bottom.size == (40, 50)

    def test_horizontal_odd_height(self):
        img = pImage.new("RGB", (40, 101), (255, 0, 0))
        top, bottom = _split_image(img, "horizontal")
        assert top.size == (40, 50)
        assert bottom.size == (40, 51)

    def test_no_resizing_pixel_count_preserved(self):
        """The two halves together cover every original pixel exactly once."""
        img = pImage.new("RGB", (37, 23), (200, 100, 50))
        left, right = _split_image(img, "vertical")
        assert left.size[0] + right.size[0] == 37
        assert left.size[1] == right.size[1] == 23


# ---------------------------------------------------------------------------
# _split_bounds
# ---------------------------------------------------------------------------


class TestSplitBounds:
    def test_vertical_split_divides_longitude(self):
        """Vertical split: left gets [west, mid], right gets [mid, east].
        Latitude unchanged for both halves."""
        bounds = _bounds(40.0, 39.0, -82.0, -81.0)
        left, right = _split_bounds(bounds, "vertical")

        # Left half: lon [-82, -81.5], lat [39, 40]
        assert left["northWest"]["lon"] == pytest.approx(-82.0)
        assert left["northEast"]["lon"] == pytest.approx(-81.5)
        assert left["southWest"]["lon"] == pytest.approx(-82.0)
        assert left["southEast"]["lon"] == pytest.approx(-81.5)
        assert left["northWest"]["lat"] == pytest.approx(40.0)
        assert left["southEast"]["lat"] == pytest.approx(39.0)

        # Right half: lon [-81.5, -81], lat [39, 40]
        assert right["northWest"]["lon"] == pytest.approx(-81.5)
        assert right["northEast"]["lon"] == pytest.approx(-81.0)
        assert right["southWest"]["lon"] == pytest.approx(-81.5)
        assert right["southEast"]["lon"] == pytest.approx(-81.0)
        assert right["northWest"]["lat"] == pytest.approx(40.0)
        assert right["southEast"]["lat"] == pytest.approx(39.0)

    def test_vertical_split_center_is_lon_midpoint(self):
        bounds = _bounds(40.0, 39.0, -82.0, -81.0)
        left, right = _split_bounds(bounds, "vertical")
        # Center lon of left = (-82 + -81.5) / 2
        assert left["center"]["lon"] == pytest.approx(-81.75)
        assert right["center"]["lon"] == pytest.approx(-81.25)

    def test_horizontal_split_uses_web_mercator_midpoint(self):
        """Horizontal split: the latitude midpoint is computed in Web
        Mercator, NOT as the arithmetic mean (north+south)/2.

        For lat 40 and 39, the Web Mercator Y midpoint reprojected back is
        NOT 39.5 -- it is slightly different because Web Mercator is
        non-linear in latitude.
        """
        bounds = _bounds(40.0, 39.0, -82.0, -81.0)
        top, bottom = _split_bounds(bounds, "horizontal")

        # Compute expected mid_lat via Web Mercator
        to_wm = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
        to_wgs84 = pyproj.Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
        _, y_n = to_wm.transform(-82.0, 40.0)
        _, y_s = to_wm.transform(-82.0, 39.0)
        y_mid = (y_n + y_s) / 2.0
        _, mid_lat = to_wgs84.transform(-82.0, y_mid)

        # Top half: lat [mid_lat, 40]
        assert top["northWest"]["lat"] == pytest.approx(40.0)
        assert top["southEast"]["lat"] == pytest.approx(mid_lat)

        # Bottom half: lat [39, mid_lat]
        assert bottom["northWest"]["lat"] == pytest.approx(mid_lat)
        assert bottom["southEast"]["lat"] == pytest.approx(39.0)

        # Longitude unchanged for both halves
        assert top["northWest"]["lon"] == pytest.approx(-82.0)
        assert top["northEast"]["lon"] == pytest.approx(-81.0)
        assert bottom["northWest"]["lon"] == pytest.approx(-82.0)
        assert bottom["northEast"]["lon"] == pytest.approx(-81.0)

    def test_horizontal_split_midpoint_not_arithmetic_mean(self):
        """Explicitly verify the mid-latitude differs from (north+south)/2.

        At lat 60 and 40 the difference is more pronounced.
        """
        bounds = _bounds(60.0, 40.0, 0.0, 10.0)
        top, bottom = _split_bounds(bounds, "horizontal")

        arithmetic_mid = (60.0 + 40.0) / 2.0  # = 50.0
        actual_mid = top["southEast"]["lat"]

        # The Web Mercator midpoint is NOT 50.0 for these latitudes
        assert actual_mid != pytest.approx(arithmetic_mid, abs=1e-6)
        # But it should be reasonably close
        assert 48.0 < actual_mid < 52.0

    def test_split_bounds_at_equator_is_arithmetic(self):
        """Near the equator, Web Mercator is approximately linear in latitude,
        so the midpoint should be very close to the arithmetic mean."""
        bounds = _bounds(1.0, -1.0, 0.0, 10.0)
        top, bottom = _split_bounds(bounds, "horizontal")
        mid = top["southEast"]["lat"]
        assert mid == pytest.approx(0.0, abs=1e-3)


# ---------------------------------------------------------------------------
# main (end-to-end)
# ---------------------------------------------------------------------------


class TestMain:
    def test_split_wide_image_auto_vertical(self, tmp_path):
        """A wide image auto-splits vertically. Two halves written, manifest
        updated, sibling files copied."""
        src = tmp_path / "src"
        out = tmp_path / "out"
        entries = [
            _entry("gathered_r0_c0", 40.0, 39.0, -82.0, -81.0),
            _entry("gathered_r0_c1", 40.0, 39.0, -81.0, -80.0),
        ]
        _make_terrain_dir(
            src,
            entries,
            image_sizes={"gathered_r0_c0": (100, 40), "gathered_r0_c1": (100, 40)},
            extra_files={"Shape.json": "{}"},
        )

        result = main(str(src / "gathered_r0_c0.png"), str(out))

        assert result["axis"] == "vertical"
        assert result["original_name"] == "gathered_r0_c0"
        assert result["first"] == "gathered_r0_c0_a"
        assert result["second"] == "gathered_r0_c0_b"

        # Two new images on disk
        assert (out / "gathered_r0_c0_a.png").is_file()
        assert (out / "gathered_r0_c0_b.png").is_file()
        # Original NOT copied
        assert not (out / "gathered_r0_c0.png").is_file()
        # Sibling image copied
        assert (out / "gathered_r0_c1.png").is_file()
        # Non-image file copied
        assert (out / "Shape.json").is_file()

        # Image sizes: 100 -> 50 + 50
        with pImage.open(out / "gathered_r0_c0_a.png") as im:
            assert im.size == (50, 40)
        with pImage.open(out / "gathered_r0_c0_b.png") as im:
            assert im.size == (50, 40)

        # Manifest: original replaced by two entries, sibling preserved
        manifest = json.loads((out / "height_info.json").read_text())
        names = [e["name"] for e in manifest["images"]]
        assert "gathered_r0_c0" not in names
        assert "gathered_r0_c0_a" in names
        assert "gathered_r0_c0_b" in names
        assert "gathered_r0_c1" in names
        assert len(manifest["images"]) == 3

    def test_split_tall_image_auto_horizontal(self, tmp_path):
        """A tall image auto-splits horizontally."""
        src = tmp_path / "src"
        out = tmp_path / "out"
        entries = [_entry("gathered_r0_c0", 40.0, 38.0, -82.0, -81.0)]
        _make_terrain_dir(src, entries, image_sizes={"gathered_r0_c0": (40, 100)})

        result = main(str(src / "gathered_r0_c0.png"), str(out))

        assert result["axis"] == "horizontal"
        with pImage.open(out / "gathered_r0_c0_a.png") as im:
            assert im.size == (40, 50)
        with pImage.open(out / "gathered_r0_c0_b.png") as im:
            assert im.size == (40, 50)

    def test_odd_dimension_split(self, tmp_path):
        """Odd width: first half gets floor(w/2), second gets the remainder."""
        src = tmp_path / "src"
        out = tmp_path / "out"
        entries = [_entry("gathered_r0_c0", 40.0, 39.0, -82.0, -81.0)]
        _make_terrain_dir(src, entries, image_sizes={"gathered_r0_c0": (101, 40)})

        main(str(src / "gathered_r0_c0.png"), str(out), axis="vertical")

        with pImage.open(out / "gathered_r0_c0_a.png") as im:
            assert im.size == (50, 40)
        with pImage.open(out / "gathered_r0_c0_b.png") as im:
            assert im.size == (51, 40)

    def test_elevation_files_preserved(self, tmp_path):
        """elevation_files in the manifest are carried through to the output."""
        src = tmp_path / "src"
        out = tmp_path / "out"
        entries = [_entry("gathered_r0_c0", 40.0, 39.0, -82.0, -81.0)]
        _make_terrain_dir(
            src,
            entries,
            image_sizes={"gathered_r0_c0": (100, 40)},
            elevation_files=["elevation_merged.tif"],
        )

        main(str(src / "gathered_r0_c0.png"), str(out))

        manifest = json.loads((out / "height_info.json").read_text())
        assert manifest["elevation_files"] == ["elevation_merged.tif"]
        assert (out / "elevation_merged.tif").is_file()

    def test_forced_vertical_axis(self, tmp_path):
        """--axis vertical forces left/right even on a tall image."""
        src = tmp_path / "src"
        out = tmp_path / "out"
        entries = [_entry("gathered_r0_c0", 40.0, 38.0, -82.0, -81.0)]
        _make_terrain_dir(src, entries, image_sizes={"gathered_r0_c0": (40, 100)})

        result = main(str(src / "gathered_r0_c0.png"), str(out), axis="vertical")
        assert result["axis"] == "vertical"
        with pImage.open(out / "gathered_r0_c0_a.png") as im:
            assert im.size == (20, 100)

    def test_forced_horizontal_axis(self, tmp_path):
        """--axis horizontal forces top/bottom even on a wide image."""
        src = tmp_path / "src"
        out = tmp_path / "out"
        entries = [_entry("gathered_r0_c0", 40.0, 39.0, -82.0, -81.0)]
        _make_terrain_dir(src, entries, image_sizes={"gathered_r0_c0": (100, 40)})

        result = main(str(src / "gathered_r0_c0.png"), str(out), axis="horizontal")
        assert result["axis"] == "horizontal"
        with pImage.open(out / "gathered_r0_c0_a.png") as im:
            assert im.size == (100, 20)

    def test_manifest_entry_position_preserved(self, tmp_path):
        """The two new entries replace the original at its position in the
        manifest's images array (not appended at the end)."""
        src = tmp_path / "src"
        out = tmp_path / "out"
        entries = [
            _entry("gathered_r0_c0", 40.0, 39.0, -82.0, -81.0),
            _entry("gathered_r0_c1", 40.0, 39.0, -81.0, -80.0),
            _entry("gathered_r1_c0", 39.0, 38.0, -82.0, -81.0),
        ]
        _make_terrain_dir(
            src, entries, image_sizes={e["name"]: (100, 40) for e in entries}
        )

        main(str(src / "gathered_r0_c1.png"), str(out), axis="vertical")

        manifest = json.loads((out / "height_info.json").read_text())
        names = [e["name"] for e in manifest["images"]]
        # r0_c1 is at index 1; the two halves replace it in-place
        assert names[0] == "gathered_r0_c0"
        assert names[1] == "gathered_r0_c1_a"
        assert names[2] == "gathered_r0_c1_b"
        assert names[3] == "gathered_r1_c0"

    def test_split_image_not_in_manifest_raises(self, tmp_path):
        """If the image is not listed in the manifest, raise ValueError."""
        src = tmp_path / "src"
        out = tmp_path / "out"
        entries = [_entry("gathered_r0_c0", 40.0, 39.0, -82.0, -81.0)]
        _make_terrain_dir(src, entries, image_sizes={"gathered_r0_c0": (100, 40)})
        # Create an extra image not in the manifest
        pImage.new("RGB", (100, 40)).save(src / "gathered_r0_c5.png")

        with pytest.raises(ValueError, match="not found"):
            main(str(src / "gathered_r0_c5.png"), str(out))

    def test_missing_manifest_raises(self, tmp_path):
        """If height_info.json doesn't exist in the image's directory, raise."""
        src = tmp_path / "src"
        out = tmp_path / "out"
        src.mkdir()
        pImage.new("RGB", (100, 40)).save(src / "gathered_r0_c0.png")

        with pytest.raises(FileNotFoundError, match="height_info.json"):
            main(str(src / "gathered_r0_c0.png"), str(out))

    def test_invalid_axis_raises(self, tmp_path):
        src = tmp_path / "src"
        out = tmp_path / "out"
        entries = [_entry("gathered_r0_c0", 40.0, 39.0, -82.0, -81.0)]
        _make_terrain_dir(src, entries, image_sizes={"gathered_r0_c0": (100, 40)})

        with pytest.raises(ValueError, match="axis must be"):
            main(str(src / "gathered_r0_c0.png"), str(out), axis="diagonal")

    def test_missing_image_raises(self, tmp_path):
        src = tmp_path / "src"
        out = tmp_path / "out"
        entries = [_entry("gathered_r0_c0", 40.0, 39.0, -82.0, -81.0)]
        _make_terrain_dir(src, entries)

        with pytest.raises(FileNotFoundError, match="Image not found"):
            main(str(src / "nonexistent.png"), str(out))

    def test_output_dir_created(self, tmp_path):
        """The output directory is created if it does not exist."""
        src = tmp_path / "src"
        out = tmp_path / "deeply" / "nested" / "out"
        entries = [_entry("gathered_r0_c0", 40.0, 39.0, -82.0, -81.0)]
        _make_terrain_dir(src, entries, image_sizes={"gathered_r0_c0": (100, 40)})

        main(str(src / "gathered_r0_c0.png"), str(out))
        assert out.is_dir()

    def test_rgba_image_preserved(self, tmp_path):
        """RGBA images keep their mode after splitting."""
        src = tmp_path / "src"
        out = tmp_path / "out"
        entries = [_entry("gathered_r0_c0", 40.0, 39.0, -82.0, -81.0)]
        _make_terrain_dir(src, entries, image_sizes={"gathered_r0_c0": (100, 40)})
        # Overwrite with RGBA
        pImage.new("RGBA", (100, 40), (255, 0, 0, 128)).save(src / "gathered_r0_c0.png")

        main(str(src / "gathered_r0_c0.png"), str(out), axis="vertical")
        with pImage.open(out / "gathered_r0_c0_a.png") as im:
            assert im.mode == "RGBA"

    def test_vertical_split_bounds_match_image_extent(self, tmp_path):
        """The left half's bounds cover the western half of the original,
        the right half covers the eastern half. Longitude midpoint is exact."""
        src = tmp_path / "src"
        out = tmp_path / "out"
        entries = [_entry("gathered_r0_c0", 40.0, 39.0, -82.0, -80.0)]
        _make_terrain_dir(src, entries, image_sizes={"gathered_r0_c0": (200, 100)})

        main(str(src / "gathered_r0_c0.png"), str(out), axis="vertical")

        manifest = json.loads((out / "height_info.json").read_text())
        by_name = {e["name"]: e for e in manifest["images"]}
        left = by_name["gathered_r0_c0_a"]
        right = by_name["gathered_r0_c0_b"]

        # Left: lon [-82, -81], right: lon [-81, -80]
        assert left["bounds"]["northWest"]["lon"] == pytest.approx(-82.0)
        assert left["bounds"]["northEast"]["lon"] == pytest.approx(-81.0)
        assert right["bounds"]["northWest"]["lon"] == pytest.approx(-81.0)
        assert right["bounds"]["northEast"]["lon"] == pytest.approx(-80.0)
        # Latitude unchanged for both
        assert left["bounds"]["northWest"]["lat"] == pytest.approx(40.0)
        assert left["bounds"]["southEast"]["lat"] == pytest.approx(39.0)
        assert right["bounds"]["northWest"]["lat"] == pytest.approx(40.0)
        assert right["bounds"]["southEast"]["lat"] == pytest.approx(39.0)


# ---------------------------------------------------------------------------
# Multithreaded behaviour
# ---------------------------------------------------------------------------


class TestThreading:
    def test_workers_parameter_produces_correct_output(self, tmp_path):
        """The workers parameter doesn't change the output — just the
        concurrency. Results are identical regardless of thread count."""
        src = tmp_path / "src"
        out1 = tmp_path / "out_1worker"
        out2 = tmp_path / "out_4workers"
        entries = [
            _entry("gathered_r0_c0", 40.0, 39.0, -82.0, -81.0),
            _entry("gathered_r0_c1", 40.0, 39.0, -81.0, -80.0),
            _entry("gathered_r1_c0", 39.0, 38.0, -82.0, -81.0),
        ]
        for d in (out1, out2):
            _make_terrain_dir(
                src,
                entries,
                image_sizes={e["name"]: (100, 40) for e in entries},
                extra_files={"Shape.json": "{}"},
            )

        main(str(src / "gathered_r0_c0.png"), str(out1), axis="vertical", workers=1)
        main(str(src / "gathered_r0_c0.png"), str(out2), axis="vertical", workers=4)

        # Both outputs should have the same files
        assert set(os.listdir(out1)) == set(os.listdir(out2))

        # Image sizes match
        for suffix in ("_a", "_b"):
            with (
                pImage.open(out1 / f"gathered_r0_c0{suffix}.png") as im1,
                pImage.open(out2 / f"gathered_r0_c0{suffix}.png") as im2,
            ):
                assert im1.size == im2.size

        # Manifests match
        m1 = json.loads((out1 / "height_info.json").read_text())
        m2 = json.loads((out2 / "height_info.json").read_text())
        assert m1 == m2

    def test_workers_defaults_to_cpu_count(self, tmp_path):
        """When workers=None, the pool uses os.cpu_count() (capped by task
        count). The command should still succeed."""
        src = tmp_path / "src"
        out = tmp_path / "out"
        entries = [_entry("gathered_r0_c0", 40.0, 39.0, -82.0, -81.0)]
        _make_terrain_dir(src, entries, image_sizes={"gathered_r0_c0": (100, 40)})

        result = main(str(src / "gathered_r0_c0.png"), str(out), workers=None)
        assert result["axis"] == "vertical"
        assert (out / "gathered_r0_c0_a.png").is_file()
        assert (out / "gathered_r0_c0_b.png").is_file()

    def test_workers_zero_raises_or_clamped(self, tmp_path):
        """workers=0 should be clamped to at least 1 (min() in the code)."""
        src = tmp_path / "src"
        out = tmp_path / "out"
        entries = [_entry("gathered_r0_c0", 40.0, 39.0, -82.0, -81.0)]
        _make_terrain_dir(src, entries, image_sizes={"gathered_r0_c0": (100, 40)})

        # max(1, min(0, n_tasks)) == max(1, 0) == 1, so it should work
        result = main(str(src / "gathered_r0_c0.png"), str(out), workers=0)
        assert (out / "gathered_r0_c0_a.png").is_file()

    def test_split_image_load_makes_halves_independent(self, tmp_path):
        """_split_image calls .load() on both halves, so they can be saved
        after the source image is closed (which is what happens in the
        threaded save path)."""
        src = tmp_path / "src"
        src.mkdir(parents=True, exist_ok=True)
        # Create a distinct-color image so we can verify pixel fidelity
        img = pImage.new("RGB", (100, 40), (123, 45, 67))
        img.save(src / "test.png")
        img.close()

        with pImage.open(src / "test.png") as src_img:
            left, right = _split_image(src_img, "vertical")
        # src_img is now closed; saving the halves must still work
        left_out = str(src / "left.png")
        right_out = str(src / "right.png")
        _save_image_atomic(left, left_out)
        _save_image_atomic(right, right_out)

        with pImage.open(left_out) as im:
            assert im.size == (50, 40)
            assert im.getpixel((0, 0)) == (123, 45, 67)
        with pImage.open(right_out) as im:
            assert im.size == (50, 40)
            assert im.getpixel((0, 0)) == (123, 45, 67)

    def test_many_sibling_files_copied_concurrently(self, tmp_path):
        """A directory with many sibling files: all are copied to the output
        and none are missing (the thread pool doesn't drop tasks)."""
        src = tmp_path / "src"
        out = tmp_path / "out"
        entries = [
            _entry(f"gathered_r{r}_c{c}", 40.0 - r, 39.0 - r, -82.0 + c, -81.0 + c)
            for r in range(5)
            for c in range(5)
        ]
        extra = {f"sidecar_{i}.json": json.dumps({"i": i}) for i in range(10)}
        _make_terrain_dir(
            src,
            entries,
            image_sizes={e["name"]: (100, 40) for e in entries},
            extra_files=extra,
        )

        # Split one image; the other 24 images + 10 sidecars should be copied
        main(str(src / "gathered_r0_c0.png"), str(out), axis="vertical", workers=8)

        out_files = set(os.listdir(out))
        # The split image's two halves
        assert "gathered_r0_c0_a.png" in out_files
        assert "gathered_r0_c0_b.png" in out_files
        # Original not copied
        assert "gathered_r0_c0.png" not in out_files
        # All other 24 gathered images
        for r in range(5):
            for c in range(5):
                if (r, c) == (0, 0):
                    continue
                assert f"gathered_r{r}_c{c}.png" in out_files
        # All 10 sidecar files
        for i in range(10):
            assert f"sidecar_{i}.json" in out_files
        # Manifest present
        assert "height_info.json" in out_files

    def test_atomic_save_leaves_no_temp_files(self, tmp_path):
        """The temp-then-replace save pattern should not leave .tmp files."""
        src = tmp_path / "src"
        out = tmp_path / "out"
        entries = [_entry("gathered_r0_c0", 40.0, 39.0, -82.0, -81.0)]
        _make_terrain_dir(src, entries, image_sizes={"gathered_r0_c0": (100, 40)})

        main(str(src / "gathered_r0_c0.png"), str(out), workers=4)

        tmp_files = [f for f in os.listdir(out) if f.endswith(".tmp")]
        assert tmp_files == []
