from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pyproj

from terrain_stitcher.arcgis.cache_xml import ArcGisCacheInfo
from terrain_stitcher.arcgis.tile_info import BoundedTileInfo, TileInfo
from terrain_stitcher.common import World_Coordinates
from terrain_stitcher.sources import Bounds


@dataclass
class TileFootprints:
    """Vectorized cache-CRS footprints for a batch of tiles (one entry each).

    All four fields are equal-length 1-D numpy arrays expressed in the cache's
    projected CRS (e.g. Web Mercator meters). Each tile's footprint is the
    axis-aligned rectangle bounded by its four edges::

        x in [west_x, east_x]   (west_x = north-west corner X, east_x = SE X)
        y in [south_y, north_y] (north_y = NW corner Y, south_y = SE Y)

    ArcGIS cache rows increase southward from the tiling origin, so the
    north edge is the larger northing and ``south_y <= north_y``.

    Produced by :meth:`TileBoundsCalculator.projected_footprints` and consumed
    by both :meth:`TileBoundsCalculator.bounds_for_all` (for the expensive
    WGS84 reprojection) and :meth:`ShapeTileFilter.mask` (for the cheap
    axis-aligned overlap test), so the footprint arithmetic runs once per
    batch instead of twice.
    """

    west_x: np.ndarray
    east_x: np.ndarray
    south_y: np.ndarray
    north_y: np.ndarray


class TileBoundsCalculator:
    """Computes WGS84 :class:`Bounds` for ArcGIS cache tiles.

    Each tile's footprint is computed in the cache's projected CRS (meters)
    from the tiling origin, the level's resolution, and the tile's row/col,
    then reprojected to WGS84 lat/lon. The ``pyproj`` transformer is built
    once and reused; :meth:`bounds_for_all` performs a single bulk reproject
    over every tile's corners and center using numpy arrays.

    Row indices increase southward from the (top-left) tiling origin, so the
    northing edge is computed by *subtracting* ``row * tile_height``.

    The projected footprint arithmetic (origin + col/row * tile size) is
    factored into :meth:`projected_footprints` so callers that need the
    cache-CRS footprint for something cheap -- most importantly
    :class:`ShapeTileFilter`'s axis-aligned overlap test -- can compute it
    once and hand the surviving arrays back to :meth:`bounds_for_all`,
    avoiding a second pass over the same arithmetic.
    """

    def __init__(self, cache_info: ArcGisCacheInfo) -> None:
        self._cache = cache_info
        self._res_by_id = {lvl.level_id: lvl.resolution for lvl in cache_info.levels}
        self._to_wgs84 = pyproj.Transformer.from_crs(
            f"EPSG:{cache_info.spatial_reference.value}",
            "EPSG:4326",
            always_xy=True,
        )

    def _resolution_for(self, level_id: int) -> float:
        try:
            return self._res_by_id[level_id]
        except KeyError:
            raise ValueError(f"No level-of-detail declared for level id {level_id}")

    def projected_footprints(self, tiles: list[TileInfo]) -> TileFootprints:
        """Vectorized cache-CRS footprints for ``tiles`` as a :class:`TileFootprints`.

        Each tile's footprint is the axis-aligned rectangle spanning its
        north-west and south-east corners in the cache's projected CRS;
        ``east_x = west_x + tile_width`` and ``south_y = north_y - tile_height``.
        No reprojection is performed -- this is the cheap arithmetic that
        callers (the shape filter's overlap test, or :meth:`bounds_for_all`)
        share.

        Raises ``ValueError`` if any tile references an undeclared level id,
        mirroring :meth:`_resolution_for`.
        """
        if not tiles:
            z = np.zeros(0, dtype=float)
            return TileFootprints(west_x=z, east_x=z, south_y=z, north_y=z)

        level_ids = np.array([t.layer_number for t in tiles])
        rows = np.array([t.row_number for t in tiles], dtype=float)
        cols = np.array([t.col_number for t in tiles], dtype=float)

        res = np.array([self._resolution_for(int(lid)) for lid in level_ids])
        tw = self._cache.tile_cols * res
        th = self._cache.tile_rows * res

        ox, oy = self._cache.tile_origin_x, self._cache.tile_origin_y
        west_x = ox + cols * tw
        north_y = oy - rows * th  # rows go south -> subtract
        east_x = west_x + tw
        south_y = north_y - th
        return TileFootprints(
            west_x=west_x,
            east_x=east_x,
            south_y=south_y,
            north_y=north_y,
        )

    def bounds_for(self, tile: TileInfo) -> Bounds:
        """Bounds for a single tile (NW, NE, SE, SW, center in WGS84)."""
        res = self._resolution_for(tile.layer_number)
        tw = self._cache.tile_cols * res
        th = self._cache.tile_rows * res
        ox, oy = self._cache.tile_origin_x, self._cache.tile_origin_y

        x0 = ox + tile.col_number * tw
        y0 = oy - tile.row_number * th  # rows go south -> subtract
        x1, y1 = x0 + tw, y0 - th
        xc, yc = (x0 + x1) / 2.0, (y0 + y1) / 2.0

        # NW, NE, SE, SW, center
        lon, lat = self._to_wgs84.transform(
            np.array([x0, x1, x1, x0, xc]),
            np.array([y0, y0, y1, y1, yc]),
        )
        return self._build_bounds(lon, lat)

    def bounds_for_all(
        self,
        tiles: list[TileInfo],
        footprints: Optional[TileFootprints] = None,
    ) -> list[BoundedTileInfo]:
        """Vectorized bounds for many tiles: one bulk reproject over all points.

        Returns each tile paired with its computed :class:`Bounds` as a
        :class:`BoundedTileInfo`, so callers carry a single aligned list
        (tile + bounds) into the zip/sidecar pipeline instead of two parallel
        lists that must be kept in sync by hand.

        ``footprints`` optionally supplies precomputed cache-CRS footprints
        (a :class:`TileFootprints`) -- e.g. from
        :meth:`projected_footprints` after a :class:`ShapeTileFilter` has
        masked them down to the survivors. When supplied, the footprint
        arithmetic is not recomputed; only the expensive WGS84 reprojection
        runs (and only over the survivors).
        """
        if not tiles:
            return []

        n = len(tiles)
        if footprints is not None:
            x0 = np.asarray(footprints.west_x, dtype=float)
            x1 = np.asarray(footprints.east_x, dtype=float)
            y0 = np.asarray(footprints.north_y, dtype=float)
            y1 = np.asarray(footprints.south_y, dtype=float)
            if x0.shape[0] != n:
                raise ValueError(
                    f"footprints length {x0.shape[0]} does not match tiles length {n}"
                )
        else:
            fps = self.projected_footprints(tiles)
            x0, x1 = fps.west_x, fps.east_x
            y0, y1 = fps.north_y, fps.south_y

        xc, yc = (x0 + x1) / 2.0, (y0 + y1) / 2.0

        # stack NW, NE, SE, SW, center for every tile -> a single reproject call
        xs = np.concatenate([x0, x1, x1, x0, xc])
        ys = np.concatenate([y0, y0, y1, y1, yc])
        lon, lat = self._to_wgs84.transform(xs, ys)

        lon = np.asarray(lon).reshape(5, n)
        lat = np.asarray(lat).reshape(5, n)
        return [
            BoundedTileInfo(
                tile=tiles[i], bounds=self._build_bounds(lon[:, i], lat[:, i])
            )
            for i in range(n)
        ]

    @staticmethod
    def _build_bounds(lon, lat) -> Bounds:
        """Build a Bounds from 5 (lon, lat) entries ordered NW, NE, SE, SW, center."""

        def coord(i: int) -> World_Coordinates:
            return World_Coordinates(
                lat=str(float(lat[i])),
                lon=str(float(lon[i])),
            )

        return Bounds(
            coords_northEast=coord(1),
            coords_southEast=coord(2),
            coords_southWest=coord(3),
            coords_northWest=coord(0),
            coords_center=coord(4),
        )
