from dataclasses import dataclass

from terrain_stitcher.arcgis.tile_info import TileInfo
from terrain_stitcher.common.bounds import Bounds

@dataclass
class BoundedTileInfo:
    """A :class:`TileInfo` paired with its computed WGS84 :class:`Bounds`.

    Produced by :meth:`TileBoundsCalculator.bounds_for_all` and consumed by
    the zip/sidecar processing in :mod:`terrain_stitcher.arcgis.tile_zip`.
    Bundling the two keeps the (tile, bounds) pair from drifting out of sync
    as it flows through the parallel processing pipeline -- instead of two
    parallel lists that must be kept length-aligned by hand, a single list
    of these dataclasses carries each tile together with its own bounds.
    """

    tile: "TileInfo"
    bounds: "Bounds"
