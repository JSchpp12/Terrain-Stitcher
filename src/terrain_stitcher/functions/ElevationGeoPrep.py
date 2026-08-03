"""Merge elevation GeoTIFFs that cover a region into one continuous GeoTIFF.

This restores the old ``main_arch``/``ElevationTIFPrep`` behavior where the
elevation tiles covering the target region were clipped to a common frame and
mosaicked into a single continuous GeoTIFF used downstream by the terrain
pipeline. It is exposed as the ``prep-geo`` CLI command.

The target region (coverage) comes from a Shape.json file (the same format used
by ``create-bounds``/``prep-ortho``), and every ``.tif`` in the supplied
elevation directory is considered as a candidate tile. Tiles that do not
overlap the target are ignored; tiles that overlap are reprojected+clipped to a
shared EPSG:4326 frame and composited (first valid pixel wins) into the output.

The merge is implemented with rasterio (the project's existing raster stack)
rather than the historical ``osgeo.gdal`` code, but the coverage/grouping
helpers are a direct port of the original logic so behavior matches.
"""

import logging
import os

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.warp import reproject, transform_bounds

from terrain_stitcher.common import (
    ParseArea,
    World_Bounding_Box,
    World_Coordinates,
)
from terrain_stitcher.util import (
    find_files_with_extension,
    write_star_ignore_marker,
)

log = logging.getLogger(__name__)

# All merging happens in WGS84 geographic coordinates so the output is a single
# continuous GeoTIFF regardless of the source tiles' native CRS.
TARGET_CRS = "EPSG:4326"
# Degrees of padding added around the target bounds when clipping, mirroring
# the ~1km-at-mid-latitudes padding used by the old gdal-based flow. Keeps
# downstream consumers from seeing NoData exactly at the region edge.
DEFAULT_PADDING_DEG = 0.1


class ElevationData:
    def __init__(self, srcFilePath, bounds: World_Bounding_Box):
        self.srcFilePath = srcFilePath
        self.bounds = bounds


def extractWorldBounds(filePath) -> World_Bounding_Box:
    """Return a WGS84 lat/lon bounding box for a GeoTIFF.

    Uses rasterio.warp.transform_bounds so tiles in any CRS (NAD83, Web
    Mercator, UTM, ...) are normalized to EPSG:4326 before grouping/coverage
    checks, the same way the original osr.CoordinateTransformation did.
    """
    with rasterio.open(filePath) as ds:
        src_crs = ds.crs
        left, bottom, right, top = ds.bounds
        if src_crs is None or src_crs.to_string() == TARGET_CRS:
            min_lon, min_lat, max_lon, max_lat = left, bottom, right, top
        else:
            min_lon, min_lat, max_lon, max_lat = transform_bounds(
                src_crs, TARGET_CRS, left, bottom, right, top, densify_pts=21
            )

    return World_Bounding_Box(
        World_Coordinates(lat=str(min_lat), lon=str(min_lon)),
        World_Coordinates(lat=str(max_lat), lon=str(max_lon)),
    )


def buildElevationDataFromFile(filePath) -> ElevationData:
    return ElevationData(filePath, extractWorldBounds(filePath))


def gatherAllElevationFiles(elevationDataDir: os.PathLike) -> list:
    return find_files_with_extension(elevationDataDir, ".tif")


def processAllElevationFiles(elevationDataDir: os.PathLike) -> list:
    files = gatherAllElevationFiles(elevationDataDir)
    return [buildElevationDataFromFile(p) for p in files]


def findContinuousRegions(boxes: list) -> list:
    """Group tiles whose WGS84 boxes overlap or touch into continuous regions.

    Direct port of the original union-find grouping. ``boxes`` is a list of
    ElevationData; each group returned is the list of ElevationData in that
    connected component.
    """
    if not boxes:
        return []

    def boxesOverlapOrTouch(a: World_Bounding_Box, b: World_Bounding_Box) -> bool:
        a_min_lon = a.get_lower_left().get_lon()
        a_max_lon = a.get_upper_right().get_lon()
        a_min_lat = a.get_lower_left().get_lat()
        a_max_lat = a.get_upper_right().get_lat()

        b_min_lon = b.get_lower_left().get_lon()
        b_max_lon = b.get_upper_right().get_lon()
        b_min_lat = b.get_lower_left().get_lat()
        b_max_lat = b.get_upper_right().get_lat()

        lon_gap = a_min_lon > b_max_lon or b_min_lon > a_max_lon
        lat_gap = a_min_lat > b_max_lat or b_min_lat > a_max_lat
        return not lon_gap and not lat_gap

    parent = list(range(len(boxes)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            if boxesOverlapOrTouch(boxes[i].bounds, boxes[j].bounds):
                union(i, j)

    groups = {}
    for i, box in enumerate(boxes):
        groups.setdefault(find(i), []).append(box)
    return list(groups.values())


def lonIntervalsCover(boxes, target_min_lon: float, target_max_lon: float) -> bool:
    """True if the lon intervals of ``boxes`` cover [min, max] with no gaps."""
    intervals = []
    for ed in boxes:
        lo = max(ed.bounds.get_lower_left().get_lon(), target_min_lon)
        hi = min(ed.bounds.get_upper_right().get_lon(), target_max_lon)
        if lo < hi:
            intervals.append((lo, hi))

    if not intervals:
        return False

    intervals.sort()
    covered_up_to = target_min_lon
    for lo, hi in intervals:
        if lo > covered_up_to:
            return False
        covered_up_to = max(covered_up_to, hi)
    return covered_up_to >= target_max_lon


def isFullyCovered(target: World_Bounding_Box, region: list) -> bool:
    """Sweep-line check that ``region`` fully covers ``target``.

    Port of the original implementation: slice the target along all unique
    latitude boundaries found in the region and require full longitude
    coverage in every strip.
    """
    t_min_lat = target.get_lower_left().get_lat()
    t_max_lat = target.get_upper_right().get_lat()
    t_min_lon = target.get_lower_left().get_lon()
    t_max_lon = target.get_upper_right().get_lon()

    lat_breaks = {t_min_lat, t_max_lat}
    for ed in region:
        for lat in (
            ed.bounds.get_lower_left().get_lat(),
            ed.bounds.get_upper_right().get_lat(),
        ):
            if t_min_lat < lat < t_max_lat:
                lat_breaks.add(lat)

    sorted_lats = sorted(lat_breaks)
    for i in range(len(sorted_lats) - 1):
        strip_min_lat = sorted_lats[i]
        strip_max_lat = sorted_lats[i + 1]
        strip_mid_lat = (strip_min_lat + strip_max_lat) / 2

        covering_boxes = [
            ed
            for ed in region
            if ed.bounds.get_lower_left().get_lat() <= strip_mid_lat
            and ed.bounds.get_upper_right().get_lat() >= strip_mid_lat
        ]
        if not lonIntervalsCover(covering_boxes, t_min_lon, t_max_lon):
            return False
    return True


def findIntersectingFiles(elevationData: list, target: World_Bounding_Box) -> list:
    """Every ElevationData whose box has strict (non-zero-area) overlap with target.

    Edge-only touchers are excluded because they contain no pixels inside the
    target, matching the original behavior.
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

        lon_overlap = e_min_lon < t_max_lon and e_max_lon > t_min_lon
        lat_overlap = e_min_lat < t_max_lat and e_max_lat > t_min_lat
        if lon_overlap and lat_overlap:
            intersecting.append(ed)
    return intersecting


def _resolve_nodata(dtype) -> float:
    """Pick a NoData sentinel compatible with the output dtype.

    Floating-point elevation rasters use NaN so the composite can be masked
    with isnan; integer rasters use the type min as a sentinel.
    """
    if np.issubdtype(dtype, np.floating):
        return float("nan")
    return np.iinfo(dtype).min


def _padded_bounds(target: World_Bounding_Box, padding_deg: float):
    west = target.get_lower_left().get_lon() - padding_deg
    south = target.get_lower_left().get_lat() - padding_deg
    east = target.get_upper_right().get_lon() + padding_deg
    north = target.get_upper_right().get_lat() + padding_deg
    return west, south, east, north


def _target_resolution(intersecting: list, target: World_Bounding_Box):
    """Finest source pixel size, expressed in EPSG:4326 degrees.

    For geographic sources the native resolution is already in degrees. For
    projected sources we use rasterio's calculate_default_transform to derive
    the equivalent degree pixel size at the source's location. Taking the min
    (finest) across sources preserves the most detail in the merged output.
    """
    res_x = res_y = None
    t_min_lon = target.get_lower_left().get_lon()
    t_max_lon = target.get_upper_right().get_lon()
    t_min_lat = target.get_lower_left().get_lat()
    t_max_lat = target.get_upper_right().get_lat()

    for ed in intersecting:
        with rasterio.open(ed.srcFilePath) as ds:
            src_res = ds.res  # (x, y) in source CRS units
            if ds.crs is None or ds.crs.to_string() == TARGET_CRS:
                px, py = abs(src_res[0]), abs(src_res[1])
            elif ds.crs.is_geographic:
                # Already degrees, possibly a different geographic datum (NAD83
                # vs WGS84); the degree spacing is directly usable.
                px, py = abs(src_res[0]), abs(src_res[1])
            else:
                # Projected: derive the degree-equivalent pixel size by
                # reprojecting this tile's bounds into EPSG:4326 and reading
                # the default output transform's pixel spacing.
                from rasterio.warp import calculate_default_transform

                dst_transform, _, _ = calculate_default_transform(
                    ds.crs,
                    TARGET_CRS,
                    ds.width,
                    ds.height,
                    *ds.bounds,
                )
                px = abs(dst_transform.a)
                py = abs(dst_transform.e)
        if res_x is None or px < res_x:
            res_x = px
        if res_y is None or py < res_y:
            res_y = py

    # Guard against a zero/empty resolution from malformed tiles.
    if not res_x or not res_y or res_x <= 0 or res_y <= 0:
        res_x = res_y = 1e-4
    return res_x, res_y


def mosaicElevationFiles(
    intersecting: list,
    outputPath: str,
    target: World_Bounding_Box,
    padding_deg: float = DEFAULT_PADDING_DEG,
) -> str:
    """Reproject+clip every intersecting tile into a shared frame and composite.

    All tiles are reprojected to EPSG:4326 over the padded target bounds at the
    finest source resolution, then composited (first valid pixel wins) into a
    single output GeoTIFF. This is the rasterio equivalent of the old
    gdal.BuildVRT + gdal.Translate mosaic.
    """
    if not intersecting:
        raise ValueError("No elevation files intersect the target region")

    west, south, east, north = _padded_bounds(target, padding_deg)
    res_x, res_y = _target_resolution(intersecting, target)

    width = int(round((east - west) / res_x))
    height = int(round((north - south) / res_y))
    if width <= 0 or height <= 0:
        raise ValueError(
            f"Computed output grid is empty ({width}x{height}); check target "
            f"bounds and padding."
        )

    dst_transform = from_origin(west, north, res_x, res_y)

    # Use the first source as the dtype/band-count reference. Elevation tiles
    # are single-band; mismatched band counts are rejected to avoid silently
    # dropping bands.
    with rasterio.open(intersecting[0].srcFilePath) as ref:
        count = ref.count
        dtype = ref.dtypes[0]
    for ed in intersecting:
        with rasterio.open(ed.srcFilePath) as ds:
            if ds.count != count or ds.dtypes[0] != dtype:
                raise ValueError(
                    f"Elevation tile band/dtype mismatch: {ed.srcFilePath} "
                    f"has {ds.count}x{ds.dtypes[0]}, expected {count}x{dtype}"
                )

    nodata = _resolve_nodata(np.dtype(dtype))
    out = np.full((count, height, width), nodata, dtype=dtype)

    for ed in intersecting:
        with rasterio.open(ed.srcFilePath) as src:
            tmp = np.full((count, height, width), nodata, dtype=dtype)
            for b in range(count):
                reproject(
                    source=rasterio.band(src, b + 1),
                    destination=tmp[b],
                    src_transform=src.transform,
                    src_crs=src.crs,
                    src_nodata=src.nodata,
                    dst_transform=dst_transform,
                    dst_crs=TARGET_CRS,
                    dst_nodata=nodata,
                    resampling=Resampling.bilinear,
                )
            # First valid pixel wins: only fill where the accumulator is still
            # NoData. NaN-safe for float, sentinel-safe for int.
            if np.issubdtype(np.dtype(dtype), np.floating):
                valid = ~np.isnan(tmp)
                unfilled = np.isnan(out)
            else:
                valid = tmp != nodata
                unfilled = out == nodata
            write = valid & unfilled
            out = np.where(write, tmp, out)

    profile = {
        "driver": "GTiff",
        "height": height,
        "width": width,
        "count": count,
        "dtype": dtype,
        "crs": TARGET_CRS,
        "transform": dst_transform,
        "tiled": True,
        "compress": "lzw",
    }
    # NaN is the natural NoData for floating elevation; record it so downstream
    # readers treat masked pixels correctly. Integer outputs keep the sentinel.
    if np.issubdtype(np.dtype(dtype), np.floating):
        profile["nodata"] = float("nan")

    os.makedirs(os.path.dirname(os.path.abspath(outputPath)), exist_ok=True)
    with rasterio.open(outputPath, "w", **profile) as dst:
        dst.write(out)

    # Pair the created GeoTIFF with an empty .star_ignore_<name> marker so
    # downstream directory scans can ignore the tif next to its sibling.
    write_star_ignore_marker(outputPath)
    log.info("Mosaicked %d tile(s) -> %s", len(intersecting), outputPath)
    return outputPath


def main(
    shapeFile: os.PathLike,
    elevationDataDir: os.PathLike,
    outputFile: os.PathLike,
    padding_deg: float = DEFAULT_PADDING_DEG,
) -> str:
    """Merge elevation GeoTIFFs covering ``shapeFile`` into one continuous tif.

    Returns the path to the merged output GeoTIFF.
    """
    if not os.path.isfile(shapeFile):
        raise Exception(f"Shape file is invalid or not found: {shapeFile}")
    if not os.path.isdir(elevationDataDir):
        raise Exception(f"Elevation data directory not found: {elevationDataDir}")

    targetArea = ParseArea.fromJSONFile(shapeFile).getTotalRegion()
    log.info(
        "Target region from %s: lat [%.4f, %.4f] lon [%.4f, %.4f]",
        os.path.basename(shapeFile),
        targetArea.get_lower_left().get_lat(),
        targetArea.get_upper_right().get_lat(),
        targetArea.get_lower_left().get_lon(),
        targetArea.get_upper_right().get_lon(),
    )

    elevationData = processAllElevationFiles(elevationDataDir)
    if not elevationData:
        raise Exception(f"No .tif elevation files found in {elevationDataDir}")

    intersectingData = findIntersectingFiles(elevationData, targetArea)
    if not intersectingData:
        raise Exception(
            "No elevation GeoTIFF files intersect the requested region -- "
            "nothing to merge."
        )

    # Warn (but continue) if the union of tiles does not fully cover the target;
    # the output may contain NoData near the edges. Matches the old behavior.
    coveredAreas = findContinuousRegions(intersectingData)
    if not any(isFullyCovered(targetArea, region) for region in coveredAreas):
        log.warning(
            "Available GeoTIFF files do not fully cover the requested region. "
            "The merged output may contain NoData pixels near the edges."
        )

    return mosaicElevationFiles(intersectingData, outputFile, targetArea, padding_deg)
