import pyproj

from shapely.geometry import Polygon
from shapely.ops import transform
from rtree import index

from terrain_stitcher.sources import Bounds

# Projector for WGS84 → Web Mercator (meters)
project = pyproj.Transformer.from_crs(
    "EPSG:4326", "EPSG:3857", always_xy=True
).transform


class Terrain_Data:
    def __init__(self, record, bounds: Bounds):
        self.record = record
        self.bounds = bounds


def toProjected(polygon):
    return transform(project, polygon)


def toPolygon(terrainChunk: Terrain_Data):
    bounds = terrainChunk.bounds
    return Polygon(
        [
            (bounds.coords_northWest.get_lon(), bounds.coords_northWest.get_lat()),
            (bounds.coords_northEast.get_lon(), bounds.coords_northEast.get_lat()),
            (bounds.coords_southEast.get_lon(), bounds.coords_southEast.get_lat()),
            (bounds.coords_southWest.get_lon(), bounds.coords_southWest.get_lat()),
            (bounds.coords_northWest.get_lon(), bounds.coords_northWest.get_lat()),
        ]
    )


def buildRTree(polygons):
    idx = index.Index()
    for i, poly in enumerate(polygons):
        idx.insert(i, poly.bounds)
    return idx
