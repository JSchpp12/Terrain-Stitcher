import pyproj
import json
import os

from shapely.geometry import Polygon
from shapely.ops import transform
from collections import defaultdict
from rtree import index

from .DataSource import DataSource, DataDownloadRequest, DataInfoWriter, DataInfo
from terrain_stitcher.usgs import Client
from terrain_stitcher.common import World_Coordinates

# Projector for WGS84 → Web Mercator (meters)
project = pyproj.Transformer.from_crs(
    "EPSG:4326", "EPSG:3857", always_xy=True
).transform

def get_aerial_photography_datasets(usgs, bounding_box: World_Coordinates):
    aDatasets = []

    prime = []
    test_datasets = usgs.find_datasets_for(bounding_box)
    for test in test_datasets: 
        if test['abstractText'] is not None and "NED" in test['abstractText']:
            prime.append(test)


    datasets = usgs.find_datasets_for(bounding_box, "high_res_ortho")
    for dataset in datasets:
        if (
            "keywords" in dataset
            and dataset["keywords"] is not None
            and "Aerial" in dataset["keywords"]
        ):
            aDatasets.append(dataset)

    return aDatasets[0]


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
            'center': self.coords_center.toJSON(),
            'northEast': self.coords_northEast.toJSON(), 
            'southEast': self.coords_southEast.toJSON(),
            'southWest': self.coords_southWest.toJSON(),
            'northWest': self.coords_northWest.toJSON() 
        }
    
    @classmethod
    def fromDict(cls, data): 
        center = World_Coordinates.fromDict(data['center'])
        northEast = World_Coordinates.fromDict(data['northEast'])
        southEast = World_Coordinates.fromDict(data['southEast'])
        southWest = World_Coordinates.fromDict(data['southWest'])
        northWest = World_Coordinates.fromDict(data['northWest'])

        return cls(northEast, southEast, southWest, northWest, center)

def buildRTree(polygons):
    idx = index.Index()
    for i, poly in enumerate(polygons):
        idx.insert(i, poly.bounds)
    return idx


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

class ImageDataWriter(DataInfoWriter):
    def __init__(self, bounds : Bounds, imageFileName = None):
        self.bounds = bounds 
        self.imageFileName = imageFileName

        super().__init__() 

    def setImageFileName(self, imageFileName): 
        self.imageFileName = imageFileName

    def toJSON(self):
        return {
            'bounds': self.bounds.toJSON(),
            'imageFileName': self.imageFileName
        }
    
    @staticmethod
    def ExtractImageFileName(dataInfoFilePath):
        parentDir = os.path.abspath(os.path.join(dataInfoFilePath, os.pardir))

        with open(dataInfoFilePath, 'r') as file: 
            jData = json.load(file)

            if 'imageFileName' in jData: 
                return jData['imageFileName']
        return None
    
    @classmethod
    def fromDict(cls, data): 
        bounds = Bounds.fromDict(data['bounds'])
        imageFileName = data['imageFileName']
        return cls(bounds, imageFileName)

    def writeFileContents(self, downloadDirPath, downloadedFile, dataFilePath):
        fPath = os.path.join(downloadDirPath, dataFilePath)
        self.imageFileName = downloadedFile

        with open(fPath, "w") as jsonFile: 
            json.dump(self.toJSON(), jsonFile, indent=4)

    def hasDataAlreadyBeenDownloaded(self, downloadDirPath : str, dataFilePath : str) -> bool: 
        dataInfoFile = os.path.join(downloadDirPath, dataFilePath)
        if os.path.isfile(dataInfoFile): 
            mediaFilePath = ImageDataWriter.ExtractImageFileName(dataInfoFile)
            
            fullMediaFilePath = os.path.join(downloadDirPath, mediaFilePath)
            if os.path.isfile(fullMediaFilePath): 
                return True
            
        return False
    
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

    @staticmethod
    def FindOverlappingChunks(bounds, threshold=0.3):
        projectPolygons = [toProjected(toPolygon(b)) for b in bounds]
        rTreeIdx = buildRTree(projectPolygons)

        overlaps = set()

        for i, poly in enumerate(projectPolygons):
            for j in rTreeIdx.intersection(poly.bounds):
                if i >= j:
                    continue  # avoid duplicate or self comparison

                poly_j = projectPolygons[j]
                if not poly.intersects(poly_j):
                    continue

                intersection_area = poly.intersection(poly_j).area
                min_area = min(poly.area, poly_j.area)
                if min_area == 0:
                    continue

                overlap_ratio = intersection_area / min_area
                if overlap_ratio >= threshold:
                    overlaps.add((i, j, overlap_ratio))

        return sorted(overlaps, key=lambda x: -x[2])

    @staticmethod
    def GroupOverlappingChunks(overlapPairs, numChunks) -> list:
        # Build adjacency list
        graph = defaultdict(set)
        for i, j, _ in overlapPairs:
            graph[i].add(j)
            graph[j].add(i)

        # DFS to find connected components
        visited = set()
        groups = []

        def dfs(node, group):
            visited.add(node)
            group.add(node)
            for neighbor in graph[node]:
                if neighbor not in visited:
                    dfs(neighbor, group)

        for i in range(numChunks):
            if i not in visited:
                group = set()
                dfs(i, group)
                if group:
                    groups.append(group)

        return groups

    @staticmethod
    def SelectRepresentatives(groups, boundsList, criteria="min_index"):
        selected = []

        for group in groups:
            if criteria == "min_index":
                chosen = min(group)
            elif criteria == "max_area":
                chosen = max(
                    group, key=lambda idx: toProjected(toPolygon(boundsList[idx])).area
                )
            else:
                raise ValueError("Unknown criteria")
            selected.append(chosen)

        return selected

    def getDownloadRequests(
        self, usgsClient: Client, coords: World_Coordinates
    ) -> DataDownloadRequest:
        aerial_dataset = get_aerial_photography_datasets(usgsClient, coords)
        scenes = usgsClient.find_scenes(aerial_dataset, coords)

        # use same logic as before
        request = DataDownloadRequest(self.name)

        print("Processing scene bounds")
        allChunks = []
        for scene in scenes["results"]:
            allChunks.append(
                Terrain_Data(scene, HighResolutionOrthoImagery.ExtractBounds(scene))
            )
        print("Done")

        print("Processing overlaps")
        overlaps = HighResolutionOrthoImagery.FindOverlappingChunks(allChunks, 0.3)
        print("Done")

        print("Grouping overlaps")
        groups = HighResolutionOrthoImagery.GroupOverlappingChunks(
            overlaps, len(allChunks)
        )
        print("Selecting representatives for overlaps")

        # create a chunk for each record
        selected = HighResolutionOrthoImagery.SelectRepresentatives(groups, allChunks)

        # find overlaps
        print("Done")

        for i in range(len(selected)):
            bounds = HighResolutionOrthoImagery.ExtractBounds(allChunks[selected[i]].record)
            imageWriter = ImageDataWriter(bounds)
            entityID = allChunks[selected[i]].record["entityId"]
            info = DataInfo(entityID, self.name, imageWriter)
            request.addDataInfo(info)

        return request
