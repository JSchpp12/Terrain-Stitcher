"""Split a single gathered terrain image in half and update its manifest.

Takes one ``gathered_r*_c*.png`` from a terrain output directory (produced by
``gather-ortho -src arcgis`` or ``process-terrain``), splits it in half along
its longer pixel dimension (no resizing — pure crop), and writes the two
halves plus an updated ``height_info.json`` to a new output directory. All
sibling files (other gathered PNGs, ``elevation_merged.tif``, ``Shape.json``,
sidecars) are copied to the output so it stays self-contained.

The image save (PNG encode) and sibling-file copy steps are parallelised with
a ``ThreadPoolExecutor``: PNG encoding releases the GIL, so the two halves
encode concurrently, and file copies are I/O-bound. The thread count defaults
to ``os.cpu_count()`` and is capped by the amount of work available.

The bounds for each half are computed in Web Mercator (EPSG:3857) because
image pixels are linearly spaced in projected space, not in WGS84 lat/lon:

  * **Vertical split** (left/right, dividing longitude): longitude is linear
    with Web Mercator X, so the pixel midpoint is exactly ``(west + east) / 2``
    in degrees. No reprojection needed for the longitude split, but we still
    reproject the new corners so the latitudes stay exact (the latitude of the
    NW and NE corners is the same for an axis-aligned Web Mercator rectangle,
    so this is effectively a no-op for latitude — included for uniformity).

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

from terrain_stitcher.common import get_all_files_in_directory

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


_MANIFEST_NAME = "height_info.json"


def _load_manifest(source_dir: str) -> dict:
    """Load height_info.json from *source_dir*."""
    manifest_path = os.path.join(source_dir, _MANIFEST_NAME)
    if not os.path.isfile(manifest_path):
        raise FileNotFoundError(
            f"{_MANIFEST_NAME} not found in {source_dir!r}. The split-image "
            "command expects a terrain output directory containing the "
            "manifest alongside the image."
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


def _copy_file(src_path: str, dest_path: str) -> str:
    """Copy *src_path* to *dest_path* preserving metadata."""
    shutil.copy2(src_path, dest_path)
    return dest_path


def main(
    image_path: str,
    output_dir: str,
    axis: str = _AXIS_AUTO,
    workers: Optional[int] = None,
) -> dict:
    """Split a single gathered terrain image in half and write the results.

    The PNG encode/save of the two halves and the copy of all sibling files
    are dispatched to a ``ThreadPoolExecutor`` so they run concurrently. PNG
    encoding releases the GIL (the two halves encode simultaneously) and file
    copies are I/O-bound, so threads are the right concurrency model here —
    no process-pool IPC overhead.

    Parameters
    ----------
    image_path
        Path to the ``gathered_r*_c*.png`` image to split.
    output_dir
        Directory to write the two new images + updated ``height_info.json``
        and copies of all sibling files. Created if it does not exist.
    axis
        Split axis: ``"auto"`` (default; splits along the longer pixel
        dimension), ``"vertical"`` (left/right), or ``"horizontal"``
        (top/bottom).
    workers
        Number of worker threads for the save + copy I/O (default:
        ``os.cpu_count()``). Each task is one image save or one file copy;
        the pool is sized to ``min(workers, n_tasks)`` so idle threads are
        never created.

    Returns
    -------
    dict
        Summary with keys: ``original_name``, ``axis``, ``first``,
        ``second``, ``manifest_path``.
    """
    if axis not in _VALID_AXES:
        raise ValueError(f"axis must be one of {_VALID_AXES!r}, got {axis!r}")
    if not os.path.isfile(image_path):
        raise FileNotFoundError(f"Image not found: {image_path!r}")

    source_dir = os.path.dirname(os.path.abspath(image_path))
    image_basename = os.path.basename(image_path)
    # Stem without extension (e.g. "gathered_r0_c0" from "gathered_r0_c0.png")
    image_stem = os.path.splitext(image_basename)[0]

    # --- Load the image and determine the split axis -------------------------
    with pImage.open(image_path) as img:
        width, height = img.size
        resolved_axis = _resolve_axis(width, height, axis)
        first_img, second_img = _split_image(img, resolved_axis)
    # img is now closed; first_img/second_img are .load()-ed and independent

    # --- Load the manifest and find this image's entry -----------------------
    manifest = _load_manifest(source_dir)

    original_entry = None
    remaining_entries = []
    for entry in manifest.get("images", []):
        if entry["name"] == image_stem:
            original_entry = entry
        else:
            remaining_entries.append(entry)

    if original_entry is None:
        raise ValueError(
            f"Image {image_stem!r} not found in {_MANIFEST_NAME}. The manifest "
            "must list the image being split."
        )

    # --- Compute new bounds for each half ------------------------------------
    first_bounds, second_bounds = _split_bounds(original_entry["bounds"], resolved_axis)

    first_name = image_stem + "_a"
    second_name = image_stem + "_b"

    first_entry = {"name": first_name, "bounds": first_bounds}
    second_entry = {"name": second_name, "bounds": second_bounds}

    # Build the new manifest: original entry replaced by the two halves,
    # position preserved (other entries keep their order).
    new_images = []
    inserted = False
    for entry in manifest.get("images", []):
        if entry["name"] == image_stem:
            new_images.append(first_entry)
            new_images.append(second_entry)
            inserted = True
        else:
            new_images.append(entry)
    if not inserted:
        # Should not happen since we validated above, but guard anyway
        new_images.extend([first_entry, second_entry])

    new_manifest = {"images": new_images}
    if "elevation_files" in manifest:
        new_manifest["elevation_files"] = manifest["elevation_files"]

    # --- Write output (threaded save + copy) ---------------------------------
    os.makedirs(output_dir, exist_ok=True)

    # Collect all I/O tasks: 2 image saves + N sibling file copies.
    # Each is independent (disjoint output paths), so they can all run in
    # parallel threads. PNG encoding releases the GIL; file copies are
    # I/O-bound — ThreadPoolExecutor is the right model (no IPC overhead).
    first_out = os.path.join(output_dir, first_name + ".png")
    second_out = os.path.join(output_dir, second_name + ".png")

    copy_specs: list[tuple[str, str]] = []
    for src_path in get_all_files_in_directory(source_dir):
        basename = os.path.basename(src_path)
        if basename == image_basename:
            continue
        if basename == _MANIFEST_NAME:
            continue
        copy_specs.append((src_path, os.path.join(output_dir, basename)))

    # Total independent tasks: 2 saves + len(copy_specs) copies
    n_tasks = 2 + len(copy_specs)
    num_workers = max(
        1, min(workers if workers is not None else os.cpu_count() or 1, n_tasks)
    )

    with ThreadPoolExecutor(max_workers=num_workers) as pool:
        futures = {}

        # Submit image saves (PNG encode — releases GIL)
        futures[pool.submit(_save_image_atomic, first_img, first_out)] = first_out
        futures[pool.submit(_save_image_atomic, second_img, second_out)] = second_out

        # Submit sibling file copies (I/O-bound)
        for src, dest in copy_specs:
            futures[pool.submit(_copy_file, src, dest)] = dest

        # Wait for all to complete; raise on first failure
        for fut in as_completed(futures):
            fut.result()

    # Write the new manifest (after all files are on disk)
    manifest_path = _save_manifest(output_dir, new_manifest)

    axis_label = "left/right" if resolved_axis == _AXIS_VERTICAL else "top/bottom"
    print(f"Split {image_stem!r} ({width}x{height}) {axis_label}")
    print(f"  {first_name}: {first_img.size[0]}x{first_img.size[1]} px")
    print(f"  {second_name}: {second_img.size[0]}x{second_img.size[1]} px")
    print(f"Copied {len(copy_specs)} sibling file(s) on {num_workers} workers")
    print(f"Wrote manifest: {manifest_path} ({len(new_images)} image(s))")

    return {
        "original_name": image_stem,
        "axis": resolved_axis,
        "first": first_name,
        "second": second_name,
        "manifest_path": manifest_path,
    }
