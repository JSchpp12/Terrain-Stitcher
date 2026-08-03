import importlib
import math
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tqdm import tqdm

import requests
from pyproj import Transformer

from terrain_stitcher.common.ParseArea import ParseArea

# --- Service-specific settings ---------------------------------------------
# from alaska_vivid_2023_30cm ImageServer tileInfo

BASE_URL = (
    "https://apps.geo.fpac.usda.gov/nrcs-imagery/rest/services/"
    "ortho_imagery/alaska_vivid_2023_30cm/ImageServer/exportImage"
)
NATIVE_PIXEL_SIZE_M = 0.2985821417389697  # ~30cm, from the service's tileInfo
SRS = 3857
# ----------------------------------------------------------------------------

WGS84_TO_WEBMERC = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)

TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}


def pixel_size_for_zoom(zoom: int) -> float:
    # standard web mercator resolution at zoom z, at the equator
    earth_circumference_m = 2 * math.pi * 6378137.0
    return earth_circumference_m / (256 * 2**zoom)


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


def build_chunk_grid(xmin, ymin, xmax, ymax, chunk_px: int, pixel_size_m: float):
    """Return a list of chunk dicts covering the AOI, each with pixel
    dimensions and a projected bbox, sized at `pixel_size_m` resolution."""
    total_w_px = max(1, round((xmax - xmin) / pixel_size_m))
    total_h_px = max(1, round((ymax - ymin) / pixel_size_m))

    n_cols = math.ceil(total_w_px / chunk_px)
    n_rows = math.ceil(total_h_px / chunk_px)

    chunks = []
    for row in range(n_rows):
        y_off = row * chunk_px
        h = min(chunk_px, total_h_px - y_off)
        chunk_ymax = ymax - y_off * pixel_size_m
        chunk_ymin = chunk_ymax - h * pixel_size_m

        for col in range(n_cols):
            x_off = col * chunk_px
            w = min(chunk_px, total_w_px - x_off)
            chunk_xmin = xmin + x_off * pixel_size_m
            chunk_xmax = chunk_xmin + w * pixel_size_m

            chunks.append(
                {
                    "row": row,
                    "col": col,
                    "w": w,
                    "h": h,
                    "xmin": chunk_xmin,
                    "ymin": chunk_ymin,
                    "xmax": chunk_xmax,
                    "ymax": chunk_ymax,
                }
            )

    print(
        f"Total raster: {total_w_px} x {total_h_px} px @ {pixel_size_m:.3f} m/px "
        f"-> {len(chunks)} chunks ({n_cols} cols x {n_rows} rows, {chunk_px}px each)"
    )
    return chunks


def fetch_chunk(
    session: requests.Session,
    chunk: dict,
    img_format: str,
    max_retries: int,
    timeout: int,
) -> bytes:
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
            time.sleep(min(2**attempt, 30))
            continue

        if resp.status_code == 200 and resp.headers.get("Content-Type", "").startswith(
            "image"
        ):
            return resp.content

        if resp.status_code in TRANSIENT_STATUS_CODES:
            last_error = RuntimeError(f"HTTP {resp.status_code} (transient)")
            time.sleep(min(2**attempt, 30))
            continue

        # Non-transient failure (bad request, auth, etc.) - don't bother retrying
        raise RuntimeError(
            f"exportImage failed for chunk ({chunk['col']},{chunk['row']}): "
            f"HTTP {resp.status_code}: {resp.text[:300]}"
        )

    raise RuntimeError(
        f"Chunk ({chunk['col']},{chunk['row']}) failed after {max_retries} retries: {last_error}"
    )


def georeference_chunk(
    raw_bytes: bytes, chunk: dict, img_format: str, tmp_dir: Path
) -> Path:
    ext = "tif" if img_format == "tiff" else img_format
    raw_path = tmp_dir / f"raw_{chunk['col']}_{chunk['row']}.{ext}"
    raw_path.write_bytes(raw_bytes)

    out_path = tmp_dir / f"chunk_{chunk['col']}_{chunk['row']}.tif"
    subprocess.run(
        [
            "gdal_translate",
            "-a_srs",
            f"EPSG:{SRS}",
            "-a_ullr",
            str(chunk["xmin"]),
            str(chunk["ymax"]),
            str(chunk["xmax"]),
            str(chunk["ymin"]),
            str(raw_path),
            str(out_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    raw_path.unlink(missing_ok=True)
    return out_path


def download_all_chunks(
    chunks, img_format, max_retries, timeout, workers, tmp_dir: Path
):
    chunk_paths = []
    failed = []

    with requests.Session() as session, ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(fetch_chunk, session, c, img_format, max_retries, timeout): c
            for c in chunks
        }

        with tqdm(total=len(chunks), desc="Downloading chunks") as pbar:
            for future in as_completed(futures):
                chunk = futures[future]
                try:
                    raw_bytes = future.result()
                    path = georeference_chunk(raw_bytes, chunk, img_format, tmp_dir)
                    chunk_paths.append(path)
                    pbar.set_postfix_str(f"Chunk ({chunk['col']},{chunk['row']}) OK")
                except Exception as e:
                    failed.append(chunk)
                    pbar.set_postfix_str(
                        f"Chunk ({chunk['col']},{chunk['row']}) FAILED: {e}"
                    )
                finally:
                    pbar.update(1)
    if failed:
        print(
            f"\n{len(failed)} chunk(s) failed after all retries. "
            f"Consider re-running with more retries or fewer workers."
        )
    return chunk_paths


def build_mosaic(chunk_paths, tmp_dir: Path) -> Path:
    vrt_path = tmp_dir / "mosaic.vrt"
    file_list = tmp_dir / "chunk_list.txt"
    file_list.write_text("\n".join(str(p) for p in chunk_paths))
    subprocess.run(
        ["gdalbuildvrt", "-input_file_list", str(file_list), str(vrt_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return vrt_path


def run_gdal2tiles(
    raster_path: Path,
    outdir: str,
    lod: str,
    xyz: bool,
    resampling: str,
    processes: int,
    webviewer: str,
):
    base_args = [
        "-z",
        str(lod),
        "-w",
        webviewer,
        "-r",
        resampling,
        "--processes",
        str(processes),
    ]
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


def download_from_arcgis(
    shapefile_path: str,
    outdir,
    zoom,
    xyz,
    resampling,
    processes,
    timeout,
    num_workers,
    chunk_px,
):
    shape_area = ParseArea.fromJSONFile(shapefile_path)

    xmin, ymin, xmax, ymax = bbox_from_radius(
        shape_area.center.get_lat(),
        shape_area.center.get_lon(),
        shape_area.view_distance,
    )
    print(f"AOI bbox (EPSG:3857): {xmin:.1f}, {ymin:.1f}, {xmax:.1f}, {ymax:.1f}")

    tmp_dir = Path("aoi_chunks")
    tmp_dir.mkdir(exist_ok=True)

    pixel_size_m = max(NATIVE_PIXEL_SIZE_M, pixel_size_for_zoom(zoom))

    chunks = build_chunk_grid(xmin, ymin, xmax, ymax, chunk_px, pixel_size_m)

    print(f"Downloading {len(chunks)} chunks with {num_workers} workers...")
    chunk_paths = download_all_chunks(chunks, "png", 5, timeout, num_workers, tmp_dir)

    if not chunk_paths:
        print("No chunks downloaded successfully - aborting.")
        sys.exit(1)

    print("Building mosaic...")
    mosaic_path = build_mosaic(chunk_paths, tmp_dir)

    print(f"Tiling (zoom {zoom})...")
    run_gdal2tiles(mosaic_path, outdir, zoom, xyz, resampling, processes, "none")

    # delete temporary files
    shutil.rmtree(tmp_dir, ignore_errors=True)

    print(f"Done. Tiles written to: {outdir} ({'XYZ' if xyz else 'TMS'} numbering)")


def main(
    shape_file,
    lod: int,
    outdir="output_tiles",
    xyz: bool = True,
    resampling: str = "lanczos",
    processes: int = 32,
    timeout: int = 30,
    num_workers: int = 32,
    chunk_px: int = 256,
):
    download_from_arcgis(
        shapefile_path=shape_file,
        outdir=outdir,
        zoom=lod,
        xyz=xyz,
        resampling=resampling,
        processes=processes,
        timeout=timeout,
        num_workers=num_workers,
        chunk_px=chunk_px,
    )
