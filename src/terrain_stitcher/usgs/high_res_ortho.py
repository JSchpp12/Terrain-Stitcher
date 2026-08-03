from .api_client import Client
from .data_source import DataSource, DataDownloadRequest, DataInfo
from terrain_stitcher.common import World_Coordinates
from terrain_stitcher.common.geometry import Terrain_Data
from terrain_stitcher.common.tile_overlap import (
    FindOverlappingChunks,
    GroupOverlappingChunks,
    SelectRepresentatives,
)
from terrain_stitcher.sources import ImageDataWriter
from terrain_stitcher.common.bounds import Bounds


def get_aerial_photography_datasets(usgs, bounding_box: World_Coordinates):
    aDatasets = []

    datasets = usgs.find_datasets_for(bounding_box, "high_res_ortho")
    for dataset in datasets:
        if (
            "keywords" in dataset
            and dataset["keywords"] is not None
            and "Aerial" in dataset["keywords"]
        ):
            aDatasets.append(dataset)

    return aDatasets[0]


class HighResolutionOrthoImagery(DataSource):
    def __init__(self, datasetName):
        self.name = datasetName

    def all_published_dates(scenes):
        published_dates = []
        for scene in scenes["results"]:
            date = scene["temporalCoverage"]["startDate"]
            if date not in published_dates:
                published_dates.append(scene["publishDate"])

        return published_dates

    @staticmethod
    def ExtractBounds(record) -> Bounds:
        NECorner = World_Coordinates()
        NWCorner = World_Coordinates()
        SECorner = World_Coordinates()
        SWCorner = World_Coordinates()
        Center = World_Coordinates()

        for meta in record["metadata"]:
            if NWCorner.lat is None and meta["fieldName"] == "NW Corner Lat dec":
                NWCorner.lat = meta["value"]
            if NWCorner.lon is None and meta["fieldName"] == "NW Corner Long dec":
                NWCorner.lon = meta["value"]
            if NECorner.lat is None and meta["fieldName"] == "NE Corner Lat dec":
                NECorner.lat = meta["value"]
            if NECorner.lon is None and meta["fieldName"] == "NE Corner Long dec":
                NECorner.lon = meta["value"]
            if SWCorner.lat is None and meta["fieldName"] == "SW Corner Lat dec":
                SWCorner.lat = meta["value"]
            if SWCorner.lon is None and meta["fieldName"] == "SW Corner Long dec":
                SWCorner.lon = meta["value"]
            if SECorner.lat is None and meta["fieldName"] == "SE Corner Lat dec":
                SECorner.lat = meta["value"]
            if SECorner.lon is None and meta["fieldName"] == "SE Corner Long dec":
                SECorner.lon = meta["value"]
            if Center.lat is None and meta["fieldName"] == "Center Latitude dec":
                Center.lat = meta["value"]
            if Center.lon is None and meta["fieldName"] == "Center Longitude dec":
                Center.lon = meta["value"]

        bounds = Bounds(NECorner, SECorner, SWCorner, NWCorner, Center)

        if not bounds.isValid():
            raise Exception("Failed to get bounds for set")

        return bounds

    def getDownloadRequests(
        self, usgsClient: Client, coords: World_Coordinates
    ) -> DataDownloadRequest:
        aerial_dataset = get_aerial_photography_datasets(usgsClient, coords)

        acquisition_filter = {"start": "2004-01-01", "end": "2004-05-05"}
        scenes = usgsClient.find_scenes(aerial_dataset, coords, acquisition_filter)

        # use same logic as before
        request = DataDownloadRequest(self.name)

        print("Processing scene bounds")
        allChunks = []
        for scene in scenes["results"]:
            allChunks.append(
                Terrain_Data(scene, HighResolutionOrthoImagery.ExtractBounds(scene))
            )
        print("Done")
        print(f"Number of total chunks: {len(allChunks)}")

        print("Processing overlaps")
        overlaps = FindOverlappingChunks(allChunks, 0.9)
        print("Done")

        print("Grouping overlaps")
        groups = GroupOverlappingChunks(overlaps, len(allChunks))
        print("Selecting representatives for overlaps")

        # create a chunk for each record
        selected = SelectRepresentatives(groups, allChunks)

        # find overlaps
        print("Done")

        for i in range(len(selected)):
            bounds = HighResolutionOrthoImagery.ExtractBounds(
                allChunks[selected[i]].record
            )
            imageWriter = ImageDataWriter(bounds)
            entityID = allChunks[selected[i]].record["entityId"]
            info = DataInfo(entityID, self.name, imageWriter)
            request.addDataInfo(info)

        return request
