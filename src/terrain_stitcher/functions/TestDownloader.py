#!/usr/bin/env python3
"""
Export imagery from an ArcGIS ImageServer within a mile radius of a point,
downloading in parallel chunks (with retries on transient server errors),
mosaicking them, and cutting the result into z/x/y tiles.

Requires:
    pip install pyproj requests
    GDAL command-line tools (gdal_translate, gdalbuildvrt) on PATH,
    plus the gdal2tiles module (ships with the GDAL Python bindings).

    conda install -c conda-forge gdal   # most reliable way to get all of the above

Usage:
    python export_imageserver_tiles.py --lat 55.9096 --lon -159.1595 --radius-miles 1 --zoom 10-17
"""

import argparse
import importlib
import math
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from pyproj import Transformer

# --- Service-specific settings ---------------------------------------------
BASE_URL = (
    "https://apps.geo.fpac.usda.gov/nrcs-imagery/rest/services/"
    "ortho_imagery/alaska_vivid_2023_30cm/ImageServer/exportImage"
)
NATIVE_PIXEL_SIZE_M = 0.2985821417389697  # ~30cm, from the service's tileInfo
SRS = 3857
# ----------------------------------------------------------------------------

WGS84_TO_WEBMERC = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)

TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}


def bbox_from_radius(lat: float, lon: float, radius_miles: float):
    """Bounding box (xmin, ymin, xmax, ymax) in EPSG:3857 meters corresponding
    to a real-world radius in miles around lat/lon."""
    radius_m = radius_miles * 1609.344
    meters_per_deg_lat = 111_320.0
    meters_per_deg_lon = 111_320.0 * math.cos(math.radians(lat))

    dlat = radius_m / meters_per_deg_lat
    dlon = radius_m / meters_per_deg_lon

    lat_min, lat_max = lat - dlat, lat + dlat
    lon_min, lon_max = lon - dlon, lon + dlon

    xmin, ymin = WGS84_TO_WEBMERC.transform(lon_min, lat_min)
    xmax, ymax = WGS84_TO_WEBMERC.transform(lon_max, lat_max)
    return xmin, ymin, xmax, ymax


def build_chunk_grid(xmin, ymin, xmax, ymax, chunk_px: int):
    """Return a list of chunk dicts covering the AOI, each with pixel
    dimensions and a projected bbox, sized at the service's native resolution."""
    total_w_px = max(1, round((xmax - xmin) / NATIVE_PIXEL_SIZE_M))
    total_h_px = max(1, round((ymax - ymin) / NATIVE_PIXEL_SIZE_M))

    n_cols = math.ceil(total_w_px / chunk_px)
    n_rows = math.ceil(total_h_px / chunk_px)

    chunks = []
    for row in range(n_rows):
        y_off = row * chunk_px
        h = min(chunk_px, total_h_px - y_off)
        chunk_ymax = ymax - y_off * NATIVE_PIXEL_SIZE_M
        chunk_ymin = chunk_ymax - h * NATIVE_PIXEL_SIZE_M

        for col in range(n_cols):
            x_off = col * chunk_px
            w = min(chunk_px, total_w_px - x_off)
            chunk_xmin = xmin + x_off * NATIVE_PIXEL_SIZE_M
            chunk_xmax = chunk_xmin + w * NATIVE_PIXEL_SIZE_M

            chunks.append({
                "row": row, "col": col,
                "w": w, "h": h,
                "xmin": chunk_xmin, "ymin": chunk_ymin,
                "xmax": chunk_xmax, "ymax": chunk_ymax,
            })

    print(f"Total raster: {total_w_px} x {total_h_px} px -> {len(chunks)} chunks "
          f"({n_cols} cols x {n_rows} rows, {chunk_px}px each)")
    return chunks


def fetch_chunk(session: requests.Session, chunk: dict, img_format: str,
                 max_retries: int, timeout: int) -> bytes:
    params = {
        "bbox": f"{chunk['xmin']},{chunk['ymin']},{chunk['xmax']},{chunk['ymax']}",
        "bboxSR": SRS,
        "imageSR": SRS,
        "size": f"{chunk['w']},{chunk['h']}",
        "format": img_format,
        "pixelType": "U8",
        "interpolation": "RSP_BilinearInterpolation",
        "f": "image",
    }

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = session.get(BASE_URL, params=params, timeout=timeout)
        except requests.RequestException as e:
            last_error = e
            time.sleep(min(2 ** attempt, 30))
            continue

        if resp.status_code == 200 and resp.headers.get("Content-Type", "").startswith("image"):
            return resp.content

        if resp.status_code in TRANSIENT_STATUS_CODES:
            last_error = RuntimeError(f"HTTP {resp.status_code} (transient)")
            time.sleep(min(2 ** attempt, 30))
            continue

        # Non-transient failure (bad request, auth, etc.) - don't bother retrying
        raise RuntimeError(
            f"exportImage failed for chunk ({chunk['col']},{chunk['row']}): "
            f"HTTP {resp.status_code}: {resp.text[:300]}"
        )

    raise RuntimeError(
        f"Chunk ({chunk['col']},{chunk['row']}) failed after {max_retries} retries: {last_error}"
    )


def georeference_chunk(raw_bytes: bytes, chunk: dict, img_format: str, tmp_dir: Path) -> Path:
    ext = "tif" if img_format == "tiff" else img_format
    raw_path = tmp_dir / f"raw_{chunk['col']}_{chunk['row']}.{ext}"
    raw_path.write_bytes(raw_bytes)

    out_path = tmp_dir / f"chunk_{chunk['col']}_{chunk['row']}.tif"
    subprocess.run(
        [
            "gdal_translate",
            "-a_srs", f"EPSG:{SRS}",
            "-a_ullr", str(chunk["xmin"]), str(chunk["ymax"]), str(chunk["xmax"]), str(chunk["ymin"]),
            str(raw_path), str(out_path),
        ],
        check=True, capture_output=True, text=True,
    )
    raw_path.unlink(missing_ok=True)
    return out_path


def download_all_chunks(chunks, img_format, max_retries, timeout, workers, tmp_dir: Path):
    chunk_paths = []
    failed = []

    with requests.Session() as session, ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(fetch_chunk, session, c, img_format, max_retries, timeout): c
            for c in chunks
        }
        done = 0
        for future in as_completed(futures):
            chunk = futures[future]
            done += 1
            try:
                raw_bytes = future.result()
                path = georeference_chunk(raw_bytes, chunk, img_format, tmp_dir)
                chunk_paths.append(path)
                print(f"  [{done}/{len(chunks)}] chunk ({chunk['col']},{chunk['row']}) OK")
            except Exception as e:
                failed.append(chunk)
                print(f"  [{done}/{len(chunks)}] chunk ({chunk['col']},{chunk['row']}) FAILED: {e}")

    if failed:
        print(f"\n{len(failed)} chunk(s) failed after all retries. "
              f"Consider re-running with more retries or fewer workers.")
    return chunk_paths


def build_mosaic(chunk_paths, tmp_dir: Path) -> Path:
    vrt_path = tmp_dir / "mosaic.vrt"
    file_list = tmp_dir / "chunk_list.txt"
    file_list.write_text("\n".join(str(p) for p in chunk_paths))
    subprocess.run(
        ["gdalbuildvrt", "-input_file_list", str(file_list), str(vrt_path)],
        check=True, capture_output=True, text=True,
    )
    return vrt_path


def run_gdal2tiles(raster_path: Path, outdir: str, zoom: str, xyz: bool,
                    resampling: str, processes: int, webviewer: str):
    base_args = ["-z", zoom, "-w", webviewer, "-r", resampling, "--processes", str(processes)]
    if xyz:
        base_args.append("--xyz")
    base_args += [str(raster_path), outdir]

    exe = shutil.which("gdal2tiles.py") or shutil.which("gdal2tiles")
    if exe:
        subprocess.run([exe, *base_args], check=True)
        return

    for module_name in ("osgeo_utils.gdal2tiles", "gdal2tiles"):
        try:
            importlib.import_module(module_name)
        except ImportError:
            continue
        subprocess.run([sys.executable, "-m", module_name, *base_args], check=True)
        return

    raise RuntimeError(
        "Could not find gdal2tiles. Install with:\n"
        "  conda install -c conda-forge gdal\n"
        "  (or) pip install gdal2tiles-leaflet"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lon", type=float, required=True)
    parser.add_argument("--radius-miles", type=float, required=True)
    parser.add_argument("--zoom", default="10-17")
    parser.add_argument("--outdir", default="tiles_output")

    parser.add_argument("--chunk-px", type=int, default=2048,
                         help="chunk size in pixels per exportImage request (max ~4000 given service limits)")
    parser.add_argument("--workers", type=int, default=6,
                         help="concurrent download threads (keep modest - this is a public server)")
    parser.add_argument("--retries", type=int, default=5, help="retries per chunk on transient errors")
    parser.add_argument("--timeout", type=int, default=60, help="per-request timeout in seconds")
    parser.add_argument("--img-format", default="tiff", choices=["tiff", "png", "jpgpng"])

    parser.add_argument("--xyz", action="store_true")
    parser.add_argument("--tms", dest="xyz", action="store_false")
    parser.set_defaults(xyz=True)
    parser.add_argument("--resampling", default="average",
                         choices=["average", "near", "bilinear", "cubic", "cubicspline", "lanczos"])
    parser.add_argument("--processes", type=int, default=4)
    parser.add_argument("--webviewer", default="none", choices=["none", "leaflet", "openlayers", "all"])
    parser.add_argument("--keep-temp", action="store_true")
    args = parser.parse_args()

    xmin, ymin, xmax, ymax = bbox_from_radius(args.lat, args.lon, args.radius_miles)
    print(f"AOI bbox (EPSG:3857): {xmin:.1f}, {ymin:.1f}, {xmax:.1f}, {ymax:.1f}")

    tmp_dir = Path("aoi_chunks")
    tmp_dir.mkdir(exist_ok=True)

    chunks = build_chunk_grid(xmin, ymin, xmax, ymax, args.chunk_px)

    print(f"Downloading {len(chunks)} chunks with {args.workers} workers...")
    chunk_paths = download_all_chunks(
        chunks, args.img_format, args.retries, args.timeout, args.workers, tmp_dir
    )

    if not chunk_paths:
        print("No chunks downloaded successfully - aborting.")
        sys.exit(1)

    print("Building mosaic...")
    mosaic_path = build_mosaic(chunk_paths, tmp_dir)

    print(f"Tiling (zoom {args.zoom})...")
    run_gdal2tiles(
        mosaic_path, args.outdir, args.zoom, args.xyz,
        args.resampling, args.processes, args.webviewer,
    )

    if not args.keep_temp:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    print(f"Done. Tiles written to: {args.outdir} ({'XYZ' if args.xyz else 'TMS'} numbering)")


if __name__ == "__main__":
    main()