"""Web Mercator level-of-detail (LOD) math shared by the downloaders.

A Web Mercator LOD ``z`` is the integer zoom of the standard 256-px tile
pyramid: level 0 has one 256-px tile spanning the whole world
(156543.03392804097 m/px) and every subsequent level halves the resolution.

Talking in LODs rather than raw m/px matters for alignment: an elevation
raster fetched at ``pixel_size_for_zoom(z)`` and anchored to the tile grid
(see :func:`tile_grid_envelope`) lands pixel-for-pixel on the same grid as
the ortho tile pyramid at the same LOD, so no client-side resampling is
needed to line the two up.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a runtime cycle
    from terrain_stitcher.arcgis.services import ImageryService


@dataclass()
class LevelOfDetailInfo:
    level_id: int
    scale: float
    resolution: float


# -- Web Mercator pyramid constants -------------------------------------------
# These must stay in lockstep with TileSchemeInfo.from_web_mercator and the
# gdal2tiles tile pyramid the ortho path produces, so they are single-sourced
# here and imported from everywhere else.

WEB_MERCATOR_TILE_PX = 256
WEB_MERCATOR_INITIAL_RESOLUTION = 2 * math.pi * 6378137.0 / WEB_MERCATOR_TILE_PX
WEB_MERCATOR_WORLD_X_MIN = -20037508.342787001
WEB_MERCATOR_WORLD_Y_MAX = 20037508.342787001


def pixel_size_for_zoom(zoom: int) -> float:
    """Standard Web Mercator resolution (m/px) at zoom *zoom*.

    Equals the ortho tile pyramid's per-pixel ground size at that level, so
    a raster fetched at this resolution and anchored to the tile grid is
    1:1 aligned with ortho tiles of the same LOD.
    """
    if zoom < 0:
        raise ValueError(f"LOD must be >= 0, got {zoom}")
    return WEB_MERCATOR_INITIAL_RESOLUTION / (2**zoom)


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
    return round(
        math.log2(WEB_MERCATOR_INITIAL_RESOLUTION / service.native_pixel_size_m)
    )


def web_mercator_lod_for_res(pixel_size_m: float) -> int | None:
    """The integer LOD whose exact pixel size equals *pixel_size_m*, else None.

    Returns ``None`` when the resolution does not sit on the Web Mercator
    pyramid (i.e. is not half of level 0's resolution some integer number of
    times), so callers can treat the raster as not grid-aligned.
    """
    if pixel_size_m is None or pixel_size_m <= 0:
        return None
    exact = math.log2(WEB_MERCATOR_INITIAL_RESOLUTION / pixel_size_m)
    nearest = round(exact)
    if abs(exact - nearest) > 1e-6:
        return None  # resolution is not on the Web Mercator pyramid
    if nearest < 0:
        return None  # coarser than LOD 0: not part of the standard grid
    return nearest


def tile_grid_envelope(
    xmin: float, ymin: float, xmax: float, ymax: float, lod: int
) -> tuple[float, float, float, float, int, int, int, int]:
    """Snap a Web Mercator bbox outward onto the 256-px tile grid at *lod*.

    Returns ``(env_xmin, env_ymin, env_xmax, env_ymax, col0, row0, n_cols,
    n_rows)`` where the envelope is the union of the whole tiles (starting at
    tile column ``col0`` and row ``row0``) that cover the input bbox. Rounding
    is outward, so the envelope always contains the input bbox; a bbox that
    already sits on tile boundaries is returned unchanged.

    This is the anchoring that makes alignment work: pixel edges derived from
    the world origin + ``n * pixel_size_for_zoom(lod)`` coincide exactly with
    the ortho tile pyramid's pixel edges at the same LOD.
    """
    res = pixel_size_for_zoom(lod)
    tile_m = WEB_MERCATOR_TILE_PX * res
    # Tiny epsilon so a bbox that lands exactly on a tile boundary (up to
    # float noise) does not gain a spurious extra row/col of tiles.
    eps = 1e-9
    col0 = math.floor((xmin - WEB_MERCATOR_WORLD_X_MIN) / tile_m + eps)
    col1 = math.ceil((xmax - WEB_MERCATOR_WORLD_X_MIN) / tile_m - eps)
    row0 = math.floor((WEB_MERCATOR_WORLD_Y_MAX - ymax) / tile_m + eps)
    row1 = math.ceil((WEB_MERCATOR_WORLD_Y_MAX - ymin) / tile_m - eps)

    env_xmin = WEB_MERCATOR_WORLD_X_MIN + col0 * tile_m
    env_xmax = WEB_MERCATOR_WORLD_X_MIN + col1 * tile_m
    env_ymax = WEB_MERCATOR_WORLD_Y_MAX - row0 * tile_m
    env_ymin = WEB_MERCATOR_WORLD_Y_MAX - row1 * tile_m
    return env_xmin, env_ymin, env_xmax, env_ymax, col0, row0, col1 - col0, row1 - row0