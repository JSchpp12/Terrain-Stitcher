"""Create Shape.json files for incomplete WeatherCams sites."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from terrain_stitcher.common import (
    ParseArea,
    TerrainBoundsCalculateType,
    World_Coordinates,
)
from terrain_stitcher.weathercams.client import WeathercamSite
from terrain_stitcher.weathercams.ledger import load_weathercam_ledger


@dataclass(frozen=True)
class ShapePreparationResult:
    created_site_ids: list[int]
    skipped_site_ids: list[int]


def prepare_weathercam_shapes(
    sites: Iterable[WeathercamSite],
    *,
    ledger_path: Path,
    shape_dir: Path,
    view_distance: float,
    limit: Optional[int] = None,
) -> ShapePreparationResult:
    """Write Shape.json files for incomplete sites, up to ``limit`` sites.

    Completed sites are skipped and do not consume the limit. When ``limit`` is
    ``None``, every incomplete site is prepared.
    """
    if view_distance <= 0:
        raise ValueError("view_distance must be greater than zero")
    if limit is not None and limit <= 0:
        raise ValueError("limit must be greater than zero")

    ledger = load_weathercam_ledger(ledger_path)
    created_site_ids: list[int] = []
    skipped_site_ids: list[int] = []

    for site in sites:
        site_id = site.site_id
        key = str(site_id)
        if key not in ledger:
            raise ValueError(
                f"WeatherCams site {site_id} is not in the ledger. "
                "Run weathercams-sync before weathercams-prepare."
            )
        if ledger[key]:
            skipped_site_ids.append(site_id)
            continue

        shape_path = shape_dir / f"{site_id}.json"
        write_weathercam_shape(
            site,
            shape_path=shape_path,
            view_distance=view_distance,
        )
        created_site_ids.append(site_id)
        if limit is not None and len(created_site_ids) >= limit:
            break

    return ShapePreparationResult(
        created_site_ids=sorted(created_site_ids),
        skipped_site_ids=sorted(skipped_site_ids),
    )


def write_weathercam_shape(
    site: WeathercamSite, *, shape_path: Path, view_distance: float
) -> None:
    """Write one WeatherCams site to the existing Shape.json schema."""
    area = ParseArea(
        TerrainBoundsCalculateType.POINT,
        World_Coordinates(str(site.latitude), str(site.longitude)),
        view_distance,
    )

    # Force coordinate and bounds validation before writing the file.
    region = area.getTotalRegion()
    region.get_lower_left().get_lat()
    region.get_lower_left().get_lon()
    region.get_upper_right().get_lat()
    region.get_upper_right().get_lon()

    shape_path.parent.mkdir(parents=True, exist_ok=True)
    shape_path.write_text(
        json.dumps(area.toJSON(), indent=4) + "\n",
        encoding="utf-8",
    )
