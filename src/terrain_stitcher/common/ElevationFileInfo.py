"""Sidecar metadata for merged elevation GeoTIFFs.

``download-elevation`` and the elevation merge commands write a single merged
GeoTIFF (by default ``elevation_merged.tif``) plus a twin
``elevation_merged_info.json`` next to it. The sidecar records the projection
system of the GeoTIFF so consumers do not have to guess between the aligned
Web Mercator output (EPSG:3857) and the legacy composited output (EPSG:4326):
with the aligned pipeline the elevation raster is in projected meters, not
degrees, and horizontal scale must be corrected by cos(latitude) at the
rendering site.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Union

__all__ = [
    "ElevationFileInfo",
    "elevation_info_path",
    "write_elevation_info",
]


@dataclass
class ElevationFileInfo:
    """Contents of ``elevation_merged_info.json``.

    Members:
        projection: The projection system used for the GeoTIFF, e.g.
            ``"EPSG:3857"`` (Web Mercator, tile-grid aligned) or
            ``"EPSG:4326"`` (legacy composited output).
    """

    projection: str


def elevation_info_path(geotiff_path: Union[str, Path]) -> Path:
    """The sidecar JSON path that belongs to *geotiff_path*.

    ``<dir>/elevation_merged.tif`` -> ``<dir>/elevation_merged_info.json``.
    """
    geotiff_path = Path(geotiff_path)
    return geotiff_path.with_name(geotiff_path.stem + "_info.json")


def write_elevation_info(geotiff_path: Union[str, Path], info: ElevationFileInfo) -> Path:
    """Write *info* next to *geotiff_path* and return the sidecar path."""
    path = elevation_info_path(geotiff_path)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(asdict(info), fh, indent=2)
        fh.write("\n")
    return path