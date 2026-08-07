from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from pyproj import Transformer
from tqdm import tqdm

from terrain_stitcher.arcgis.services import (
    AmbiguousServiceError,
    ImageryService,
    load_services,
    select_service,
)

WGS84_TO_WEBMERC = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)

TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}

MANIFEST_FILENAME = "manifest.json"


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
    service: ImageryService,
    chunk: dict,
    img_format: str,
    max_retries: int,
    timeout: int,
    pixel_type: str = "U8",
) -> bytes:
    params = {
        "bbox": f"{chunk['xmin']},{chunk['ymin']},{chunk['xmax']},{chunk['ymax']}",
        "bboxSR": service.srs,
        "imageSR": service.srs,
        "size": f"{chunk['w']},{chunk['h']}",
        "format": img_format,
        "pixelType": pixel_type,
        "interpolation": "RSP_BilinearInterpolation",
        "f": "image",
    }

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = session.get(service.base_url, params=params, timeout=timeout)
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


def _georeference_inprocess(
    raw_path: Path, out_path: Path, chunk: dict, srs: int
) -> Path:
    """Georeference a raw image using the GDAL Python bindings.

    Equivalent to ``gdal_translate -a_srs EPSG:{srs} -a_ullr ...`` but runs
    in-process, avoiding the ~30-50 ms per-call subprocess-spawn overhead on
    Windows that dominates the cost for small chunks.
    """
    from osgeo import gdal

    options = gdal.TranslateOptions(
        [
            "-a_srs",
            f"EPSG:{srs}",
            "-a_ullr",
            str(chunk["xmin"]),
            str(chunk["ymax"]),
            str(chunk["xmax"]),
            str(chunk["ymin"]),
        ],
        format="GTiff",
    )
    ds = gdal.Translate(str(out_path), str(raw_path), options=options)
    if ds is None:
        raise RuntimeError(
            f"gdal.Translate failed for {raw_path} "
            f"(chunk {chunk['col']},{chunk['row']})"
        )
    ds = None  # close the output dataset
    return out_path


def _georeference_subprocess(
    raw_path: Path, out_path: Path, chunk: dict, srs: int
) -> Path:
    """Georeference a raw image by shelling out to ``gdal_translate``.

    Fallback used when the GDAL Python bindings are not importable.
    """
    subprocess.run(
        [
            "gdal_translate",
            "-a_srs",
            f"EPSG:{srs}",
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
    return out_path


def georeference_chunk(
    raw_bytes: bytes, chunk: dict, img_format: str, tmp_dir: Path, srs: int
) -> Path:
    ext = "tif" if img_format == "tiff" else img_format
    raw_path = tmp_dir / f"raw_{chunk['col']}_{chunk['row']}.{ext}"
    raw_path.write_bytes(raw_bytes)

    out_path = tmp_dir / f"chunk_{chunk['col']}_{chunk['row']}.tif"
    try:
        _georeference_inprocess(raw_path, out_path, chunk, srs)
    except ImportError:
        _georeference_subprocess(raw_path, out_path, chunk, srs)
    finally:
        raw_path.unlink(missing_ok=True)
    return out_path


def write_chunk_direct(
    raw_bytes: bytes, chunk: dict, img_format: str, tmp_dir: Path
) -> Path:
    """Persist an already-georeferenced exportImage response directly.

    Used by the elevation path: ``format=tiff`` + ``pixelType=F32`` chunks
    come back as fully georeferenced GeoTIFFs (they carry their own affine
    transform + SRS), so unlike raw PNG ortho chunks they need no
    ``-a_ullr`` / ``-a_srs`` pass. Writes ``chunk_{col}_{row}.tif`` to match
    the filename convention the manifest and on-disk recovery expect.
    """
    ext = "tif" if img_format == "tiff" else img_format
    out_path = tmp_dir / f"chunk_{chunk['col']}_{chunk['row']}.{ext}"
    out_path.write_bytes(raw_bytes)
    return out_path


def _chunk_key(chunk: dict) -> str:
    return f"{chunk['row']}_{chunk['col']}"


def _load_manifest(tmp_dir: Path) -> dict:
    """Load an existing chunk manifest from *tmp_dir*, or return an empty
    manifest if none exists.  The manifest maps ``"row_col"`` keys to
    ``{"status": "downloaded"|"failed", "file": str|None}``."""
    manifest_path = tmp_dir / MANIFEST_FILENAME
    if manifest_path.is_file():
        try:
            with open(manifest_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            print("Warning: manifest.json was corrupt -- ignoring it.")
    return {}


def _save_manifest(tmp_dir: Path, manifest: dict) -> None:
    manifest_path = tmp_dir / MANIFEST_FILENAME
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)


def _recover_manifest_from_disk(tmp_dir: Path) -> dict:
    """Build a manifest from chunk GeoTIFFs left on disk by an interrupted
    run (one where ``manifest.json`` was never written).

    Scans *tmp_dir* for files named ``chunk_{col}_{row}.tif``, parses the
    col/row from each filename, and returns a manifest dict with those chunks
    marked as ``"downloaded"``.
    """
    manifest: dict[str, dict] = {}
    for chunk_file in tmp_dir.glob("chunk_*.tif"):
        # filename is chunk_{col}_{row}.tif
        stem = chunk_file.stem  # "chunk_0_3"
        parts = stem.split("_")
        if len(parts) != 3 or parts[0] != "chunk":
            continue
        try:
            col = int(parts[1])
            row = int(parts[2])
        except ValueError:
            continue
        key = f"{row}_{col}"
        manifest[key] = {"status": "downloaded", "file": chunk_file.name}
    return manifest


def _verify_chunk(path: Path) -> bool:
    """Return True if *path* is a readable raster with valid pixel data.

    Tries, in order, the GDAL Python bindings (most thorough -- reads all
    pixel data via ``Checksum``), the ``gdalinfo`` command-line tool, and
    finally a basic TIFF header + minimum size sanity check.
    """
    # 1. GDAL Python bindings -- forces a full read of every band
    try:
        from osgeo import gdal

        ds = gdal.OpenEx(str(path), gdal.OF_RASTER | gdal.OF_READONLY)
        if ds is None:
            return False
        if ds.RasterXSize <= 0 or ds.RasterYSize <= 0 or ds.RasterCount < 1:
            ds = None
            return False
        try:
            for i in range(1, ds.RasterCount + 1):
                ds.GetRasterBand(i).Checksum()
        except Exception:
            ds = None
            return False
        ds = None
        return True
    except ImportError:
        pass

    # 2. gdalinfo command-line tool
    gdalinfo = shutil.which("gdalinfo")
    if gdalinfo:
        result = subprocess.run(
            [gdalinfo, "-checksum", str(path)],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    # 3. Basic TIFF header + minimum size sanity check
    try:
        import struct

        with open(path, "rb") as fh:
            header = fh.read(4)
        if header[:2] not in (b"II", b"MM"):
            return False
        endian = "<" if header[:2] == b"II" else ">"
        magic = struct.unpack(endian + "H", header[2:4])[0]
        if magic != 42:
            return False
        return path.stat().st_size > 1024
    except (OSError, struct.error):
        return False


def download_all_chunks(
    chunks,
    service: ImageryService,
    img_format,
    max_retries,
    timeout,
    workers,
    tmp_dir: Path,
    pixel_type: str = "U8",
    georeference: bool = True,
) -> tuple[list[Path], list[dict]]:
    """Download *chunks* into *tmp_dir*, skipping any that already appear as
    downloaded in a previous run's manifest (and whose GeoTIFF still exists).

    ``pixel_type`` selects the exportImage band depth (``U8`` for ortho PNGs,
    ``F32`` for elevation). When ``georeference`` is True (ortho) raw bytes
    are passed through :func:`georeference_chunk`; when False (elevation) the
    already-georeferenced TIFF bytes are written directly via
    :func:`write_chunk_direct`.

    Returns ``(chunk_paths, failed)`` where *chunk_paths* are the GeoTIFFs of
    every successfully fetched chunk (reused + freshly downloaded) and
    *failed* is the list of chunks that could not be fetched.
    """
    manifest = _load_manifest(tmp_dir)

    if not manifest:
        recovered = _recover_manifest_from_disk(tmp_dir)
        if recovered:
            print(
                f"No manifest found but {len(recovered)} chunk file(s) exist "
                f"on disk from a previous interrupted run -- recovering."
            )
            manifest = recovered

    chunk_paths: list[Path] = []
    failed: list[dict] = []
    to_download: list[dict] = []

    # Collect chunks the manifest says are already downloaded so we can
    # verify them before reusing -- a truncated/corrupt file (e.g. from an
    # interrupted write) must be re-fetched, not silently used.
    cached: list[tuple[dict, str, Path]] = []
    for chunk in chunks:
        key = _chunk_key(chunk)
        entry = manifest.get(key)
        if entry and entry.get("status") == "downloaded":
            cached_path = tmp_dir / entry["file"]
            if cached_path.is_file():
                cached.append((chunk, key, cached_path))
                continue
        to_download.append(chunk)

    if cached:
        corrupt: list[tuple[dict, str, Path]] = []
        with ThreadPoolExecutor(max_workers=workers) as verify_pool:
            futures = {
                verify_pool.submit(_verify_chunk, cached_path): (
                    chunk,
                    key,
                    cached_path,
                )
                for chunk, key, cached_path in cached
            }
            with tqdm(total=len(cached), desc="Verifying cached chunks") as pbar:
                for future in as_completed(futures):
                    chunk, key, cached_path = futures[future]
                    try:
                        ok = future.result()
                    except Exception:
                        ok = False
                    if ok:
                        chunk_paths.append(cached_path)
                    else:
                        pbar.write(
                            f"Chunk ({chunk['col']},{chunk['row']}) failed "
                            f"verification -- will re-download."
                        )
                        cached_path.unlink(missing_ok=True)
                        manifest[key] = {"status": "failed", "file": None}
                        corrupt.append((chunk, key, cached_path))
                    pbar.update(1)
        if corrupt:
            to_download.extend(c[0] for c in corrupt)
            print(
                f"{len(cached) - len(corrupt)} chunk(s) verified, "
                f"{len(corrupt)} corrupt and will be re-downloaded."
            )
        else:
            print(f"{len(cached)} chunk(s) verified OK.")

    if to_download:
        print(
            f"{len(chunk_paths)} chunk(s) already downloaded; "
            f"fetching {len(to_download)} remaining chunk(s)..."
        )
    elif chunk_paths:
        print("All chunks already downloaded -- skipping fetch.")
    else:
        print(f"Downloading {len(chunks)} chunks with {workers} workers...")

    with requests.Session() as session, ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                fetch_chunk,
                session,
                service,
                c,
                img_format,
                max_retries,
                timeout,
                pixel_type,
            ): c
            for c in to_download
        }

        with tqdm(total=len(to_download), desc="Downloading chunks") as pbar:
            for future in as_completed(futures):
                chunk = futures[future]
                key = _chunk_key(chunk)
                try:
                    raw_bytes = future.result()
                    if georeference:
                        path = georeference_chunk(
                            raw_bytes, chunk, img_format, tmp_dir, service.srs
                        )
                    else:
                        path = write_chunk_direct(raw_bytes, chunk, img_format, tmp_dir)
                    chunk_paths.append(path)
                    manifest[key] = {"status": "downloaded", "file": path.name}
                    pbar.set_postfix_str(f"Chunk ({chunk['col']},{chunk['row']}) OK")
                except Exception as e:
                    failed.append(chunk)
                    manifest[key] = {"status": "failed", "file": None}
                    pbar.set_postfix_str(
                        f"Chunk ({chunk['col']},{chunk['row']}) FAILED: {e}"
                    )
                finally:
                    pbar.update(1)

    _save_manifest(tmp_dir, manifest)

    if failed:
        print(
            f"\n{len(failed)} chunk(s) failed after all retries. "
            f"Re-run the command to retry only the failed chunks."
        )
    return chunk_paths, failed


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


def _translate_to_geotiff(vrt_path: Path, out_path: Path) -> Path:
    """Burn a VRT into a single standalone GeoTIFF at *out_path*.

    Tries the GDAL Python bindings first (in-process), falling back to the
    ``gdal_translate`` command-line tool when they are not importable.
    """
    try:
        from osgeo import gdal

        ds = gdal.Translate(str(out_path), str(vrt_path), format="GTiff")
        if ds is None:
            raise RuntimeError(f"gdal.Translate failed for {vrt_path}")
        ds = None
        return Path(out_path)
    except ImportError:
        subprocess.run(
            ["gdal_translate", str(vrt_path), str(out_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        return Path(out_path)


class ArcGISDownloaderBase:
    """Shared plumbing for ArcGIS raster downloaders.

    Subclasses (``OrthoDownloader``, ``ElevationDownloader``) set the
    capability ``kind`` and the fetch parameters (image ``format``,
    ``pixel_type``, and whether the raw exportImage bytes are already
    georeferenced), then implement the rest of their pipeline (tiling vs.
    merged-GeoTIFF). This class provides common service resolution and the
    shared chunk-download/mosaic helpers.
    """

    kind = "imagery"
    img_format = "png"
    pixel_type = "U8"
    georeference = True
    tmp_dir_name = "aoi_chunks"
    default_max_retries = 5

    def __init__(
        self,
        service: ImageryService | None = None,
        service_index: int | None = None,
    ):
        self.service = service
        self.service_index = service_index

    def resolve_service(self, loader, aoi_bbox) -> ImageryService:
        """Pick a registered service of this downloader's ``kind`` for the AOI.

        ``loader`` is a zero-arg callable returning the service registry
        (usually :func:`terrain_stitcher.arcgis.services.load_services`).
        When an explicit ``service`` was supplied to the constructor it is
        returned unchanged. When several candidates cover the AOI and no
        ``service_index`` disambiguates, the candidate list is printed and the
        process exits (2).
        """
        if self.service is not None:
            return self.service
        try:
            service = select_service(
                loader(), aoi_bbox, index=self.service_index, kind=self.kind
            )
        except AmbiguousServiceError as e:
            print(
                f"Multiple {self.kind} services cover this area. Re-run with "
                "`--service-index <N>` to choose one:"
            )
            for i, s in enumerate(e.candidates):
                print(
                    f"  {i}: {s.label} ({s.key})  "
                    f"native {s.native_pixel_size_m:.4f} m/px"
                )
            sys.exit(2)
        print(f"Selected {self.kind} service: {service.label} ({service.key})")
        return service

    def download_chunks(
        self,
        service: ImageryService,
        chunks,
        tmp_dir: Path,
        timeout: int,
        num_workers: int,
        max_retries: int | None = None,
    ) -> tuple[list[Path], list[dict]]:
        """Download *chunks* for *service* into *tmp_dir* using this
        downloader's ``img_format`` / ``pixel_type`` / ``georeference``."""
        max_retries = self.default_max_retries if max_retries is None else max_retries
        return download_all_chunks(
            chunks,
            service,
            self.img_format,
            max_retries,
            timeout,
            num_workers,
            tmp_dir,
            pixel_type=self.pixel_type,
            georeference=self.georeference,
        )
