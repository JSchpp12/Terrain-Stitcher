import json
import os
from abc import abstractmethod

from terrain_stitcher.common import World_Coordinates
from terrain_stitcher.common.bounds import Bounds


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
