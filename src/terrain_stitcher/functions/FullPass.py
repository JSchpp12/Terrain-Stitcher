"""Single-command full terrain pass (`process-terrain`).

Orchestrates the existing download + gather entrypoints into one command that
produces one requested LOD tier for the AOI:

  <output>/<name>_<lod>

The required ``lod`` is used for both the ArcGIS download and the gather. If
the initial gather reports that no tiles survived at that LOD, the command
performs one dedicated download at the same LOD and retries the gather.

Chunking is mandatory: ``dimension`` must be >= 2. With dimension 1 every
cache tile becomes its own output file (thousands of files for a real AOI),
which this command exists to prevent.
"""

from __future__ import annotations

import os
import shutil
from typing import Optional

from .OrthoDownloader import main as main_arcgis_downloader
from .ArcGisImporter import (
    import_from_download as main_ortho_arcgis_import_from_download,
)
from .ElevationDownloader import main_elevation
from .ElevationGeoPrep import DEFAULT_PADDING_DEG

_MIN_DIMENSION = 2


def _download_kwargs(
    shape_file: str,
    outdir: str,
    lod: int,
    workers: int,
    chunk_px: int,
    timeout: int,
    resampling: str,
    processes: int,
    service_index: Optional[int],
) -> dict:
    return dict(
        shape_file=shape_file,
        lod=lod,
        outdir=outdir,
        num_workers=workers,
        chunk_px=chunk_px,
        timeout=timeout,
        processes=processes,
        resampling=resampling,
        service_index=service_index,
    )


def _gather_kwargs(
    shape_file: str,
    download_dir: str,
    lod: int,
    output_dir: str,
    dimension: int,
    scale_factor: float,
    gather_workers: Optional[int],
    elevation_data_dir: Optional[str],
    elevation_padding_deg: float,
) -> dict:
    return dict(
        shape_file=shape_file,
        download_dir=download_dir,
        min_level=lod,
        max_level=lod,
        output_dir=output_dir,
        dimension=dimension,
        scale_factor=scale_factor,
        workers=gather_workers,
        elevation_data_dir=elevation_data_dir,
        elevation_padding_deg=elevation_padding_deg,
    )


def _is_missing_lod_error(exc: BaseException) -> bool:
    """True when a gather failed because its LOD is absent from the pyramid.

    discover_survivors raises this ValueError when the requested ``lod`` has no
    surviving tiles (e.g. gdal2tiles emitted only the top level). That is the
    signal to fall back to a dedicated download for this LOD; any other
    ValueError (bad dimension/scale_factor) must propagate.
    """
    return isinstance(exc, ValueError) and "No surviving tiles at LOD" in str(exc)


def main_process_terrain(
    *,
    name: str,
    shape_file: str,
    output: str = ".",
    dimension: int,
    lod: int,
    with_elevation: bool = False,
    keep_tiles: bool = False,
    scale_factor: float = 1.0,
    workers: int = 32,
    processes: int = 32,
    gather_workers: Optional[int] = None,
    chunk_px: int = 256,
    timeout: int = 30,
    resampling: str = "lanczos",
    service_index: Optional[int] = None,
) -> None:
    """Run a full download + gather pass for one requested LOD.

    See the module docstring for the output layout. This is pure orchestration:
    it calls the already-wired ``download-arcgis`` / ``gather-ortho
    --from-download`` / ``download-elevation`` entrypoints in sequence, so the
    output schema (gathered_r*_c*.png + height_info.json + copied
    elevation/shape) is identical to running the commands by hand.
    """
    if dimension < _MIN_DIMENSION:
        raise ValueError(
            f"dimension must be >= {_MIN_DIMENSION} (chunking is required: "
            f"d=1 would emit one file per tile, thousands for a real AOI)"
        )
    if lod <= 0:
        raise ValueError("lod must be greater than zero")
    if not (0.0 < scale_factor <= 1.0):
        raise ValueError(
            "scale_factor must be in (0.0, 1.0]; only downscaling is supported"
        )

    os.makedirs(output, exist_ok=True)
    tiles_dir = os.path.join(output, f"{name}_tiles")
    elevation_dir = os.path.join(output, f"{name}_elevation")
    cleanup_dirs: list[str] = []

    # 1) One download at the highest requested LOD builds the shared pyramid.
    print(f"Downloading orthoimagery at LOD {lod} -> {tiles_dir}")
    main_arcgis_downloader(
        **_download_kwargs(
            shape_file=shape_file,
            outdir=tiles_dir,
            lod=lod,
            workers=workers,
            chunk_px=chunk_px,
            timeout=timeout,
            resampling=resampling,
            processes=processes,
            service_index=service_index,
        )
    )
    cleanup_dirs.append(tiles_dir)

    # 2) Optional continuous elevation GeoTIFF, fed to the gather.
    elevation_data_dir: Optional[str] = None
    if with_elevation:
        os.makedirs(elevation_dir, exist_ok=True)
        elev_path = os.path.join(elevation_dir, "elevation_merged.tif")
        print(f"Downloading elevation -> {elev_path}")
        main_elevation(
            shape_file=shape_file,
            outdir=elev_path,
            num_workers=workers,
            chunk_px=chunk_px,
            timeout=timeout,
            service_index=service_index,
        )
        elevation_data_dir = elevation_dir

    # 3) Gather the requested LOD into <name>_<lod>.
    tier_dir = os.path.join(output, f"{name}_{lod}")
    print(f"Gathering tier LOD {lod} -> {tier_dir}")
    try:
        main_ortho_arcgis_import_from_download(
            **_gather_kwargs(
                shape_file=shape_file,
                download_dir=tiles_dir,
                lod=lod,
                output_dir=tier_dir,
                dimension=dimension,
                scale_factor=scale_factor,
                gather_workers=gather_workers,
                elevation_data_dir=elevation_data_dir,
                elevation_padding_deg=DEFAULT_PADDING_DEG,
            )
        )
    except ValueError as exc:
        if not _is_missing_lod_error(exc):
            raise
        # The shared pyramid did not contain this LOD (gdal2tiles emitted
        # only the top level). Fall back to a dedicated download at this
        # LOD so the command is correct regardless of -z semantics.
        tier_tiles = os.path.join(output, f"{name}_{lod}_tiles")
        print(
            f"LOD {lod} not present in shared pyramid; downloading it "
            f"separately -> {tier_tiles}"
        )
        main_arcgis_downloader(
            **_download_kwargs(
                shape_file=shape_file,
                outdir=tier_tiles,
                lod=lod,
                workers=workers,
                chunk_px=chunk_px,
                timeout=timeout,
                resampling=resampling,
                processes=processes,
                service_index=service_index,
            )
        )
        cleanup_dirs.append(tier_tiles)
        main_ortho_arcgis_import_from_download(
            **_gather_kwargs(
                shape_file=shape_file,
                download_dir=tier_tiles,
                lod=lod,
                output_dir=tier_dir,
                dimension=dimension,
                scale_factor=scale_factor,
                gather_workers=gather_workers,
                elevation_data_dir=elevation_data_dir,
                elevation_padding_deg=DEFAULT_PADDING_DEG,
            )
        )
    # 4) Drop intermediate tile pyramids unless the user asked to keep them.
    if not keep_tiles:
        for d in cleanup_dirs:
            shutil.rmtree(d, ignore_errors=True)
        print(
            "Removed intermediate tile pyramids (use --keep-tiles to retain "
            "them for manual re-runs)."
        )

    print("process-terrain complete. Outputs:")
    print(f"  {tier_dir}  (LOD {lod})")
    if with_elevation:
        print(f"  {elevation_dir}  (elevation)")
