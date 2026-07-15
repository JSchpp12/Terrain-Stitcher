from __future__ import annotations

import numpy as np
import pyproj

from terrain_stitcher.arcgis.cache_xml import ArcGisCacheInfo
from terrain_stitcher.arcgis.tile_info import TileInfo
from terrain_stitcher.arcgis.tile_bounds import TileFootprints
from terrain_stitcher.common import World_Bounding_Box


class ShapeTileFilter:
    """Callable filter that includes tiles overlapping a shape region.

    The shape region (a WGS84 lat/lon bounding box) is projected once into the
    cache's CRS (e.g. Web Mercator meters). ``__call__(tile)`` then computes
    the tile's footprint in that same CRS -- cheap, no per-tile reprojection --
    and tests axis-aligned overlap. Operating on :class:`TileInfo` directly
    lets the acquisition filter tiles before the bulk bounds / zip work.

    A tile is included if ANY part of it falls within the shape bounds -- i.e.
    overlap, not full containment. The overlap test uses closed intervals, so a
    tile that merely shares an edge or a corner with the shape region is still
    included.

    Two entry points are provided:

    * :meth:`__call__` -- per-tile test, kept for callers/tests that want a
      boolean predicate (e.g. ``[t for t in tiles if f(t)]``).
    * :meth:`mask` -- vectorized test over a precomputed
      :class:`TileFootprints` (see
      :meth:`TileBoundsCalculator.projected_footprints`). This lets
      :meth:`ArcGisProAcquisitionSource.acquire` compute the footprints once,
      filter with this mask, and hand the surviving footprints back to
      :meth:`TileBoundsCalculator.bounds_for_all`, so the footprint arithmetic
      runs a single time instead of twice.
    """

    def __init__(
        self,
        cache_info: ArcGisCacheInfo,
        region: World_Bounding_Box,
    ) -> None:
        self._cache = cache_info
        self._res_by_id = {lvl.level_id: lvl.resolution for lvl in cache_info.levels}

        to_cache = pyproj.Transformer.from_crs(
            "EPSG:4326",
            f"EPSG:{cache_info.spatial_reference.value}",
            always_xy=True,
        )
        ll = region.get_lower_left()
        ur = region.get_upper_right()
        # always_xy -> input (lon, lat); lower_left is (min_lat, min_lon)
        min_x, min_y = to_cache.transform(ll.get_lon(), ll.get_lat())
        max_x, max_y = to_cache.transform(ur.get_lon(), ur.get_lat())
        self._box = (min_x, min_y, max_x, max_y)

    @property
    def box(self) -> tuple:
        """Projected (min_x, min_y, max_x, max_y) shape region in cache CRS."""
        return self._box


    @classmethod
    def from_box(cls, cache_info: "ArcGisCacheInfo", box: tuple) -> "ShapeTileFilter":
        """Build a filter from a precomputed projected box, no Transformer.

        ``__init__`` builds a pyproj Transformer only to project the shape
        region into the cache CRS and store ``self._box``. Once that box is
        known (the main process computes it once), a worker subprocess can
        reconstruct a fully-functional filter from just the box + cache_info:
        ``mask`` only needs ``_box`` and ``_res_by_id``, never the transformer.
        Bypassing ``__init__`` keeps the non-picklable transformer out of the
        worker's pickle path.
        """
        f = cls.__new__(cls)
        f._cache = cache_info
        f._res_by_id = {lvl.level_id: lvl.resolution for lvl in cache_info.levels}
        f._box = box
        return f

    def __call__(self, tile: TileInfo) -> bool:
        try:
            res = self._res_by_id[tile.layer_number]
        except KeyError:
            raise ValueError(
                f"No level-of-detail declared for level id {tile.layer_number}"
            )

        tw = self._cache.tile_cols * res
        th = self._cache.tile_rows * res
        ox, oy = self._cache.tile_origin_x, self._cache.tile_origin_y

        x0 = ox + tile.col_number * tw
        y0 = oy - tile.row_number * th  # top edge (rows go south)
        x1 = x0 + tw
        y1 = y0 - th  # bottom edge

        min_x, min_y, max_x, max_y = self._box
        # tile footprint [x0, x1] x [y1, y0]; shape box [min_x, max_x] x [min_y, max_y].
        # Closed-interval AABB overlap: include if the two rectangles share any
        # point (so a shared edge/corner counts), not just positive-area overlap.
        return (x0 <= max_x) and (x1 >= min_x) and (y1 <= max_y) and (y0 >= min_y)

    def mask(self, footprints: TileFootprints) -> np.ndarray:
        """Vectorized closed-interval AABB overlap test over ``footprints``.

        ``footprints`` are the tile footprints in the cache's CRS, as produced
        by :meth:`TileBoundsCalculator.projected_footprints` (a
        :class:`TileFootprints` with one entry per tile). Returns a boolean
        numpy array, ``True`` where the corresponding tile overlaps the shape
        region. Mirrors :meth:`__call__` exactly, including the closed-
        interval semantics (shared edge/corner counts as overlap).

        Level-id validation is intentionally NOT repeated here: the caller
        has already resolved resolutions to build the footprints (raising
        ``ValueError`` on an unknown level id there), so the mask only needs
        the geometric test.
        """
        min_x, min_y, max_x, max_y = self._box
        # tile footprint [west_x, east_x] x [south_y, north_y];
        # shape box [min_x, max_x] x [min_y, max_y].
        x0 = np.asarray(footprints.west_x)
        x1 = np.asarray(footprints.east_x)
        y0 = np.asarray(footprints.north_y)
        y1 = np.asarray(footprints.south_y)
        return (x0 <= max_x) & (x1 >= min_x) & (y1 <= max_y) & (y0 >= min_y)
