import enum
from dataclasses import dataclass
from typing import Optional
from terrain_stitcher.common.bounds import Bounds


class TileSide(enum.Enum):
    NORTH = "north"
    EAST = "east"
    SOUTH = "south"
    WEST = "west"


@dataclass
class Tile:
    """A single ortho tile and its geographic bounds.

    A tile carries no neighbor/position state of its own; placement within a
    merged image comes from the window-relative positions captured on its
    GatheredTiles group (snapshotted from the grid at partition time).
    """

    name: str
    image_path: str
    # Optional for the ArcGIS streaming path, which derives a group's envelope
    # from cache geometry instead of storing per-tile Bounds. The USGS path
    # always sets a real Bounds.
    bounds: Optional[Bounds]

    # edge helpers (bounds corners are rectangular for both USGS & ArcGIS tiles)
    def north_lat(self) -> float:
        return self.bounds.coords_northWest.get_lat()

    def south_lat(self) -> float:
        return self.bounds.coords_southEast.get_lat()

    def west_lon(self) -> float:
        return self.bounds.coords_northWest.get_lon()

    def east_lon(self) -> float:
        return self.bounds.coords_northEast.get_lon()
