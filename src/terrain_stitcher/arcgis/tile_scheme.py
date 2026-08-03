from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

import xml.etree.ElementTree as ET


@dataclass()
class LevelOfDetailInfo:
    level_id: int
    scale: float
    resolution: float


@dataclass()
class TileSchemeInfo:
    """Source-independent tile scheme metadata.

    This describes how to interpret tile row/column coordinates for a tiled
    raster cache. It is intentionally not tied to ArcGIS Pro's conf.xml format.

    The same structure can be constructed from:
      - ArcGIS Pro conf.xml
      - generated Web Mercator defaults
      - gdal2tiles output conventions
      - ImageServer metadata plus local tiling settings
    """

    wkid: int
    latest_wkid: int
    tile_origin_x: float
    tile_origin_y: float
    tile_cols: int
    tile_rows: int
    dpi: int
    cache_tile_format: str
    levels: list[LevelOfDetailInfo] = field(default_factory=list)

    # Optional metadata. These are useful for ArcGIS compatibility but should
    # not be required by generic tile processing code.
    storage_format: str = ""
    precise_dpi: Optional[int] = None

    @property
    def pyproj_epsg(self) -> int:
        return self.latest_wkid or self.wkid

    @classmethod
    def from_arcgis_conf_xml(cls, path: str) -> "TileSchemeInfo":
        """Parse an ArcGIS Pro cache conf.xml into a generic tile scheme."""
        root = ET.parse(path).getroot()

        tile_cache = root.find("TileCacheInfo")
        if tile_cache is None:
            raise ValueError("conf.xml is missing <TileCacheInfo>")

        spatial_ref = tile_cache.find("SpatialReference")
        if spatial_ref is None:
            raise ValueError("conf.xml is missing <SpatialReference>")

        wkid = int(spatial_ref.findtext("WKID") or 0)
        latest_wkid = int(spatial_ref.findtext("LatestWKID") or wkid)

        origin = tile_cache.find("TileOrigin")
        if origin is None:
            raise ValueError("conf.xml is missing <TileOrigin>")

        tile_origin_x = float(_required_text(origin, "X"))
        tile_origin_y = float(_required_text(origin, "Y"))

        tile_cols = int(_required_text(tile_cache, "TileCols"))
        tile_rows = int(_required_text(tile_cache, "TileRows"))
        dpi = int(_required_text(tile_cache, "DPI"))
        precise_dpi = _optional_int(tile_cache.findtext("PreciseDPI"))

        levels: list[LevelOfDetailInfo] = []
        lod_infos = tile_cache.find("LODInfos")
        if lod_infos is not None:
            for lod in lod_infos.findall("LODInfo"):
                levels.append(
                    LevelOfDetailInfo(
                        level_id=int(_required_text(lod, "LevelID")),
                        scale=float(_required_text(lod, "Scale")),
                        resolution=float(_required_text(lod, "Resolution")),
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
            wkid=wkid,
            latest_wkid=latest_wkid,
            tile_origin_x=tile_origin_x,
            tile_origin_y=tile_origin_y,
            tile_cols=tile_cols,
            tile_rows=tile_rows,
            dpi=dpi,
            precise_dpi=precise_dpi,
            cache_tile_format=cache_tile_format.lower(),
            storage_format=storage_format,
            levels=levels,
        )

    @classmethod
    def from_web_mercator(
        cls,
        *,
        min_level: int,
        max_level: int,
        tile_size: int = 256,
        dpi: int = 96,
        cache_tile_format: str = "png",
        storage_format: str = "directory",
    ) -> "TileSchemeInfo":
        """Build a standard Web Mercator tile scheme.

        This matches the usual ArcGIS/Google/OSM Web Mercator pyramid:
          level 0 resolution = 156543.03392804097 m/px
          each subsequent level halves the resolution
          origin is top-left Web Mercator world extent
        """
        if min_level < 0:
            raise ValueError("min_level must be >= 0")
        if max_level < min_level:
            raise ValueError("max_level must be >= min_level")
        if tile_size <= 0:
            raise ValueError("tile_size must be > 0")
        if dpi <= 0:
            raise ValueError("dpi must be > 0")

        initial_resolution = 156543.03392804097

        levels: list[LevelOfDetailInfo] = []
        for level_id in range(min_level, max_level + 1):
            resolution = initial_resolution / (2**level_id)

            # ArcGIS conf.xml scale convention:
            # scale denominator = ground_resolution_m_per_px * dpi / meters_per_inch
            scale = resolution * dpi / 0.0254

            levels.append(
                LevelOfDetailInfo(
                    level_id=level_id,
                    scale=scale,
                    resolution=resolution,
                )
            )

        return cls(
            wkid=102100,
            latest_wkid=3857,
            tile_origin_x=-20037508.342787001,
            tile_origin_y=20037508.342787001,
            tile_cols=tile_size,
            tile_rows=tile_size,
            dpi=dpi,
            precise_dpi=dpi,
            cache_tile_format=cache_tile_format.lower(),
            storage_format=storage_format,
            levels=levels,
        )

    def level_by_id(self, level_id: int) -> LevelOfDetailInfo:
        for level in self.levels:
            if level.level_id == level_id:
                return level
        raise KeyError(f"LOD {level_id} not present in tile scheme")

    def tile_bounds(
        self, level_id: int, row: int, col: int
    ) -> tuple[float, float, float, float]:
        """Return projected bounds for a tile as xmin, ymin, xmax, ymax.

        Assumes ArcGIS/XYZ-style top-left origin where rows increase downward.
        """
        level = self.level_by_id(level_id)
        res = level.resolution

        xmin = self.tile_origin_x + col * self.tile_cols * res
        xmax = xmin + self.tile_cols * res

        ymax = self.tile_origin_y - row * self.tile_rows * res
        ymin = ymax - self.tile_rows * res

        return xmin, ymin, xmax, ymax


def _required_text(parent: ET.Element, child_name: str) -> str:
    value = parent.findtext(child_name)
    if value is None:
        raise ValueError(f"Missing required XML element <{child_name}>")
    return value


def _optional_int(value: Optional[str]) -> Optional[int]:
    if value is None or value == "":
        return None
    return int(float(value))
