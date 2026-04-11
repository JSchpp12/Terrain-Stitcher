import os
import shutil
import logging
import tempfile
import json

from osgeo import gdal, osr

from terrain_stitcher.util import find_files_with_extension
from terrain_stitcher.common import World_Bounding_Box, World_Coordinates
from terrain_stitcher.common import ParseArea

from concurrent.futures import ThreadPoolExecutor

MAX_WORKERS = 12


class ElevationData:
    def __init__(self, srcFilePath, bounds: World_Bounding_Box):
        self.srcFilePath = srcFilePath
        self.bounds = bounds


def extractWorldBounds(filePath) -> World_Bounding_Box:
    # Open the GeoTIFF
    ds = gdal.Open(filePath)
    if ds is None:
        raise FileNotFoundError(f"Could not open: {filePath}")

    # Get the geotransform
    # Format: (x_min, pixel_width, rotation_x, y_max, rotation_y, pixel_height)
    gt = ds.GetGeoTransform()

    width = ds.RasterXSize
    height = ds.RasterYSize

    # Calculate the four corner coordinates in the file's native CRS
    x_min = gt[0]
    y_max = gt[3]
    x_max = gt[0] + width * gt[1]
    y_min = gt[3] + height * gt[5]  # gt[5] is negative for north-up images

    # Set up coordinate transformation to WGS84 (EPSG:4326)
    source_crs = osr.SpatialReference()
    source_crs.ImportFromWkt(ds.GetProjection())

    target_crs = osr.SpatialReference()
    target_crs.ImportFromEPSG(4326)
    target_crs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

    transform = osr.CoordinateTransformation(source_crs, target_crs)

    # Transform all four corners to lat/lon
    corners = [
        (x_min, y_min),
        (x_min, y_max),
        (x_max, y_min),
        (x_max, y_max),
    ]
    lon_lat_corners = [transform.TransformPoint(x, y)[:2] for x, y in corners]

    lats = [pt[0] for pt in lon_lat_corners]
    lons = [pt[1] for pt in lon_lat_corners]

    return World_Bounding_Box(
        World_Coordinates(min(lats), min(lons)), World_Coordinates(max(lats), max(lons))
    )


def buildElevationDataFromFile(filePath: os.PathLike) -> ElevationData:
    return ElevationData(filePath, extractWorldBounds(filePath))


def getTotalElevationFile(input) -> str:
    if os.path.isfile(input):
        return input


def copyTotalElevationFile(path, outputDir) -> None:
    if not os.path.isfile(path):
        raise Exception("Elevation file does not exist")

    name = os.path.basename(path)
    fPath = os.path.join(outputDir, name)

    shutil.copy(path, fPath)


def gatherAllElevationFiles(elevationDataDir: os.PathLike) -> list:
    elevationFiles = []

    for ele in os.listdir(elevationDataDir):
        root, ext = os.path.splitext(ele)
        if ext == ".tif":
            elevationFiles.append(os.path.join(elevationDataDir, ele))

    return elevationFiles


def processAllEleationFiles(elevationDataDir: os.PathLike):
    allElevationFiles = gatherAllElevationFiles(elevationDataDir)

    results = None
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        results = list(executor.map(buildElevationDataFromFile, allElevationFiles))

    return results


def findContinuousRegions(boxes: list[ElevationData]) -> list[list[World_Bounding_Box]]:
    """
    Groups World_Bounding_Box objects into continuous (overlapping or touching) regions.

    Returns a list of groups, where each group is a list of boxes that form
    one continuous region.
    """
    if not boxes:
        return []

    def boxesOverlapOrTouch(a: World_Bounding_Box, b: World_Bounding_Box) -> bool:
        """Check if two bounding boxes overlap or share an edge/corner."""
        a_min_lon = a.get_lower_left().get_lon()
        a_max_lon = a.get_upper_right().get_lon()
        a_min_lat = a.get_lower_left().get_lat()
        a_max_lat = a.get_upper_right().get_lat()

        b_min_lon = b.get_lower_left().get_lon()
        b_max_lon = b.get_upper_right().get_lon()
        b_min_lat = b.get_lower_left().get_lat()
        b_max_lat = b.get_upper_right().get_lat()

        # Two boxes are connected if they overlap or share an edge/corner.
        # They are NOT connected only if there is a strict gap between them.
        lon_gap = a_min_lon > b_max_lon or b_min_lon > a_max_lon
        lat_gap = a_min_lat > b_max_lat or b_min_lat > a_max_lat

        return not lon_gap and not lat_gap

    # Union-Find (Disjoint Set Union) for grouping connected boxes
    parent = list(range(len(boxes)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]  # Path compression
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    # Compare every pair of boxes and union those that overlap or touch
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            if boxesOverlapOrTouch(boxes[i].bounds, boxes[j].bounds):
                union(i, j)

    # Group boxes by their root representative
    groups: dict[int, list[World_Bounding_Box]] = {}
    for i, box in enumerate(boxes):
        root = find(i)
        groups.setdefault(root, []).append(box)

    return list(groups.values())


def lonIntervalsCover(
    boxes: list[World_Bounding_Box], target_min_lon: float, target_max_lon: float
) -> bool:
    """
    Returns True if the longitude intervals of the given boxes
    collectively cover [target_min_lon, target_max_lon] with no gaps.
    """
    # Clip each box's lon range to the target window and collect intervals
    intervals = []
    for box in boxes:
        lo = max(box.bounds.get_lower_left().get_lon(), target_min_lon)
        hi = min(box.bounds.get_upper_right().get_lon(), target_max_lon)
        if lo < hi:
            intervals.append((lo, hi))

    if not intervals:
        return False

    intervals.sort()

    # Walk the merged intervals; if any gap exists, coverage fails
    covered_up_to = target_min_lon
    for lo, hi in intervals:
        if lo > covered_up_to:
            return False  # gap found
        covered_up_to = max(covered_up_to, hi)

    return covered_up_to >= target_max_lon


def isFullyCovered(
    target: World_Bounding_Box, region: list[World_Bounding_Box]
) -> bool:
    """
    Checks whether a target World_Bounding_Box is entirely covered
    by the union of boxes in a continuous region.

    Uses a sweep-line approach: slices the target along all unique
    latitude boundaries found in the region, then checks each horizontal
    strip is fully covered in longitude.
    """
    t_min_lat = target.get_lower_left().get_lat()
    t_max_lat = target.get_upper_right().get_lat()
    t_min_lon = target.get_lower_left().get_lon()
    t_max_lon = target.get_upper_right().get_lon()

    # Collect all unique lat boundaries from region boxes that fall
    # within the target's lat range — these are our sweep breakpoints.
    lat_breaks = set()
    lat_breaks.add(t_min_lat)
    lat_breaks.add(t_max_lat)

    for box in region:
        for lat in (
            box.bounds.get_lower_left().get_lat(),
            box.bounds.get_upper_right().get_lat(),
        ):
            if t_min_lat < lat < t_max_lat:
                lat_breaks.add(lat)

    sorted_lats = sorted(lat_breaks)

    # For each horizontal strip between consecutive lat breakpoints,
    # check that the full [t_min_lon, t_max_lon] span is covered.
    for i in range(len(sorted_lats) - 1):
        strip_min_lat = sorted_lats[i]
        strip_max_lat = sorted_lats[i + 1]
        strip_mid_lat = (strip_min_lat + strip_max_lat) / 2  # representative point

        # Gather all region boxes that cover this lat strip
        covering_boxes = [
            box
            for box in region
            if box.bounds.get_lower_left().get_lat() <= strip_mid_lat
            and box.bounds.get_upper_right().get_lat() >= strip_mid_lat
        ]

        # Merge their lon intervals and check full coverage
        if not lonIntervalsCover(covering_boxes, t_min_lon, t_max_lon):
            return False

    return True


def mergeRegionToBoundingBox(region: list[World_Bounding_Box]) -> World_Bounding_Box:
    """
    Optional utility: collapses a continuous region into a single
    minimum bounding box that covers all boxes in the region.
    """
    min_lat = min(b.get_lower_left().get_lat() for b in region)
    max_lat = max(b.get_upper_right().get_lat() for b in region)
    min_lon = min(b.get_lower_left().get_lon() for b in region)
    max_lon = max(b.get_upper_right().get_lon() for b in region)

    return World_Bounding_Box(
        World_Coordinates(lat=str(min_lat), lon=str(min_lon)),
        World_Coordinates(lat=str(max_lat), lon=str(max_lon)),
    )


def findIntersectingFiles(
    elevationData: list[ElevationData], target: World_Bounding_Box
) -> list[ElevationData]:
    """
    Returns every ElevationData whose bounding box has any overlap with
    the target region. Tiles that only touch on an edge are excluded because
    they contain no pixels inside the target.
    """
    t_min_lon = target.get_lower_left().get_lon()
    t_max_lon = target.get_upper_right().get_lon()
    t_min_lat = target.get_lower_left().get_lat()
    t_max_lat = target.get_upper_right().get_lat()

    intersecting = []
    for ed in elevationData:
        e_min_lon = ed.bounds.get_lower_left().get_lon()
        e_max_lon = ed.bounds.get_upper_right().get_lon()
        e_min_lat = ed.bounds.get_lower_left().get_lat()
        e_max_lat = ed.bounds.get_upper_right().get_lat()

        # Strict overlap: there must be a non-zero area of intersection
        lon_overlap = e_min_lon < t_max_lon and e_max_lon > t_min_lon
        lat_overlap = e_min_lat < t_max_lat and e_max_lat > t_min_lat

        if lon_overlap and lat_overlap:
            intersecting.append(ed)

    return intersecting


def clipElevationFileToRegion(
    elevationData: ElevationData,
    target: World_Bounding_Box,
    outputPath: str,
    padding_deg: float = 0.1,  # ~1km at mid-latitudes; tune to your needs
) -> str:
    min_lon = target.get_lower_left().get_lon() - padding_deg
    min_lat = target.get_lower_left().get_lat() - padding_deg
    max_lon = target.get_upper_right().get_lon() + padding_deg
    max_lat = target.get_upper_right().get_lat() + padding_deg

    warp_options = gdal.WarpOptions(
        format="GTiff",
        outputBounds=(min_lon, min_lat, max_lon, max_lat),
        outputBoundsSRS="EPSG:4326",
        dstSRS="EPSG:4326",
        # No width/height — GDAL preserves the source's native pixel spacing
        resampleAlg=gdal.GRA_Bilinear,
        creationOptions=["TILED=YES"],
    )

    ds = gdal.Warp(outputPath, elevationData.srcFilePath, options=warp_options)
    if ds is None:
        raise RuntimeError(
            f"gdal.Warp failed while clipping {elevationData.srcFilePath}"
        )
    ds = None

    logging.info(
        f"Clipped {os.path.basename(elevationData.srcFilePath)} → {outputPath}"
    )
    return outputPath


def mosaicClippedFiles(
    clippedPaths: list[str],
    outputPath: str,
    target: World_Bounding_Box,
    padding_deg: float = 0.1,  # must match the value used in clipElevationFileToRegion
) -> str:
    if not clippedPaths:
        raise ValueError("No clipped files provided for mosaicking")

    if len(clippedPaths) == 1:
        shutil.copy2(clippedPaths[0], outputPath)
        logging.info(f"Single tile — copied directly to {outputPath}")
        return outputPath

    vrt_path = outputPath.replace(".tif", "_mosaic.vrt")
    vrt_ds = gdal.BuildVRT(vrt_path, clippedPaths)
    if vrt_ds is None:
        raise RuntimeError("gdal.BuildVRT failed")
    vrt_ds.FlushCache()
    vrt_ds = None

    # Use the same padded bounds so Translate doesn't crop the padding back off
    min_lon = target.get_lower_left().get_lon() - padding_deg
    min_lat = target.get_lower_left().get_lat() - padding_deg
    max_lon = target.get_upper_right().get_lon() + padding_deg
    max_lat = target.get_upper_right().get_lat() + padding_deg

    translate_options = gdal.TranslateOptions(
        format="GTiff",
        projWin=[min_lon, max_lat, max_lon, min_lat],
        projWinSRS="EPSG:4326",
    )
    out_ds = gdal.Translate(outputPath, vrt_path, options=translate_options)
    if out_ds is None:
        raise RuntimeError(
            f"gdal.Translate failed while writing mosaic to {outputPath}"
        )
    out_ds = None

    if os.path.exists(vrt_path):
        os.remove(vrt_path)

    logging.info(f"Mosaicked {len(clippedPaths)} tiles → {outputPath}")
    return outputPath


def getBoundsFromHeightInfo(heightInfoPath: str) -> World_Bounding_Box:
    with open(heightInfoPath) as f:
        data = json.load(f)

    all_lats, all_lons = [], []
    for image in data["images"]:
        for key, corner in image["bounds"].items():
            if key == "center":
                continue
            all_lats.append(float(corner["lat"]))
            all_lons.append(float(corner["lon"]))

    return World_Bounding_Box(
        World_Coordinates(min(all_lats), min(all_lons)),
        World_Coordinates(max(all_lats), max(all_lons)),
    )


def main(inputDir, outputDir, elevationDataDir: os.PathLike, shapeFile: os.PathLike):
    if inputDir is None or (inputDir is not None and not os.path.isdir(inputDir)):
        raise Exception("Input dir is not defined")

    if not os.path.isdir(outputDir):
        os.mkdir(outputDir)

    targetArea = getBoundsFromHeightInfo(os.path.join(outputDir, "height_info.json"))

    # Load bounding-box metadata for every available GeoTIFF
    elevationData = processAllEleationFiles(elevationDataDir)

    # Find which tiles actually overlap the target region
    intersectingData = findIntersectingFiles(elevationData, targetArea)
    if not intersectingData:
        logging.warning(
            "No GeoTIFF files intersect the requested area — nothing to do."
        )
        return

    # Warn if the union of intersecting tiles does not fully cover the target
    coveredAreas = findContinuousRegions(intersectingData)
    fullyCovered = any(isFullyCovered(targetArea, region) for region in coveredAreas)
    if not fullyCovered:
        logging.warning(
            "The available GeoTIFF files do not fully cover the requested area. "
            "The output may contain NoData pixels near the edges."
        )

    # Clip each intersecting tile to the target bounding box in a temp directory
    with tempfile.TemporaryDirectory() as tmpDir:
        clippedPaths = []
        for ed in intersectingData:
            baseName = os.path.splitext(os.path.basename(ed.srcFilePath))[0]
            clippedPath = os.path.join(tmpDir, f"{baseName}_clipped.tif")
            try:
                clipElevationFileToRegion(ed, targetArea, clippedPath)
                clippedPaths.append(clippedPath)
            except RuntimeError as e:
                logging.error(f"Skipping {ed.srcFilePath}: {e}")

        if not clippedPaths:
            logging.error("All clip operations failed — no output produced.")
            return

       

        # Merge all clipped tiles into a single output GeoTIFF
        outputFilePath = os.path.join(outputDir, "elevation_merged.tif")
        mosaicClippedFiles(clippedPaths, outputFilePath, targetArea)

    logging.info(f"Done. Output written to: {outputFilePath}")
