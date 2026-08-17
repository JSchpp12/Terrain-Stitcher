"""Tests for the split-image command (ImageSplitter.main).

Covers:
  * Image cropping (no resizing) for vertical and horizontal splits
  * Bounds computation: vertical split uses linear longitude midpoint;
    horizontal split uses Web Mercator Y midpoint (not arithmetic lat mean)
  * Odd-dimension pixel handling (one half gets the extra pixel)
  * Directory-level split: every image in the directory is split, manifest
    updated with all original entries replaced by their two halves in-order
  * Sibling-file passthrough (non-image files copied, images excluded)
  * Auto axis detection (wider -> vertical, taller -> horizontal)
  * Multithreading: workers parameter, thread independence, many images
  * Error cases: directory not found, manifest missing, invalid axis
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
    extra_files : optional dict mapping filename -> bytes/string content
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
        assert left["center"]["lon"] == pytest.approx(-81.75)
        assert right["center"]["lon"] == pytest.approx(-81.25)

    def test_horizontal_split_uses_web_mercator_midpoint(self):
        """Horizontal split: the latitude midpoint is computed in Web
        Mercator, NOT as the arithmetic mean (north+south)/2."""
        bounds = _bounds(40.0, 39.0, -82.0, -81.0)
        top, bottom = _split_bounds(bounds, "horizontal")

        # Compute expected mid_lat via Web Mercator
        to_wm = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
        to_wgs84 = pyproj.Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
        _, y_n = to_wm.transform(-82.0, 40.0)
        _, y_s = to_wm.transform(-82.0, 39.0)
        y_mid = (y_n + y_s) / 2.0
        _, mid_lat = to_wgs84.transform(-82.0, y_mid)

        assert top["northWest"]["lat"] == pytest.approx(40.0)
        assert top["southEast"]["lat"] == pytest.approx(mid_lat)
        assert bottom["northWest"]["lat"] == pytest.approx(mid_lat)
        assert bottom["southEast"]["lat"] == pytest.approx(39.0)

        # Longitude unchanged for both halves
        assert top["northWest"]["lon"] == pytest.approx(-82.0)
        assert top["northEast"]["lon"] == pytest.approx(-81.0)
        assert bottom["northWest"]["lon"] == pytest.approx(-82.0)
        assert bottom["northEast"]["lon"] == pytest.approx(-81.0)

    def test_horizontal_split_midpoint_not_arithmetic_mean(self):
        """The mid-latitude differs from (north+south)/2 at higher latitudes."""
        bounds = _bounds(60.0, 40.0, 0.0, 10.0)
        top, bottom = _split_bounds(bounds, "horizontal")

        arithmetic_mid = (60.0 + 40.0) / 2.0  # = 50.0
        actual_mid = top["southEast"]["lat"]

        assert actual_mid != pytest.approx(arithmetic_mid, abs=1e-6)
        assert 48.0 < actual_mid < 52.0

    def test_split_bounds_at_equator_is_arithmetic(self):
        """Near the equator, Web Mercator is approximately linear in latitude."""
        bounds = _bounds(1.0, -1.0, 0.0, 10.0)
        top, bottom = _split_bounds(bounds, "horizontal")
        mid = top["southEast"]["lat"]
        assert mid == pytest.approx(0.0, abs=1e-3)


# ---------------------------------------------------------------------------
# main (directory-level, multithreaded)
# ---------------------------------------------------------------------------


class TestMain:
    def test_split_all_images_in_directory(self, tmp_path):
        """Every image in the directory is split. The manifest has each
        original entry replaced by its two halves in-order."""
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

        result = main(str(src), str(out), axis="vertical")

        assert result["split_count"] == 3
        assert result["total_images"] == 6

        # Two halves per image on disk
        for name in ("gathered_r0_c0", "gathered_r0_c1", "gathered_r1_c0"):
            assert (out / f"{name}_a.png").is_file()
            assert (out / f"{name}_b.png").is_file()
            assert not (out / f"{name}.png").is_file()

        # Manifest: 6 entries, original order preserved
        manifest = json.loads((out / "height_info.json").read_text())
        names = [e["name"] for e in manifest["images"]]
        assert names == [
            "gathered_r0_c0_a",
            "gathered_r0_c0_b",
            "gathered_r0_c1_a",
            "gathered_r0_c1_b",
            "gathered_r1_c0_a",
            "gathered_r1_c0_b",
        ]

    def test_auto_axis_per_image(self, tmp_path):
        """Auto axis is resolved per image: a wide image splits vertically,
        a tall image splits horizontally."""
        src = tmp_path / "src"
        out = tmp_path / "out"
        entries = [
            _entry("wide_img", 40.0, 39.0, -82.0, -81.0),
            _entry("tall_img", 40.0, 38.0, -82.0, -81.0),
        ]
        _make_terrain_dir(
            src, entries, image_sizes={"wide_img": (100, 40), "tall_img": (40, 100)}
        )

        main(str(src), str(out))

        # Wide -> vertical (left/right): each half is 50x40
        with pImage.open(out / "wide_img_a.png") as im:
            assert im.size == (50, 40)
        # Tall -> horizontal (top/bottom): each half is 40x50
        with pImage.open(out / "tall_img_a.png") as im:
            assert im.size == (40, 50)

    def test_forced_axis_applies_to_all(self, tmp_path):
        """--axis horizontal forces top/bottom for every image, even wide ones."""
        src = tmp_path / "src"
        out = tmp_path / "out"
        entries = [
            _entry("wide_img", 40.0, 39.0, -82.0, -81.0),
            _entry("tall_img", 40.0, 38.0, -82.0, -81.0),
        ]
        _make_terrain_dir(
            src, entries, image_sizes={"wide_img": (100, 40), "tall_img": (40, 100)}
        )

        main(str(src), str(out), axis="horizontal")

        with pImage.open(out / "wide_img_a.png") as im:
            assert im.size == (100, 20)
        with pImage.open(out / "tall_img_a.png") as im:
            assert im.size == (40, 50)

    def test_odd_dimensions(self, tmp_path):
        """Odd width: first half gets floor(w/2), second gets the remainder."""
        src = tmp_path / "src"
        out = tmp_path / "out"
        entries = [_entry("gathered_r0_c0", 40.0, 39.0, -82.0, -81.0)]
        _make_terrain_dir(src, entries, image_sizes={"gathered_r0_c0": (101, 40)})

        main(str(src), str(out), axis="vertical")

        with pImage.open(out / "gathered_r0_c0_a.png") as im:
            assert im.size == (50, 40)
        with pImage.open(out / "gathered_r0_c0_b.png") as im:
            assert im.size == (51, 40)

    def test_sibling_files_copied(self, tmp_path):
        """Non-image files are copied to the output; original images are not."""
        src = tmp_path / "src"
        out = tmp_path / "out"
        entries = [_entry("gathered_r0_c0", 40.0, 39.0, -82.0, -81.0)]
        _make_terrain_dir(
            src,
            entries,
            image_sizes={"gathered_r0_c0": (100, 40)},
            extra_files={"Shape.json": "{}", "README.txt": "hello"},
        )

        main(str(src), str(out), axis="vertical")

        assert (out / "Shape.json").is_file()
        assert (out / "README.txt").is_file()
        assert not (out / "gathered_r0_c0.png").is_file()

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

        main(str(src), str(out), axis="vertical")

        manifest = json.loads((out / "height_info.json").read_text())
        assert manifest["elevation_files"] == ["elevation_merged.tif"]
        assert (out / "elevation_merged.tif").is_file()

    def test_manifest_entry_without_png_passes_through(self, tmp_path):
        """A manifest entry whose PNG doesn't exist on disk is kept as-is
        (not split)."""
        src = tmp_path / "src"
        out = tmp_path / "out"
        entries = [
            _entry("gathered_r0_c0", 40.0, 39.0, -82.0, -81.0),
            _entry("gathered_r0_c1", 40.0, 39.0, -81.0, -80.0),
        ]
        # Only create the first image's PNG
        _make_terrain_dir(src, [entries[0]], image_sizes={"gathered_r0_c0": (100, 40)})
        # Write manifest with both entries
        _write_manifest(src, entries)

        main(str(src), str(out), axis="vertical")

        manifest = json.loads((out / "height_info.json").read_text())
        names = [e["name"] for e in manifest["images"]]
        # r0_c0 split into _a/_b; r0_c1 passes through unchanged
        assert "gathered_r0_c0_a" in names
        assert "gathered_r0_c0_b" in names
        assert "gathered_r0_c1" in names
        # r0_c1's bounds are unchanged
        r0c1 = [e for e in manifest["images"] if e["name"] == "gathered_r0_c1"][0]
        assert r0c1["bounds"]["northWest"]["lon"] == pytest.approx(-81.0)

    def test_empty_directory_no_images(self, tmp_path):
        """A directory with a manifest but no image PNGs produces output
        with the manifest unchanged and no split images."""
        src = tmp_path / "src"
        out = tmp_path / "out"
        entries = [_entry("gathered_r0_c0", 40.0, 39.0, -82.0, -81.0)]
        src.mkdir(parents=True, exist_ok=True)
        _write_manifest(src, entries)
        result = main(str(src), str(out))
        assert result["split_count"] == 0
        assert result["total_images"] == 1
        manifest = json.loads((out / "height_info.json").read_text())
        assert manifest["images"][0]["name"] == "gathered_r0_c0"

    def test_rgba_images_preserved(self, tmp_path):
        """RGBA images keep their mode after splitting."""
        src = tmp_path / "src"
        out = tmp_path / "out"
        entries = [_entry("gathered_r0_c0", 40.0, 39.0, -82.0, -81.0)]
        _make_terrain_dir(src, entries, image_sizes={"gathered_r0_c0": (100, 40)})
        pImage.new("RGBA", (100, 40), (255, 0, 0, 128)).save(src / "gathered_r0_c0.png")

        main(str(src), str(out), axis="vertical")
        with pImage.open(out / "gathered_r0_c0_a.png") as im:
            assert im.mode == "RGBA"

    def test_output_dir_created(self, tmp_path):
        """The output directory is created if it does not exist."""
        src = tmp_path / "src"
        out = tmp_path / "deeply" / "nested" / "out"
        entries = [_entry("gathered_r0_c0", 40.0, 39.0, -82.0, -81.0)]
        _make_terrain_dir(src, entries, image_sizes={"gathered_r0_c0": (100, 40)})

        main(str(src), str(out))
        assert out.is_dir()

    def test_vertical_split_bounds_match_image_extent(self, tmp_path):
        """The left half's bounds cover the western half, right the eastern."""
        src = tmp_path / "src"
        out = tmp_path / "out"
        entries = [_entry("gathered_r0_c0", 40.0, 39.0, -82.0, -80.0)]
        _make_terrain_dir(src, entries, image_sizes={"gathered_r0_c0": (200, 100)})

        main(str(src), str(out), axis="vertical")

        manifest = json.loads((out / "height_info.json").read_text())
        by_name = {e["name"]: e for e in manifest["images"]}
        left = by_name["gathered_r0_c0_a"]
        right = by_name["gathered_r0_c0_b"]

        assert left["bounds"]["northWest"]["lon"] == pytest.approx(-82.0)
        assert left["bounds"]["northEast"]["lon"] == pytest.approx(-81.0)
        assert right["bounds"]["northWest"]["lon"] == pytest.approx(-81.0)
        assert right["bounds"]["northEast"]["lon"] == pytest.approx(-80.0)

    # --- Error cases ---------------------------------------------------------

    def test_missing_directory_raises(self, tmp_path):
        out = tmp_path / "out"
        with pytest.raises(FileNotFoundError, match="Input directory not found"):
            main(str(tmp_path / "nonexistent"), str(out))

    def test_missing_manifest_raises(self, tmp_path):
        src = tmp_path / "src"
        out = tmp_path / "out"
        src.mkdir()
        pImage.new("RGB", (10, 10)).save(src / "gathered_r0_c0.png")
        with pytest.raises(FileNotFoundError, match="height_info.json"):
            main(str(src), str(out))

    def test_invalid_axis_raises(self, tmp_path):
        src = tmp_path / "src"
        out = tmp_path / "out"
        entries = [_entry("gathered_r0_c0", 40.0, 39.0, -82.0, -81.0)]
        _make_terrain_dir(src, entries, image_sizes={"gathered_r0_c0": (100, 40)})
        with pytest.raises(ValueError, match="axis must be"):
            main(str(src), str(out), axis="diagonal")


# ---------------------------------------------------------------------------
# Multithreaded behaviour
# ---------------------------------------------------------------------------


class TestThreading:
    def test_workers_parameter_produces_correct_output(self, tmp_path):
        """The workers parameter doesn't change the output — just the
        concurrency. Results are identical regardless of thread count."""
        src = tmp_path / "src"
        out1 = tmp_path / "out_1w"
        out2 = tmp_path / "out_4w"
        entries = [
            _entry("gathered_r0_c0", 40.0, 39.0, -82.0, -81.0),
            _entry("gathered_r0_c1", 40.0, 39.0, -81.0, -80.0),
            _entry("gathered_r1_c0", 39.0, 38.0, -82.0, -81.0),
        ]
        _make_terrain_dir(
            src, entries, image_sizes={e["name"]: (100, 40) for e in entries}
        )

        main(str(src), str(out1), axis="vertical", workers=1)
        main(str(src), str(out2), axis="vertical", workers=4)

        assert set(os.listdir(out1)) == set(os.listdir(out2))
        m1 = json.loads((out1 / "height_info.json").read_text())
        m2 = json.loads((out2 / "height_info.json").read_text())
        assert m1 == m2

    def test_workers_defaults_to_cpu_count(self, tmp_path):
        """When workers=None, the pool uses os.cpu_count() (capped by image
        count). The command should still succeed."""
        src = tmp_path / "src"
        out = tmp_path / "out"
        entries = [_entry("gathered_r0_c0", 40.0, 39.0, -82.0, -81.0)]
        _make_terrain_dir(src, entries, image_sizes={"gathered_r0_c0": (100, 40)})

        result = main(str(src), str(out), workers=None)
        assert result["split_count"] == 1
        assert (out / "gathered_r0_c0_a.png").is_file()

    def test_workers_zero_clamped_to_one(self, tmp_path):
        """workers=0 is clamped to 1 by the max(1, ...) guard."""
        src = tmp_path / "src"
        out = tmp_path / "out"
        entries = [_entry("gathered_r0_c0", 40.0, 39.0, -82.0, -81.0)]
        _make_terrain_dir(src, entries, image_sizes={"gathered_r0_c0": (100, 40)})

        result = main(str(src), str(out), workers=0)
        assert result["num_workers"] == 1

    def test_split_image_load_makes_halves_independent(self, tmp_path):
        """_split_image calls .load() so halves survive after the source
        image is closed."""
        d = tmp_path / "d"
        d.mkdir()
        pImage.new("RGB", (100, 40), (123, 45, 67)).save(d / "test.png")
        with pImage.open(d / "test.png") as src_img:
            left, right = _split_image(src_img, "vertical")
        # src_img closed; saving halves must still work
        _save_image_atomic(left, str(d / "left.png"))
        _save_image_atomic(right, str(d / "right.png"))
        with pImage.open(d / "left.png") as im:
            assert im.size == (50, 40)
            assert im.getpixel((0, 0)) == (123, 45, 67)

    def test_many_images_split_concurrently(self, tmp_path):
        """A directory with many images: all are split, none are missing."""
        src = tmp_path / "src"
        out = tmp_path / "out"
        entries = [
            _entry(f"gathered_r{r}_c{c}", 40.0 - r, 39.0 - r, -82.0 + c, -81.0 + c)
            for r in range(5)
            for c in range(5)
        ]
        _make_terrain_dir(
            src,
            entries,
            image_sizes={e["name"]: (100, 40) for e in entries},
            extra_files={"Shape.json": "{}"},
        )

        result = main(str(src), str(out), axis="vertical", workers=8)
        assert result["split_count"] == 25
        assert result["total_images"] == 50

        out_files = set(os.listdir(out))
        for r in range(5):
            for c in range(5):
                assert f"gathered_r{r}_c{c}_a.png" in out_files
                assert f"gathered_r{r}_c{c}_b.png" in out_files
                assert f"gathered_r{r}_c{c}.png" not in out_files
        assert "Shape.json" in out_files
        assert "height_info.json" in out_files

    def test_atomic_save_leaves_no_temp_files(self, tmp_path):
        """The temp-then-replace save pattern leaves no .tmp files."""
        src = tmp_path / "src"
        out = tmp_path / "out"
        entries = [_entry("gathered_r0_c0", 40.0, 39.0, -82.0, -81.0)]
        _make_terrain_dir(src, entries, image_sizes={"gathered_r0_c0": (100, 40)})

        main(str(src), str(out), workers=4)
        tmp_files = [f for f in os.listdir(out) if f.endswith(".tmp")]
        assert tmp_files == []
