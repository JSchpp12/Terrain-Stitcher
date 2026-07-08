from __future__ import annotations

import enum
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional


def _optional_int(value: Optional[str]) -> Optional[int]:
    if value is None or value.strip() == "":
        return None
    return int(value)


class CacheSpatialReference(enum.Enum):
    """The coordinate-system encoding declared by the cache's ``conf.xml``.

    Each member's value is the canonical EPSG code used to reproject the
    cache tiles to WGS84 lat/lon downstream. Add new members here as
    additional export projections are supported.
    """

    WGS_1984_Web_Mercator_Auxiliary_Sphere = 3857


def _resolve_spatial_reference(
    spatial: Optional[ET.Element],
) -> CacheSpatialReference:
    wkt = ""
    wkid: Optional[int] = None
    latest_wkid: Optional[int] = None
    if spatial is not None:
        wkt_el = spatial.find("WKT")
        if wkt_el is not None and wkt_el.text:
            wkt = wkt_el.text.strip()
        wkid = _optional_int(spatial.findtext("WKID"))
        latest_wkid = _optional_int(spatial.findtext("LatestWKID"))

    if (
        "WGS_1984_Web_Mercator_Auxiliary_Sphere" in wkt
        or wkid == 102100
        or latest_wkid == 3857
    ):
        return CacheSpatialReference.WGS_1984_Web_Mercator_Auxiliary_Sphere

    raise ValueError(
        f"Unsupported cache spatial reference "
        f"(wkid={wkid}, latest_wkid={latest_wkid})"
    )


@dataclass
class LevelOfDetailInfo:
    """A single level-of-detail entry parsed from the cache ``conf.xml``.

    Mirrors the Esri ``LODInfo`` element.
    """

    level_id: int
    scale: float
    resolution: float


@dataclass
class ArcGisCacheInfo:
    """Parsed contents of an ArcGISPro tile cache ``conf.xml``.

    This is the XML parser dataclass for the cache: it holds the
    information read from the xml file. The spatial reference is exposed
    as a :class:`CacheSpatialReference` enum rather than raw WKT. The
    acquisition source keeps the list of :class:`LevelOfDetailInfo`
    objects (``levels``) to drive per-level tile discovery.
    """

    spatial_reference: CacheSpatialReference
    tile_origin_x: float
    tile_origin_y: float
    tile_cols: int
    tile_rows: int
    dpi: int
    cache_tile_format: str
    storage_format: str
    levels: list[LevelOfDetailInfo] = field(default_factory=list)
    precise_dpi: Optional[int] = None

    @classmethod
    def from_xml(cls, path: str) -> "ArcGisCacheInfo":
        """Parse an ArcGISPro cache ``conf.xml`` file into a dataclass."""
        root = ET.parse(path).getroot()  # <CacheInfo>

        tile_cache = root.find("TileCacheInfo")
        if tile_cache is None:
            raise ValueError("conf.xml is missing <TileCacheInfo>")

        spatial_reference = _resolve_spatial_reference(
            tile_cache.find("SpatialReference")
        )

        origin = tile_cache.find("TileOrigin")
        tile_origin_x = float(origin.findtext("X")) if origin is not None else 0.0
        tile_origin_y = float(origin.findtext("Y")) if origin is not None else 0.0

        tile_cols = int(tile_cache.findtext("TileCols"))
        tile_rows = int(tile_cache.findtext("TileRows"))
        dpi = int(tile_cache.findtext("DPI"))
        precise_dpi = _optional_int(tile_cache.findtext("PreciseDPI"))

        lod_infos = tile_cache.find("LODInfos")
        levels: list[LevelOfDetailInfo] = []
        if lod_infos is not None:
            for lod in lod_infos.findall("LODInfo"):
                levels.append(
                    LevelOfDetailInfo(
                        level_id=int(lod.findtext("LevelID")),
                        scale=float(lod.findtext("Scale")),
                        resolution=float(lod.findtext("Resolution")),
                    )
                )

        tile_image = root.find("TileImageInfo")
        cache_tile_format = (
            tile_image.findtext("CacheTileFormat") if tile_image is not None else ""
        )

        storage = root.find("CacheStorageInfo")
        storage_format = (
            storage.findtext("StorageFormat") if storage is not None else ""
        )

        return cls(
            spatial_reference=spatial_reference,
            tile_origin_x=tile_origin_x,
            tile_origin_y=tile_origin_y,
            tile_cols=tile_cols,
            tile_rows=tile_rows,
            dpi=dpi,
            precise_dpi=precise_dpi,
            cache_tile_format=cache_tile_format,
            storage_format=storage_format,
            levels=levels,
        )
