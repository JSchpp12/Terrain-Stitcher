from __future__ import annotations

import importlib
import math
import shutil
import subprocess
import sys
from pathlib import Path

from terrain_stitcher.arcgis.services import (
    ImageryService,
    bbox_latlon_from_radius,
    load_services,
)
from terrain_stitcher.common.ParseArea import ParseArea

from .DownloaderBase import (
    ArcGISDownloaderBase,
    WGS84_TO_WEBMERC,
    build_chunk_grid,
    build_mosaic,
)


def pixel_size_for_zoom(zoom: int) -> float:
    # standard web mercator resolution at zoom z, at the equator
    earth_circumference_m = 2 * math.pi * 6378137.0
    return earth_circumference_m / (256 * 2**zoom)


def native_lod_for_service(service: ImageryService) -> int:
    """The integer Web Mercator LOD closest to the service's native cell size.

    A service's native resolution (m/px) rarely matches the exact Web Mercator
    resolution at any integer LOD: it is either declared statically as a round
    number (e.g. USGS NAIP Plus at 0.3 m/px) or derived from ArcGIS's tileInfo
    LOD table, which uses a slightly different earth-radius constant than
    :func:`pixel_size_for_zoom`. Comparing the raw m/px values is therefore too
    strict -- a service that is "~LOD 19" (0.3 m/px vs the exact 0.29858 m/px)
    would reject LOD 19 itself. Rounding the equivalent LOD to the nearest
    integer absorbs that constant/rounding mismatch so the maximum LOD the
    service actually provides is accepted.
    """
    return round(math.log2(pixel_size_for_zoom(0) / service.native_pixel_size_m))


def assert_lod_within_native(zoom: int, service: ImageryService) -> None:
    """Raise ``ValueError`` if ``zoom`` is finer than the service's native
    level of detail.

    The comparison is performed in LOD space (see
    :func:`native_lod_for_service`) rather than on raw floating-point m/px, so
    a service whose native cell size is fractionally coarser than the exact
    Web Mercator resolution at its nominal LOD (e.g. 0.3 m/px, which is ~LOD
    19 but slightly coarser than the exact 0.29858 m/px) still accepts that
    LOD -- it is the maximum the service provides. Only a requested zoom
    strictly above the native LOD is rejected; finer than that would only bake
    interpolated pixels into the cache via gdal2tiles upscaling. There is
    deliberately no override: request a lower LOD instead.
    """
    native_lod = native_lod_for_service(service)
    if zoom > native_lod:
        raise ValueError(
            f"LOD {zoom} is finer than {service.key} native resolution "
            f"({service.native_pixel_size_m:.4f} m/px, ~LOD {native_lod}). "
            f"Request a lower LOD; the service has no real detail below "
            f"its native cell size."
        )


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


class OrthoDownloader(ArcGISDownloaderBase):
    """Downloads orthoimagery over a shape AOI as PNG chunks and tiles them
    with gdal2tiles into a web-mercator tile cache."""

    kind = "imagery"
    img_format = "png"
    pixel_type = "U8"
    georeference = True
    tmp_dir_name = "aoi_chunks"
    default_outdir = "output_tiles"

    def run(
        self,
        shapefile_path: str,
        outdir: str,
        zoom: int,
        xyz: bool,
        resampling: str,
        processes: int,
        timeout: int,
        num_workers: int,
        chunk_px: int,
        skip_mosaic: bool = False,
    ) -> None:
        shape_area = ParseArea.fromJSONFile(shapefile_path)
        lat = shape_area.center.get_lat()
        lon = shape_area.center.get_lon()

        # Resolve the imagery service from the AOI when one is not supplied.
        # The service's WGS84 coverage bbox picks the right state layer and
        # validates that the whole AOI fits inside it (downloads are limited
        # to one state).
        aoi_bbox = bbox_latlon_from_radius(lat, lon, shape_area.view_distance)
        service = self.resolve_service(load_services, aoi_bbox)

        # Fail (no override) when the requested LOD is finer than the
        # service's native cell size -- see assert_lod_within_native.
        assert_lod_within_native(zoom, service)

        xmin, ymin, xmax, ymax = bbox_from_radius(
            lat,
            lon,
            shape_area.view_distance,
        )
        print(f"AOI bbox (EPSG:3857): {xmin:.1f}, {ymin:.1f}, {xmax:.1f}, {ymax:.1f}")

        tmp_dir = Path("aoi_chunks")
        tmp_dir.mkdir(exist_ok=True)

        pixel_size_m = pixel_size_for_zoom(zoom)

        chunks = build_chunk_grid(xmin, ymin, xmax, ymax, chunk_px, pixel_size_m)

        print(f"Downloading {len(chunks)} chunks with {num_workers} workers...")
        chunk_paths, failed = self.download_chunks(
            service, chunks, tmp_dir, timeout, num_workers
        )
        # The chunk-index dicts are no longer needed; drop them before the
        # memory-heavy gdal2tiles pass so they can be reclaimed.
        del chunks

        if not chunk_paths:
            print("No chunks downloaded successfully - aborting.")
            sys.exit(1)

        if not skip_mosaic:
            print("Building mosaic...")
            mosaic_path = build_mosaic(chunk_paths, tmp_dir)

            print(f"Tiling (zoom {zoom})...")
            run_gdal2tiles(
                mosaic_path, outdir, zoom, xyz, resampling, processes, "none"
            )

            if failed:
                print(
                    f"\nCompleted with {len(failed)} failed chunk(s). "
                    f"Tiles written to: {outdir} ({'XYZ' if xyz else 'TMS'} numbering), "
                    f"but gaps exist where chunks failed.\n"
                    f"Temporary chunk files kept in {tmp_dir} -- re-run the same "
                    f"command to retry only the {len(failed)} failed chunk(s)."
                )
            else:
                # all chunks succeeded -- safe to clean up temporary files
                shutil.rmtree(tmp_dir, ignore_errors=True)
                print(
                    f"Done. Tiles written to: {outdir} "
                    f"({'XYZ' if xyz else 'TMS'} numbering)"
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
    service: ImageryService | None = None,
    service_index: int | None = None,
    skip_mosaic: bool = False,
):
    OrthoDownloader(service=service, service_index=service_index).run(
        shapefile_path=shapefile_path,
        outdir=outdir,
        zoom=zoom,
        xyz=xyz,
        resampling=resampling,
        processes=processes,
        timeout=timeout,
        num_workers=num_workers,
        chunk_px=chunk_px,
        skip_mosaic=skip_mosaic,
    )


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
    service: ImageryService | None = None,
    service_index: int | None = None,
    skip_mosaic: bool = False,
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
        service=service,
        service_index=service_index,
        skip_mosaic=skip_mosaic,
    )
