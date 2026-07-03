import json
import os
from abc import abstractmethod

from terrain_stitcher.common import World_Coordinates


class DataInfoWriter:
    def __init__(self) -> None:
        pass

    @abstractmethod
    def writeFileContents(
        self, downloadDirPath, downloadedFileName: str, dataFilePath: str
    ):
        pass

    @abstractmethod
    def hasDataAlreadyBeenDownloaded(
        self, downloadDirPath: str, dataFilePath: str
    ) -> bool:
        pass


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


class ImageDataWriter(DataInfoWriter):
    def __init__(self, bounds: Bounds, imageFileName: str = None):
        self.bounds = bounds
        self.imageFileName = imageFileName

        super().__init__()

    def setImageFileName(self, imageFileName):
        self.imageFileName = imageFileName

    def toJSON(self):
        return {"bounds": self.bounds.toJSON(), "imageFileName": self.imageFileName}

    @staticmethod
    def ExtractImageFileName(dataInfoFilePath):
        parentDir = os.path.abspath(os.path.join(dataInfoFilePath, os.pardir))

        with open(dataInfoFilePath, "r") as file:
            jData = json.load(file)

            if "imageFileName" in jData:
                return jData["imageFileName"]
        return None

    @classmethod
    def fromDict(cls, data):
        bounds = Bounds.fromDict(data["bounds"])
        imageFileName = data["imageFileName"]
        return cls(bounds, imageFileName)

    def writeFileContents(self, downloadDirPath, downloadedFile, dataFilePath):
        fPath = os.path.join(downloadDirPath, dataFilePath)
        self.imageFileName = downloadedFile

        with open(fPath, "w") as jsonFile:
            json.dump(self.toJSON(), jsonFile, indent=4)

    def hasDataAlreadyBeenDownloaded(
        self, downloadDirPath: str, dataFilePath: str
    ) -> bool:
        dataInfoFile = os.path.join(downloadDirPath, dataFilePath)
        if os.path.isfile(dataInfoFile):
            mediaFilePath = ImageDataWriter.ExtractImageFileName(dataInfoFile)

            fullMediaFilePath = os.path.join(downloadDirPath, mediaFilePath)
            if os.path.isfile(fullMediaFilePath):
                return True

        return False
