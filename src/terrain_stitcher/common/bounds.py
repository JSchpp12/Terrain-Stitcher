from terrain_stitcher.common.TerrainArea import World_Coordinates


class Bounds:
    def __init__(
        self,
        coords_northEast: World_Coordinates,
        coords_southEast: World_Coordinates,
        coords_southWest: World_Coordinates,
        coords_northWest: World_Coordinates,
        coords_center: World_Coordinates,
    ):
        self.coords_northEast = coords_northEast
        self.coords_southEast = coords_southEast
        self.coords_southWest = coords_southWest
        self.coords_northWest = coords_northWest
        self.coords_center = coords_center

    def isValid(self) -> bool:
        return (
            self.coords_northEast.isValid()
            and self.coords_southEast.isValid()
            and self.coords_southWest.isValid()
            and self.coords_northWest.isValid()
            and self.coords_center.isValid()
        )

    def getCenter(self) -> World_Coordinates:
        return self.coords_center

    def toJSON(self):
        return {
            "center": self.coords_center.toJSON(),
            "northEast": self.coords_northEast.toJSON(),
            "southEast": self.coords_southEast.toJSON(),
            "southWest": self.coords_southWest.toJSON(),
            "northWest": self.coords_northWest.toJSON(),
        }

    @classmethod
    def fromDict(cls, data):
        center = World_Coordinates.fromDict(data["center"])
        northEast = World_Coordinates.fromDict(data["northEast"])
        southEast = World_Coordinates.fromDict(data["southEast"])
        southWest = World_Coordinates.fromDict(data["southWest"])
        northWest = World_Coordinates.fromDict(data["northWest"])

        return cls(northEast, southEast, southWest, northWest, center)
