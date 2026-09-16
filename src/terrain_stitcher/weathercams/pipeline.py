"""Run terrain passes for WeatherCams sites tracked by the minimal ledger."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from terrain_stitcher.weathercams.ledger import (
    load_weathercam_ledger,
    mark_site_complete,
    pending_site_ids,
)


@dataclass(frozen=True)
class ProcessTerrainOptions:
    """Options forwarded to the existing process-terrain pipeline."""

    dimension: int
    lod: int
    with_elevation: bool = False
    keep_tiles: bool = False
    scale_factor: float = 1.0
    workers: int = 32

    chunk_px: int = 256
    timeout: int = 30
    resampling: str = "lanczos"
    service_index: Optional[int] = None


@dataclass(frozen=True)
class WeathercamRunResult:
    processed_site_ids: list[int]
    failed_site_ids: list[int]


def _default_process_terrain() -> Callable[..., None]:
    """Load process-terrain lazily so ledger-only imports stay lightweight."""
    from terrain_stitcher.functions.FullPass import main_process_terrain

    return main_process_terrain


def run_weathercams(
    *,
    ledger_path: Path,
    shape_dir: Path,
    output_root: Path,
    options: ProcessTerrainOptions,
    name_prefix: str = "weathercam",
    only_site: Optional[int] = None,
    limit: Optional[int] = None,
    stop_on_error: bool = False,
    process_terrain: Optional[Callable[..., None]] = None,
) -> WeathercamRunResult:
    """Process incomplete WeatherCams sites and mark successful ones complete."""
    if options.dimension < 2:
        raise ValueError("dimension must be >= 2")
    if options.lod <= 0:
        raise ValueError("lod must be greater than zero")
    if not 0.0 < options.scale_factor <= 1.0:
        raise ValueError("scale_factor must be in the (0.0, 1.0]")
    if limit is not None and limit <= 0:
        raise ValueError("limit must be greater than zero")
    if not name_prefix:
        raise ValueError("name_prefix must not be empty")

    active_process_terrain = (
        process_terrain if process_terrain is not None else _default_process_terrain()
    )

    ledger = load_weathercam_ledger(ledger_path)
    selected_site_ids = _select_site_ids(
        ledger,
        only_site=only_site,
        limit=limit,
    )

    processed_site_ids: list[int] = []
    failed_site_ids: list[int] = []

    for site_id in selected_site_ids:
        shape_path = shape_dir / f"{site_id}.json"

        try:
            if not shape_path.is_file():
                raise FileNotFoundError(
                    f"Shape file for WeatherCams site {site_id} does not exist: "
                    f"{shape_path}. Run weathercams-prepare first."
                )

            process_weathercam_site(
                site_id,
                shape_path=shape_path,
                output_root=output_root,
                options=options,
                name_prefix=name_prefix,
                process_terrain=active_process_terrain,
            )
            mark_site_complete(ledger_path, site_id)
            processed_site_ids.append(site_id)
        except Exception as exc:
            failed_site_ids.append(site_id)
            print(f"WeatherCams site {site_id} failed: {exc}")
            if stop_on_error:
                raise

    return WeathercamRunResult(
        processed_site_ids=processed_site_ids,
        failed_site_ids=failed_site_ids,
    )


def process_weathercam_site(
    site_id: int,
    *,
    shape_path: Path,
    output_root: Path,
    options: ProcessTerrainOptions,
    name_prefix: str = "weathercam",
    process_terrain: Optional[Callable[..., None]] = None,
) -> None:
    """Run one site through process-terrain and validate its output."""
    name = weathercam_output_name(site_id, name_prefix=name_prefix)
    active_process_terrain = (
        process_terrain if process_terrain is not None else _default_process_terrain()
    )
    active_process_terrain(
        name=name,
        shape_file=str(shape_path),
        output=str(output_root),
        dimension=options.dimension,
        lod=options.lod,
        with_elevation=options.with_elevation,
        keep_tiles=options.keep_tiles,
        scale_factor=options.scale_factor,
        workers=options.workers,

        chunk_px=options.chunk_px,
        timeout=options.timeout,
        resampling=options.resampling,
        service_index=options.service_index,
    )
    validate_weathercam_outputs(
        output_root,
        name=name,
        lod=options.lod,
        with_elevation=options.with_elevation,
    )


def weathercam_output_name(site_id: int, *, name_prefix: str = "weathercam") -> str:
    return f"{name_prefix}_{site_id}"


def validate_weathercam_outputs(
    output_root: Path,
    *,
    name: str,
    lod: int,
    with_elevation: bool,
) -> None:
    """Ensure process-terrain produced non-empty manifests and image files."""
    tier_dir = output_root / f"{name}_{lod}"
    manifest_path = tier_dir / "height_info.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Missing terrain manifest: {manifest_path}")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid terrain manifest: {manifest_path}") from exc

    images = manifest.get("images")
    if not isinstance(images, list) or not images:
        raise ValueError(f"Terrain manifest has no images: {manifest_path}")

    for image in images:
        image_name = image.get("name") if isinstance(image, dict) else None
        if not image_name:
            raise ValueError(f"Terrain image entry has no name: {manifest_path}")
        image_path = tier_dir / f"{image_name}.png"
        if not image_path.is_file():
            raise FileNotFoundError(f"Missing terrain image: {image_path}")

    if with_elevation:
        elevation_path = output_root / f"{name}_elevation" / "elevation_merged.tif"
        if not elevation_path.is_file():
            raise FileNotFoundError(f"Missing elevation output: {elevation_path}")


def _select_site_ids(
    ledger: dict[str, bool],
    *,
    only_site: Optional[int],
    limit: Optional[int],
) -> list[int]:
    if only_site is not None:
        key = str(only_site)
        if key not in ledger:
            raise ValueError(f"Unknown WeatherCams site ID {only_site}")
        if ledger[key]:
            return []
        return [only_site]

    site_ids = pending_site_ids(ledger)
    if limit is not None:
        return site_ids[:limit]
    return site_ids
