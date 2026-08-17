"""Split every gathered terrain image in a directory in half and update the
manifest.

Takes a terrain output directory (produced by ``gather-ortho -src arcgis`` or
``process-terrain``) containing ``gathered_r*_c*.png`` images plus a
``height_info.json`` manifest, splits every listed image in half along its
longer pixel dimension (no resizing — pure crop), and writes the split halves
plus an updated ``height_info.json`` to a new output directory. All non-image
sibling files (``elevation_merged.tif``, ``Shape.json``, sidecars) are copied
to the output so it stays self-contained.

Each image is split in its own worker thread (``ThreadPoolExecutor``), so all
images in the directory are split concurrently. PNG encoding releases the GIL,
so the halves from different images encode simultaneously. The main thread
collects all split results and writes the single updated manifest after every
worker finishes — the manifest is never touched by worker threads.

The bounds for each half are computed in Web Mercator (EPSG:3857) because
image pixels are linearly spaced in projected space, not in WGS84 lat/lon:

  * **Vertical split** (left/right, dividing longitude): longitude is linear
    with Web Mercator X, so the pixel midpoint is exactly ``(west + east) / 2``.

  * **Horizontal split** (top/bottom, dividing latitude): latitude is
    **non-linear** with Web Mercator Y. The pixel midpoint in projected space
    is ``(y_north + y_south) / 2``, which does NOT map to
    ``(north_lat + south_lat) / 2``. We convert north/south latitudes to
    Web Mercator Y, compute the midpoint Y, and convert back to latitude via
    ``pyproj`` (already a dependency).
"""

from __future__ import annotations

import json
import os
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import pyproj
from PIL import Image as pImage
from tqdm import tqdm

from terrain_stitcher.common import get_all_files_in_directory

# ---------------------------------------------------------------------------
# Axis helpers
# ---------------------------------------------------------------------------

_AXIS_AUTO = "auto"
_AXIS_VERTICAL = "vertical"  # left / right (split longitude)
_AXIS_HORIZONTAL = "horizontal"  # top / bottom (split latitude)

_VALID_AXES = (_AXIS_AUTO, _AXIS_VERTICAL, _AXIS_HORIZONTAL)


def _resolve_axis(width: int, height: int, axis: str) -> str:
    """Resolve 'auto' to a concrete axis based on pixel dimensions.

    Wider images split vertically (left/right); taller images split
    horizontally (top/bottom). A perfect square defaults to vertical.
    """
    if axis == _AXIS_VERTICAL:
        return _AXIS_VERTICAL
    if axis == _AXIS_HORIZONTAL:
        return _AXIS_HORIZONTAL
    # auto: longer dimension
    if height > width:
        return _AXIS_HORIZONTAL
    return _AXIS_VERTICAL


# ---------------------------------------------------------------------------
# Image splitting (crop only, no resizing)
# ---------------------------------------------------------------------------


def _split_image(img: pImage.Image, axis: str) -> tuple[pImage.Image, pImage.Image]:
    """Crop *img* into two halves along *axis*. Returns (first, second).

    For a vertical split: first = left half, second = right half.
    For a horizontal split: first = top half, second = bottom half.

    Odd dimensions: the first half gets the extra pixel row/column (i.e. it is
    one pixel larger along the split dimension). This matches integer floor
    division of the crop box.

    Both halves are ``.load()``-ed before returning so their pixel data is
    resident in memory and independent of the source image's file handle —
    this lets them be saved concurrently in threads after the source is closed.
    """
    w, h = img.size
    if axis == _AXIS_VERTICAL:
        mid = w // 2
        first = img.crop((0, 0, mid, h))
        second = img.crop((mid, 0, w, h))
    else:
        mid = h // 2
        first = img.crop((0, 0, w, mid))
        second = img.crop((0, mid, w, h))
    # Force pixel data into memory so the halves are independent of the
    # source image and can be encoded in parallel threads.
    first.load()
    second.load()
    return first, second


# ---------------------------------------------------------------------------
# Bounds splitting (Web Mercator for correctness)
# ---------------------------------------------------------------------------


def _build_bounds_dict(
    x_w: float, x_e: float, y_n: float, y_s: float, to_wgs84: pyproj.Transformer
) -> dict:
    """Build a manifest bounds dict from a Web Mercator rectangle.

    Corners are reprojected to WGS84: NW, NE, SE, SW, plus center (projected
    midpoint reprojected), matching the ArcGIS ``window_bounds`` approach.
    """
    xc = (x_w + x_e) / 2.0
    yc = (y_n + y_s) / 2.0
    # NW, NE, SE, SW, center  --  order matters for _build_bounds consumers
    lons, lats = to_wgs84.transform(
        [x_w, x_e, x_e, x_w, xc],
        [y_n, y_n, y_s, y_s, yc],
    )
    return {
        "northWest": {"lat": float(lats[0]), "lon": float(lons[0])},
        "northEast": {"lat": float(lats[1]), "lon": float(lons[1])},
        "southEast": {"lat": float(lats[2]), "lon": float(lons[2])},
        "southWest": {"lat": float(lats[3]), "lon": float(lons[3])},
        "center": {"lat": float(lats[4]), "lon": float(lons[4])},
    }


def _split_bounds(bounds_dict: dict, axis: str) -> tuple[dict, dict]:
    """Split a manifest bounds dict into two halves along *axis*.

    Computes the split in Web Mercator (EPSG:3857) so the geographic midpoint
    matches the pixel midpoint of the image.
    """
    nw = bounds_dict["northWest"]
    ne = bounds_dict["northEast"]
    sw = bounds_dict["southWest"]
    se = bounds_dict["southEast"]

    north = float(nw["lat"])
    south = float(sw["lat"])
    west = float(nw["lon"])
    east = float(ne["lon"])

    # Web Mercator transformers (built once, reused for all corners)
    to_wm = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    to_wgs84 = pyproj.Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)

    # Project the NW and SE corners to Web Mercator (rectangle extents)
    x_w, y_n = to_wm.transform(west, north)
    x_e, y_s = to_wm.transform(east, south)

    if axis == _AXIS_VERTICAL:
        # Split at X midpoint (longitude is linear with X)
        x_mid = (x_w + x_e) / 2.0
        first = _build_bounds_dict(x_w, x_mid, y_n, y_s, to_wgs84)  # left
        second = _build_bounds_dict(x_mid, x_e, y_n, y_s, to_wgs84)  # right
    else:
        # Split at Y midpoint (latitude is non-linear with Y)
        y_mid = (y_n + y_s) / 2.0
        first = _build_bounds_dict(x_w, x_e, y_n, y_mid, to_wgs84)  # top
        second = _build_bounds_dict(x_w, x_e, y_mid, y_s, to_wgs84)  # bottom

    return first, second


# ---------------------------------------------------------------------------
# Manifest handling
# ---------------------------------------------------------------------------

_MANIFEST_NAME = "height_info.json"


def _load_manifest(source_dir: str) -> dict:
    """Load height_info.json from *source_dir*."""
    manifest_path = os.path.join(source_dir, _MANIFEST_NAME)
    if not os.path.isfile(manifest_path):
        raise FileNotFoundError(
            f"{_MANIFEST_NAME} not found in {source_dir!r}. The split-image "
            "command expects a terrain output directory containing the "
            "manifest alongside the images."
        )
    with open(manifest_path) as f:
        return json.load(f)


def _save_manifest(output_dir: str, data: dict) -> str:
    """Write height_info.json to *output_dir* (atomic temp-then-replace)."""
    manifest_path = os.path.join(output_dir, _MANIFEST_NAME)
    tmp_path = manifest_path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, manifest_path)
    return manifest_path


# ---------------------------------------------------------------------------
# Worker: split one image (runs in a thread)
# ---------------------------------------------------------------------------


def _save_image_atomic(img: pImage.Image, out_path: str) -> str:
    """Save *img* to *out_path* via temp-then-replace (atomic on volume).

    The temp name has no .png extension, so format="PNG" is passed explicitly
    (PIL would otherwise infer the format from the extension and fail on
    ".tmp"). Matches the pattern used by ``_save_canvas`` in OrthoStitcher.
    """
    tmp_path = out_path + ".tmp"
    img.save(tmp_path, format="PNG")
    os.replace(tmp_path, out_path)
    return out_path


def _split_one_image(
    image_path: str,
    image_stem: str,
    bounds_dict: dict,
    output_dir: str,
    axis: str,
) -> dict:
    """Split one image in half and save the two halves. Worker function.

    Loads the image, crops it into two halves (no resizing), computes the new
    bounds for each half in Web Mercator, and saves both halves atomically to
    *output_dir*. Returns the metadata the main thread needs to update the
    manifest: original name, resolved axis, and the two new entries (name +
    bounds) that replace the original.

    This is designed to run inside a ``ThreadPoolExecutor`` worker: it is
    fully self-contained (no shared mutable state), and the PNG encode inside
    ``_save_image_atomic`` releases the GIL so multiple workers encode their
    halves simultaneously.
    """
    with pImage.open(image_path) as img:
        width, height = img.size
        resolved_axis = _resolve_axis(width, height, axis)
        first_img, second_img = _split_image(img, resolved_axis)
    # img is now closed; first_img/second_img are .load()-ed and independent

    first_bounds, second_bounds = _split_bounds(bounds_dict, resolved_axis)

    first_name = image_stem + "_a"
    second_name = image_stem + "_b"

    _save_image_atomic(first_img, os.path.join(output_dir, first_name + ".png"))
    _save_image_atomic(second_img, os.path.join(output_dir, second_name + ".png"))

    return {
        "original_name": image_stem,
        "resolved_axis": resolved_axis,
        "first_name": first_name,
        "first_bounds": first_bounds,
        "second_name": second_name,
        "second_bounds": second_bounds,
        "first_size": first_img.size,
        "second_size": second_img.size,
    }


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------


def main(
    input_dir: str,
    output_dir: str,
    axis: str = _AXIS_AUTO,
    workers: Optional[int] = None,
) -> dict:
    """Split every gathered image in a directory in half and update the manifest.

    Scans *input_dir* for images listed in its ``height_info.json`` manifest,
    splits each one in half (no resizing — pure crop) in a separate worker
    thread, and writes the split halves plus an updated manifest to
    *output_dir*. Non-image sibling files are copied through. The manifest is
    written once by the main thread after every worker finishes — worker
    threads never touch the manifest.

    Parameters
    ----------
    input_dir
        Terrain output directory containing ``gathered_r*_c*.png`` images and
        a ``height_info.json`` manifest.
    output_dir
        Directory to write the split images, updated ``height_info.json``, and
        copies of all sibling files. Created if it does not exist.
    axis
        Split axis: ``"auto"`` (default; each image splits along its own
        longer pixel dimension), ``"vertical"`` (left/right), or
        ``"horizontal"`` (top/bottom).
    workers
        Number of worker threads — one per image being split (default:
        ``os.cpu_count()``). PNG encoding releases the GIL, so the halves
        from different images encode concurrently. The pool is sized to
        ``min(workers, n_images)`` so idle threads are never created.

    Returns
    -------
    dict
        Summary with keys: ``split_count``, ``total_images``,
        ``manifest_path``, ``num_workers``.
    """
    if axis not in _VALID_AXES:
        raise ValueError(f"axis must be one of {_VALID_AXES!r}, got {axis!r}")
    if not os.path.isdir(input_dir):
        raise FileNotFoundError(f"Input directory not found: {input_dir!r}")

    # --- Read the manifest ---------------------------------------------------
    manifest = _load_manifest(input_dir)
    manifest_entries = manifest.get("images", [])

    # Partition manifest entries into those whose PNG exists (split) and
    # those without a PNG on disk (pass through unchanged).
    split_specs: list[tuple[str, str, dict]] = []
    passthrough_entries: list[dict] = []
    split_basenames: set[str] = set()
    for entry in manifest_entries:
        name = entry["name"]
        png_path = os.path.join(input_dir, name + ".png")
        if os.path.isfile(png_path):
            split_specs.append((png_path, name, entry["bounds"]))
            split_basenames.add(name + ".png")
        else:
            passthrough_entries.append(entry)

    os.makedirs(output_dir, exist_ok=True)

    if not split_specs:
        print("No images to split (no manifest entries with matching PNGs).")
    else:
        print(
            f"Splitting {len(split_specs)} image(s) on "
            f"{max(1, min(workers if workers is not None else os.cpu_count() or 1, len(split_specs)))} workers..."
        )

    # --- Split every image (one worker per image) ---------------------------
    num_workers = max(
        1,
        min(
            workers if workers is not None else os.cpu_count() or 1,
            len(split_specs),
        ),
    )

    results: list[dict] = []
    if split_specs:
        with ThreadPoolExecutor(max_workers=num_workers) as pool:
            futures = {
                pool.submit(
                    _split_one_image,
                    png_path,
                    name,
                    bounds_dict,
                    output_dir,
                    axis,
                ): name
                for png_path, name, bounds_dict in split_specs
            }
            # Progress bar: one tick per image split. Each worker's PNG
            # encode releases the GIL, so multiple workers run concurrently.
            with tqdm(total=len(futures), desc="Splitting images", unit="img") as pbar:
                for fut in as_completed(futures):
                    results.append(fut.result())
                    pbar.update(1)

    # --- Build the new manifest (main thread only) --------------------------
    # Preserve original manifest order: each split entry is replaced in-place
    # by its two halves; passthrough entries stay as-is.
    result_by_name = {r["original_name"]: r for r in results}

    new_images: list[dict] = []
    for entry in manifest_entries:
        name = entry["name"]
        if name in result_by_name:
            r = result_by_name[name]
            new_images.append({"name": r["first_name"], "bounds": r["first_bounds"]})
            new_images.append({"name": r["second_name"], "bounds": r["second_bounds"]})
        else:
            new_images.append(entry)

    new_manifest = {"images": new_images}
    if "elevation_files" in manifest:
        new_manifest["elevation_files"] = manifest["elevation_files"]

    # --- Copy sibling files (non-image, non-manifest) -----------------------
    sibling_count = 0
    for src_path in get_all_files_in_directory(input_dir):
        basename = os.path.basename(src_path)
        if basename in split_basenames:
            continue
        if basename == _MANIFEST_NAME:
            continue
        shutil.copy2(src_path, os.path.join(output_dir, basename))
        sibling_count += 1

    # --- Write the manifest --------------------------------------------------
    manifest_path = _save_manifest(output_dir, new_manifest)

    print(
        f"Split {len(results)} image(s) -> {len(new_images)} image(s) "
        f"on {num_workers} workers"
    )
    if sibling_count:
        print(f"Copied {sibling_count} sibling file(s)")
    print(f"Wrote manifest: {manifest_path} ({len(new_images)} image(s))")

    return {
        "split_count": len(results),
        "total_images": len(new_images),
        "manifest_path": manifest_path,
        "num_workers": num_workers,
    }
