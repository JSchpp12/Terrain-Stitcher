from __future__ import annotations

import shutil
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
    _translate_to_geotiff,
)


class ElevationDownloader(ArcGISDownloaderBase):
    """Downloads a continuous Float32 elevation GeoTIFF over a shape AOI.

    Selects an ``"elevation"`` ArcGIS service (e.g. USGS 3DEPElevation),
    requests ``format=tiff`` + ``pixelType=F32`` chunks at ``res`` m/px,
    verifies + mosaics them, and writes the single merged GeoTIFF to *outdir*.
    Unlike orthoimagery there is no gdal2tiles pass -- elevation is a
    continuous raster, not a tile pyramid.
    """

    kind = "elevation"
    img_format = "tiff"
    pixel_type = "F32"
    georeference = False
    tmp_dir_name = "aoi_chunks_elevation"
    default_outdir = "elevation_merged.tif"

    def run(
        self,
        shapefile_path: str,
        outdir: str,
        res: float | None = None,
        chunk_px: int = 256,
        timeout: int = 30,
        num_workers: int = 32,
        padding: float = 0.0,
    ) -> None:
        shape_area = ParseArea.fromJSONFile(shapefile_path)
        lat = shape_area.center.get_lat()
        lon = shape_area.center.get_lon()

        # Resolve the elevation service from the AOI when one is not supplied.
        aoi_bbox = bbox_latlon_from_radius(lat, lon, shape_area.view_distance)
        service = self.resolve_service(load_services, aoi_bbox)

        # Default resolution to the service's registered native cell size.
        pixel_size_m = res if res is not None else service.native_pixel_size_m
        if pixel_size_m <= 0:
            raise ValueError(
                f"{service.key} has an invalid native pixel size; pass --res."
            )
        if res is not None and res < service.native_pixel_size_m:
            # 3DEP resamples on the fly; warn rather than fail so a user can
            # still request a slightly finer grid than the declared cell size.
            print(
                f"Warning: requested {res:.4f} m/px is finer than {service.key}'s "
                f"declared native resolution {service.native_pixel_size_m:.4f} "
                f"m/px; 3DEP will resample on the fly."
            )

        # Expand the AOI by an optional padding (degrees) before projecting.
        min_lat, min_lon, max_lat, max_lon = aoi_bbox
        if padding:
            min_lat -= padding
            min_lon -= padding
            max_lat += padding
            max_lon += padding
        xmin, ymin = WGS84_TO_WEBMERC.transform(min_lon, min_lat)
        xmax, ymax = WGS84_TO_WEBMERC.transform(max_lon, max_lat)
        print(f"AOI bbox (EPSG:3857): {xmin:.1f}, {ymin:.1f}, {xmax:.1f}, {ymax:.1f}")

        tmp_dir = Path(outdir).parent / self.tmp_dir_name
        tmp_dir.mkdir(exist_ok=True)

        chunks = build_chunk_grid(xmin, ymin, xmax, ymax, chunk_px, pixel_size_m)

        # Elevation chunks are already-georeferenced F32 TIFFs, so we skip the
        # georeference pass and write the bytes straight to disk.
        chunk_paths, failed = self.download_chunks(
            service, chunks, tmp_dir, timeout, num_workers
        )

        if not chunk_paths:
            print("No elevation chunks downloaded successfully - aborting.")
            sys.exit(1)

        print("Building mosaic...")
        mosaic_path = build_mosaic(chunk_paths, tmp_dir)

        print(f"Writing merged GeoTIFF -> {outdir}")
        _translate_to_geotiff(mosaic_path, Path(outdir))

        if failed:
            print(
                f"\nCompleted with {len(failed)} failed chunk(s); "
                f"gaps exist where chunks failed.\n"
                f"Temporary chunk files kept in {tmp_dir} -- re-run the same "
                f"command to retry only the {len(failed)} failed chunk(s)."
            )
        else:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            print(f"Done. Merged elevation GeoTIFF written to: {outdir}")


def download_elevation(
    shapefile_path: str,
    outdir: str = "elevation_merged.tif",
    res: float | None = None,
    chunk_px: int = 256,
    timeout: int = 30,
    num_workers: int = 32,
    service: ImageryService | None = None,
    service_index: int | None = None,
    padding: float = 0.0,
):
    """Fetch a continuous Float32 elevation GeoTIFF over the shape AOI.

    Selects an ``"elevation"`` ArcGIS service (e.g. USGS 3DEPElevation),
    requests ``format=tiff`` + ``pixelType=F32`` chunks at ``res`` m/px,
    verifies + mosaics them, and writes the single merged GeoTIFF to
    *outdir*. ``res`` defaults to the selected service's registered native
    pixel size.
    """
    ElevationDownloader(service=service, service_index=service_index).run(
        shapefile_path=shapefile_path,
        outdir=outdir,
        res=res,
        chunk_px=chunk_px,
        timeout=timeout,
        num_workers=num_workers,
        padding=padding,
    )


def main_elevation(
    shape_file,
    outdir="elevation_merged.tif",
    res: float | None = None,
    timeout: int = 30,
    num_workers: int = 32,
    chunk_px: int = 256,
    service: ImageryService | None = None,
    service_index: int | None = None,
    padding: float = 0.0,
):
    download_elevation(
        shapefile_path=shape_file,
        outdir=outdir,
        res=res,
        chunk_px=chunk_px,
        timeout=timeout,
        num_workers=num_workers,
        service=service,
        service_index=service_index,
        padding=padding,
    )
