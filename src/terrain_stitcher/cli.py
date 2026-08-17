import argparse

from terrain_stitcher.functions import (
    main_process_terrain,
    main_ortho,
    main_ortho_arcgis_import,
    main_ortho_arcgis_import_from_download,
    main_shape,
    main_prep_ortho,
    main_prep_elevation,
    main_prep_geo,
    main_stitch_ortho,
    main_arcgis_downloader,
    main_elevation,
    main_split_image,
)


def addCreateBoundsGeneratorArgs(subparser):
    parserGenerate = subparser.add_parser(
        "create-bounds",
        help="Generate a Shape.json bounds file around a center point",
        description=(
            "Generate a Shape.json bounds file that every other command uses "
            "to define the area of interest (AOI). The bounds are computed as "
            "a square region centered on -lat/-lon and sized by "
            "-vd/--viewDistance (radius in miles). The file is written to "
            "Shape.json in the current working directory."
        ),
    )

    parserGenerate.add_argument(
        "-lat",
        "--lat",
        help=(
            "Center latitude in decimal degrees (must be between -90 and 90). "
            "This is the north/south center of the generated bounds region "
            "and is stored in Shape.json as the AOI center."
        ),
    )
    parserGenerate.add_argument(
        "-lon",
        "--lon",
        help=(
            "Center longitude in decimal degrees (must be between -180 and "
            "180). This is the east/west center of the generated bounds "
            "region and is stored in Shape.json as the AOI center."
        ),
    )
    parserGenerate.add_argument(
        "-t",
        "--type",
        help=(
            "Type of generation approach to use. Currently only 'POINT' is "
            "supported: it creates a square bounding box around the center "
            "point using --viewDistance as the radius."
        ),
    )
    parserGenerate.add_argument(
        "-vd",
        "--viewDistance",
        type=float,
        default=None,
        help=(
            "View distance in miles used to size the bounds region (default: "
            "10). The radius is converted to degrees (lat offset = miles / 69; "
            "lon offset is additionally divided by cos(latitude)) and the "
            "resulting square region is written to Shape.json as "
            "'view_distance'."
        ),
    )


def addDownloadOrthoArgs(subparser):
    parserGenerate = subparser.add_parser(
        "gather-ortho",
        help=(
            "Gather orthoimagery for a shape AOI (USGS download or ArcGIS "
            "cache import)"
        ),
        description=(
            "Gather orthoimagery covering the shape AOI. With -src usgs "
            "(default) the USGS Earth Explorer M2M API is queried and the "
            "matching high-resolution ortho scenes are downloaded as zips + "
            "sidecar JSONs into the output directory. With -src arcgis the "
            "command imports an existing local tile cache (see -i/--input and "
            "--from-download) and stitches it directly into merged PNGs plus a "
            "height_info.json manifest, folding the old prep-ortho + "
            "stitch-ortho stages into one pass."
        ),
    )

    parserGenerate.add_argument(
        "-o",
        "--output",
        help=(
            "Directory to place gathered files. For usgs: downloaded ortho "
            "zips + sidecar JSONs. For arcgis: stitched gathered_r*_c*.png "
            "images, a height_info.json manifest, and (with -e) "
            "elevation_merged.tif."
        ),
    )
    parserGenerate.add_argument(
        "-s",
        "--shape",
        help=(
            "Shape file (Shape.json, produced by create-bounds) defining the "
            "area of interest. For usgs it is the region queried against the "
            "USGS API; for arcgis it filters the cache tiles to those whose "
            "footprint overlaps the region."
        ),
    )
    parserGenerate.add_argument(
        "-src",
        "--source",
        choices=["usgs", "arcgis"],
        default="usgs",
        help=(
            "Acquisition source to use (default: usgs). 'usgs' downloads "
            "high-resolution ortho scenes from the USGS Earth Explorer M2M "
            "API. 'arcgis' imports tiles that already exist on disk (requires "
            "-i/--input) and stitches them in one pass."
        ),
    )
    parserGenerate.add_argument(
        "-i",
        "--input",
        help=(
            "Source directory for local imports (required for arcgis). Either "
            "a gdal2tiles XYZ output produced by `download-arcgis` (add "
            "--from-download) or an ArcGIS Pro exploded tile cache containing "
            "conf.xml + _alllayers. Ignored for the usgs source."
        ),
    )
    parserGenerate.add_argument(
        "-w",
        "--workers",
        type=int,
        default=None,
        help=(
            "Number of long-lived worker subprocesses for arcgis gather "
            "(default: os.cpu_count()). Used for tile discovery (one worker "
            "per cache row) and for stitching; each worker holds one group's "
            "canvas in memory, so lower this if a run exhausts memory. Ignored "
            "for the usgs source."
        ),
    )
    # The following options apply to the arcgis source only, which folds the
    # old prep-ortho + stitch-ortho stages into the import: the cache native
    # (row, col) grid is composited straight from the source PNGs. Ignored for
    # the usgs source.
    parserGenerate.add_argument(
        "-d",
        "--dimension",
        type=int,
        default=1,
        help=(
            "arcgis only: square side length to combine (2 = 2x2 -> 1 image). "
            "1 = passthrough (default). The cache's native (row, col) grid is "
            "partitioned into N x N windows; each window becomes one output "
            "image (gathered_r*_c*.png). Partial edge windows and windows with "
            "holes produce smaller/ragged images with absent cells left blank."
        ),
    )
    parserGenerate.add_argument(
        "-f",
        "--scaleFactor",
        type=float,
        default=1.0,
        help=(
            "arcgis only: downscale each tile by this fraction during "
            "stitching (0.0 < value <= 1.0; 1.0 = no scaling). Only "
            "downscaling is supported. Resampling is LANCZOS; for scale "
            "factors at or below 0.25, PIL's progressive reduction "
            "(reducing_gap) is used to avoid moire and speed up large "
            "downscales."
        ),
    )
    parserGenerate.add_argument(
        "--resume",
        action="store_true",
        default=False,
        help=(
            "arcgis only: skip groups whose stitched output already exists. "
            "Outputs are written atomically (temp-then-replace), so an "
            "existing file is always a complete write from a prior run. Use "
            "this to continue an interrupted stitch instead of re-doing "
            "finished groups."
        ),
    )
    parserGenerate.add_argument(
        "-e",
        "--elevationDataDir",
        default=None,
        help=(
            "arcgis only: directory of elevation GeoTIFFs covering the shape "
            "AOI. They are clipped to the shape region (plus --padding) and "
            "composited into one continuous EPSG:4326 GeoTIFF "
            "(elevation_merged.tif) in the output, recorded in "
            "height_info.json under elevation_files, mirroring prep-geo. "
            "Fails if no tile intersects the AOI."
        ),
    )
    parserGenerate.add_argument(
        "--lod_min",
        type=int,
        default=None,
        help=(
            "arcgis only: lowest cache level of detail to stitch. For an "
            "ArcGIS Pro cache import this selects the single LOD to stitch (a "
            "cache ships every LOD it was built at, so scoping to one keeps "
            "the (row, col) grid non-overlapping). For --from-download "
            "imports it is the lowest XYZ zoom level to consider and the LOD "
            "that is stitched. When omitted, the highest LOD with surviving "
            "tiles is used."
        ),
    )
    parserGenerate.add_argument(
        "--lod_max",
        type=int,
        default=None,
        help=(
            "arcgis only: highest cache level of detail to consider. Only "
            "used with --from-download imports, where it bounds the XYZ zoom "
            "range of the tile scheme. When omitted, the highest LOD with "
            "surviving tiles is used. Ignored for ArcGIS Pro cache imports."
        ),
    )
    parserGenerate.add_argument(
        "--padding",
        type=float,
        default=None,
        help=(
            "arcgis only: degrees of padding added around the shape AOI when "
            "merging elevation GeoTIFFs (-e) into one continuous GeoTIFF "
            "(default: 0.1, ~1km at mid-latitudes). Mirrors prep-geo "
            "--padding; keeps downstream consumers from seeing NoData exactly "
            "at the region edge."
        ),
    )
    parserGenerate.add_argument(
        "--from-download",
        action="store_true",
        default=False,
        help=(
            "arcgis only: import a gdal2tiles XYZ output produced by the "
            "`download-arcgis` command (point -i at it). Without this flag, "
            "-i is treated as an ArcGIS Pro exploded tile cache (conf.xml + "
            "_alllayers). XYZ tiles live at <z>/<x>/<y>.png; cache tiles at "
            "L##/R########/C########.png."
        ),
    )


def addDownloadArcgisArgs(subparser):
    parserGenerate = subparser.add_parser(
        "download-arcgis",
        help=(
            "Download orthoimagery from an ArcGIS imagery service and tile it "
            "with gdal2tiles"
        ),
        description=(
            "Download orthoimagery over the shape AOI from a registered "
            "ArcGIS imagery service (see refresh-services) and tile the "
            "resulting mosaic with gdal2tiles into a Web Mercator tile cache. "
            "The output can be imported later with `gather-ortho -src arcgis "
            "--from-download -i <output>`."
        ),
    )

    parserGenerate.add_argument(
        "-s",
        "--shape",
        required=True,
        help=(
            "Shape file (Shape.json, produced by create-bounds) defining the "
            "area to download. The AOI center + view distance determine the "
            "download bbox; the whole AOI must fit inside a single registered "
            "imagery service."
        ),
    )
    parserGenerate.add_argument(
        "-o",
        "--output",
        default="output_tiles",
        help=(
            "Directory to write the downloaded tile cache (default: "
            "output_tiles). The gdal2tiles XYZ output can be imported with "
            "`gather-ortho -src arcgis --from-download -i <this dir>`."
        ),
    )
    parserGenerate.add_argument(
        "--lod",
        type=int,
        required=True,
        help=(
            "Zoom level to download and tile at (passed to gdal2tiles as -z). "
            "Must not be finer than the selected service's native resolution: "
            "the service has no real detail below its native cell size, so a "
            "finer LOD would only bake interpolated pixels into the cache."
        ),
    )
    parserGenerate.add_argument(
        "-w",
        "--workers",
        type=int,
        default=32,
        help=(
            "Concurrent download threads (default: 32). Each thread fetches "
            "exportImage chunks from the imagery service; failed chunks are "
            "retried with backoff and re-fetched on a re-run."
        ),
    )
    parserGenerate.add_argument(
        "--chunk-px",
        type=int,
        default=256,
        help=(
            "Pixels per exportImage request chunk (default: 256). The AOI is "
            "split into a grid of square chunks of this many pixels; larger "
            "chunks mean fewer requests but larger responses."
        ),
    )
    parserGenerate.add_argument(
        "--timeout",
        type=int,
        default=30,
        help=(
            "Per-request timeout in seconds (default: 30). Transient HTTP "
            "failures (429/5xx) are retried with exponential backoff up to 5 "
            "attempts."
        ),
    )
    parserGenerate.add_argument(
        "--resampling",
        default="lanczos",
        choices=["average", "near", "bilinear", "cubic", "cubicspline", "lanczos"],
        help=(
            "gdal2tiles resampling method, passed through as -r (default: "
            "lanczos). Controls how the mosaic is resampled when producing "
            "each zoom level's tiles."
        ),
    )
    parserGenerate.add_argument(
        "--processes",
        type=int,
        default=32,
        help=(
            "Number of gdal2tiles tiling processes (default: 32). Passed to "
            "gdal2tiles as --processes; more processes tile faster on "
            "multi-core machines."
        ),
    )
    parserGenerate.add_argument(
        "--xyz",
        dest="xyz",
        action="store_true",
        default=True,
        help="Use XYZ (Google/OSM) tile numbering, y increasing southward (default).",
    )
    parserGenerate.add_argument(
        "--tms",
        dest="xyz",
        action="store_false",
        help="Use TMS tile numbering, y increasing northward (flipped).",
    )
    parserGenerate.set_defaults(xyz=True)
    parserGenerate.add_argument(
        "--service-index",
        type=int,
        default=None,
        help=(
            "When several imagery services cover the AOI, pick one by its "
            "0-based index in the printed candidate list. Only needed when "
            "more than one registered service fully covers the AOI; run "
            "without it to print the candidates and their native resolutions."
        ),
    )
    parserGenerate.add_argument(
        "--skip_mosaic",
        dest="skip_mosaic",
        action="store_true",
        default=False,
        help="Flag to set downloader to skip processing raw downloaded chunks",
    )


def addDownloadElevationArgs(subparser):
    parserGenerate = subparser.add_parser(
        "download-elevation",
        help=(
            "Download a continuous Float32 elevation GeoTIFF over the shape "
            "AOI from a registered elevation ArcGIS service (e.g. USGS 3DEP)."
        ),
        description=(
            "Download a continuous Float32 elevation GeoTIFF over the shape "
            "AOI from a registered elevation ArcGIS service (e.g. USGS 3DEP). "
            "The AOI is fetched as georeferenced F32 TIFF chunks, verified, "
            "mosaicked, and written as a single merged GeoTIFF. Unlike "
            "download-arcgis there is no gdal2tiles pass -- elevation is a "
            "continuous raster, not a tile pyramid."
        ),
    )

    parserGenerate.add_argument(
        "-s",
        "--shape",
        required=True,
        help=(
            "Shape file (Shape.json, produced by create-bounds) defining the "
            "area to download. The AOI center + view distance determine the "
            "fetch bbox; the whole AOI must fit inside a single registered "
            "elevation service."
        ),
    )
    parserGenerate.add_argument(
        "-o",
        "--output",
        default="elevation_merged.tif",
        help=(
            "Path to write the single merged elevation GeoTIFF (default: "
            "elevation_merged.tif). The output is a Float32 continuous raster "
            "in the service's SRS (Web Mercator), suitable for prep-geo / "
            "gather-ortho -e consumption."
        ),
    )
    parserGenerate.add_argument(
        "--res",
        type=float,
        default=None,
        help=(
            "Fetch resolution in m/px (default: the service's registered "
            "native pixel size). Requesting a finer resolution than native "
            "prints a warning -- 3DEP resamples on the fly, so the extra "
            "detail is interpolated, not real."
        ),
    )
    parserGenerate.add_argument(
        "--chunk-px",
        type=int,
        default=256,
        help=(
            "Pixels per exportImage request chunk (default: 256). The AOI is "
            "split into a grid of square chunks of this many pixels; larger "
            "chunks mean fewer requests but larger responses."
        ),
    )
    parserGenerate.add_argument(
        "--timeout",
        type=int,
        default=30,
        help=(
            "Per-request timeout in seconds (default: 30). Transient HTTP "
            "failures (429/5xx) are retried with exponential backoff up to 5 "
            "attempts."
        ),
    )
    parserGenerate.add_argument(
        "-w",
        "--workers",
        type=int,
        default=32,
        help=(
            "Concurrent download threads (default: 32). Each thread fetches "
            "exportImage chunks from the elevation service; failed chunks are "
            "retried with backoff and re-fetched on a re-run."
        ),
    )
    parserGenerate.add_argument(
        "--service-index",
        type=int,
        default=None,
        help=(
            "When several elevation services cover the AOI, pick one by its "
            "0-based index in the printed candidate list. Only needed when "
            "more than one registered service fully covers the AOI; run "
            "without it to print the candidates and their native resolutions."
        ),
    )
    parserGenerate.add_argument(
        "--padding",
        type=float,
        default=0.0,
        help=(
            "Degrees of padding added around the shape AOI when fetching "
            "(default: 0). Expands the AOI bbox before projecting to Web "
            "Mercator, so the merged GeoTIFF extends beyond the shape region."
        ),
    )


def addRefreshServicesArgs(subparser):
    parserGenerate = subparser.add_parser(
        "refresh-services",
        help=(
            "Fetch metadata for each endpoint in the shipped scan list "
            "(services_to_scan.json) and cache an imagery service registry "
            "(coverage + native resolution per layer)."
        ),
        description=(
            "Fetch metadata for each endpoint in the shipped scan list "
            "(services_to_scan.json) and cache an imagery service registry "
            "(coverage + native resolution per layer) in the user cache dir. "
            "download-arcgis and download-elevation use this registry to "
            "select the service covering an AOI. Run this once (or after "
            "network changes) before the download commands."
        ),
    )
    parserGenerate.add_argument(
        "--from-file",
        default=None,
        help=(
            "JSON file with an array of ImageServer endpoints to register "
            "(URL strings, or {'url': ...} objects that may also declare "
            "'pixel_size': <m/px> and 'kinds': ['imagery'|'elevation']) "
            "instead of the shipped scan list. A top-level object with a "
            "'folders' array is also accepted."
        ),
    )
    parserGenerate.add_argument(
        "--timeout",
        type=int,
        default=30,
        help=(
            "Per-request timeout in seconds (default: 30). Applied to each "
            "endpoint's metadata request (f=json)."
        ),
    )


def addPrepOrthoImages(subparser):
    parserGenerate = subparser.add_parser(
        "prep-ortho",
        help=(
            "Prepare gathered ortho zips into PNGs + height_info.json for " "stitching"
        ),
        description=(
            "Process the ortho zips gathered by `gather-ortho -src usgs` into "
            "individual PNGs plus a height_info.json manifest. Each zip's "
            "image is decoded in memory, optionally downscaled by "
            "--scaleFactor, and written as <name>.png with its sidecar JSON. "
            "Elevation files from -e are copied into the output and recorded "
            "in the manifest under elevation_files."
        ),
    )

    parserGenerate.add_argument(
        "-o",
        "--output",
        help=(
            "Output directory for the prepared images. Receives one PNG + "
            "sidecar JSON per input zip, a height_info.json manifest, any "
            "elevation files copied from -e, and a copy of the shape file. "
            "This directory is the -i input of stitch-ortho."
        ),
    )
    parserGenerate.add_argument(
        "-i",
        "--input",
        help=(
            "Input directory containing the ortho zips + sidecar JSONs "
            "produced by `gather-ortho -src usgs`."
        ),
    )
    parserGenerate.add_argument(
        "-f",
        "--scaleFactor",
        default=1.0,
        help=(
            "Scale amount: downscale factor applied to each image before "
            "saving (default: 1.0 = no scaling). Resampling is LANCZOS; "
            "values other than 1.0 reduce the output PNG dimensions by this "
            "fraction."
        ),
    )
    parserGenerate.add_argument(
        "-e",
        "--elevationDataDir",
        help=(
            "Directory containing elevation files to move into the output. "
            "Every .tif in this directory is copied into the output directory "
            "and its basename is recorded in height_info.json under "
            "'elevation_files' for downstream consumers."
        ),
    )
    parserGenerate.add_argument(
        "-s",
        "--shapeFile",
        help=(
            "Shape file (Shape.json) to copy into the output directory so "
            "downstream stages (stitch-ortho, etc.) can consume it without "
            "re-supplying the CLI argument. Required: the command fails if "
            "the file does not exist."
        ),
    )


def addStitchOrthoArgs(subparser):
    parserGenerate = subparser.add_parser(
        "stitch-ortho",
        help="Stitch prepared ortho PNGs into merged N x N group images",
        description=(
            "Stitch the prepared ortho PNGs from a prep-ortho output directory "
            "into merged group images. Tiles are placed on their geographic "
            "grid and partitioned into N x N windows (--dimension); each "
            "window is composited into one gathered_r*_c*.png. Non-tile files "
            "(elevation TIFs, Shape.json, sidecars) are passed through to the "
            "output so it stays self-contained."
        ),
    )

    parserGenerate.add_argument(
        "-i",
        "--input",
        required=True,
        help=(
            "prep-ortho output directory (PNGs + height_info.json). The "
            "manifest's per-image bounds place each PNG on the tile grid; "
            "elevation_files listed in the manifest are passed through to the "
            "output."
        ),
    )
    parserGenerate.add_argument(
        "-o",
        "--output",
        required=True,
        help=(
            "Directory for stitched output. Receives the merged "
            "gathered_r*_c*.png images, a new height_info.json manifest, and "
            "copies of every non-tile file from the input (elevation TIFs, "
            "Shape.json, sidecars)."
        ),
    )
    parserGenerate.add_argument(
        "-d",
        "--dimension",
        type=int,
        default=1,
        help=(
            "Square side length to combine (2 = 2x2 -> 1 image). "
            "1 = passthrough (default). The tile grid is partitioned into "
            "N x N windows; each window becomes one output image. Partial "
            "edge windows and windows with holes produce smaller/ragged "
            "images with absent cells left blank."
        ),
    )
    parserGenerate.add_argument(
        "--skip-coverage-check",
        action="store_true",
        default=False,
        help=(
            "Skip the tile coverage validation (touching/overlap checks). "
            "By default the checks run: gaps/misalignment warn, and any "
            "overlapping coverage aborts the stitch. Skipping can hide "
            "missing coverage until the stitched output is inspected."
        ),
    )
    parserGenerate.add_argument(
        "-f",
        "--scaleFactor",
        type=float,
        default=1.0,
        help=(
            "Downscale each input tile by this fraction during stitching "
            "(0.0 < value <= 1.0; 1.0 = no scaling). Only downscaling is "
            "supported. Resampling is LANCZOS; for scale factors at or below "
            "0.25, PIL's progressive reduction (reducing_gap) is used to "
            "avoid moire and speed up large downscales."
        ),
    )
    parserGenerate.add_argument(
        "--resume",
        action="store_true",
        default=False,
        help=(
            "Skip groups whose stitched output already exists in the output "
            "directory. Outputs are written atomically (temp-then-replace), "
            "so an existing file is always a complete write from a prior run. "
            "Use this to continue an interrupted stitch instead of re-doing "
            "finished groups."
        ),
    )

    parserGenerate.add_argument(
        "-w",
        "--workers",
        type=int,
        default=None,
        help=(
            "Number of worker processes for stitching (default: os.cpu_count). "
            "Each worker holds one group's canvas in memory, so lower this if "
            "a run exhausts memory at large canvases (e.g. scale_factor=1.0 "
            "with a big --dimension). Groups with >= 64 tiles are split into "
            "row-strips fanned across the pool; giant canvases run as one "
            "whole-group task at a time."
        ),
    )


def addPrepGeoArgs(subparser):
    parserGenerate = subparser.add_parser(
        "prep-geo",
        help="Merge elevation GeoTIFFs into one continuous raster",
        description=(
            "Merge a directory of elevation GeoTIFFs into a single continuous "
            "raster clipped to a shape region. Tiles that do not overlap the "
            "shape are ignored; the output is EPSG:4326 at the finest source "
            "resolution, LZW-compressed, with NaN NoData for float rasters."
        ),
    )

    parserGenerate.add_argument(
        "-s",
        "--shape",
        required=True,
        help=(
            "Shape file (Shape.json, produced by create-bounds) defining the "
            "coverage region to merge elevation GeoTIFFs for. Tiles that do "
            "not overlap this region are ignored; a warning is printed if the "
            "available tiles do not fully cover it."
        ),
    )
    parserGenerate.add_argument(
        "-i",
        "--input",
        required=True,
        help=(
            "Directory of elevation GeoTIFFs (.tif) to merge. Every .tif is "
            "considered as a candidate tile; only those intersecting the shape "
            "region are merged. Fails if the directory is empty or no tile "
            "intersects the region."
        ),
    )
    parserGenerate.add_argument(
        "-o",
        "--output",
        default="elevation_merged.tif",
        help=(
            "Path for the merged continuous GeoTIFF (default: "
            "elevation_merged.tif in the current directory). The output is "
            "EPSG:4326 at the finest source resolution, LZW-compressed, with "
            "NaN NoData for float rasters."
        ),
    )
    parserGenerate.add_argument(
        "--padding",
        type=float,
        default=None,
        help=(
            "Degrees of padding added around the shape region when clipping "
            "(default: 0.1, ~1km at mid-latitudes). Keeps downstream consumers "
            "from seeing NoData exactly at the region edge."
        ),
    )


def addProcessTerrainArgs(subparser):
    parserGenerate = subparser.add_parser(
        "process-terrain",
        help=(
            "Run a full download + gather pass producing 2-3 quality tiers of "
            "the same AOI in one command"
        ),
        description=(
            "Run a full terrain pass for a shape AOI: download the orthoimagery "
            "once at the highest requested LOD and gather it into one directory "
            "per quality tier. Low quality is LOD 17 and high quality is LOD 18; "
            "add --ultra to also produce an ultra-quality LOD 19 tier. Each tier "
            "is written to <output>/<name>_<lod> (e.g. perryville_17) with the "
            "same gathered_r*_c*.png + height_info.json schema gather-ortho "
            "produces, so downstream consumers need no changes. Chunking is "
            "mandatory: -d/--dimension must be >= 2 (dimension 1 would emit one "
            "file per tile -- thousands for a real AOI). With --with-elevation "
            "a continuous elevation GeoTIFF is downloaded once (download-"
            "elevation) and merged into every tier via gather-ortho -e."
        ),
    )

    parserGenerate.add_argument(
        "--name",
        required=True,
        help=(
            "Base name for the output directories. Each tier is written to "
            "<output>/<name>_<lod> (e.g. --name perryville -> perryville_17, "
            "perryville_18, perryville_19). The shared gdal2tiles pyramid is "
            "kept in <output>/<name>_tiles (deleted afterward unless "
            "--keep-tiles) and elevation in <output>/<name>_elevation."
        ),
    )
    parserGenerate.add_argument(
        "-s",
        "--shape",
        required=True,
        help=(
            "Shape file (Shape.json, produced by create-bounds) defining the "
            "area of interest. The whole AOI must fit inside a single "
            "registered imagery service (and elevation service when "
            "--with-elevation is set)."
        ),
    )
    parserGenerate.add_argument(
        "-o",
        "--output",
        default=".",
        help=(
            "Base directory under which the per-tier output directories are "
            "created (default: current directory)."
        ),
    )
    parserGenerate.add_argument(
        "-d",
        "--dimension",
        type=int,
        required=True,
        help=(
            "Square side length to combine tiles into per output image "
            "(2 = 2x2 -> 1 image). MUST be >= 2: chunking is enforced because "
            "dimension 1 would emit one file per tile (thousands for a real "
            "AOI). Mirrors gather-ortho -d."
        ),
    )
    parserGenerate.add_argument(
        "--ultra",
        action="store_true",
        default=False,
        help=(
            "Also produce an ultra-quality LOD 19 tier (<name>_19). Without "
            "this flag only LOD 17 and LOD 18 are produced. The download is "
            "run at LOD 19 when set, LOD 18 otherwise."
        ),
    )
    parserGenerate.add_argument(
        "--with-elevation",
        action="store_true",
        default=False,
        help=(
            "Download a continuous Float32 elevation GeoTIFF once "
            "(download-elevation) into <name>_elevation and merge it into "
            "every tier's output (gather-ortho -e). Off by default to match "
            "the ortho-only run.bat flow."
        ),
    )
    parserGenerate.add_argument(
        "--keep-tiles",
        action="store_true",
        default=False,
        help=(
            "Keep the intermediate <name>_tiles gdal2tiles pyramid (and any "
            "per-LOD fallback pyramids) after gathering. By default they are "
            "deleted once all tiers are gathered; keep them to resume / re-run "
            "gathers without re-downloading."
        ),
    )
    parserGenerate.add_argument(
        "--resume",
        action="store_true",
        default=False,
        help=(
            "Skip groups whose stitched output already exists in each tier "
            "(passed to gather-ortho). Use to continue an interrupted gather "
            "pass; combine with --keep-tiles so the pyramid is still present."
        ),
    )
    parserGenerate.add_argument(
        "-f",
        "--scaleFactor",
        type=float,
        default=1.0,
        help=(
            "Downscale each tile by this fraction during stitching, applied to "
            "every tier (0.0 < value <= 1.0; 1.0 = no scaling, default). Only "
            "downscaling is supported. Mirrors gather-ortho -f."
        ),
    )
    parserGenerate.add_argument(
        "-w",
        "--workers",
        type=int,
        default=32,
        help=(
            "Concurrent download threads for download-arcgis / download-"
            "elevation (default: 32)."
        ),
    )
    parserGenerate.add_argument(
        "--gather-workers",
        type=int,
        default=None,
        help=(
            "Worker processes for each tier's gather-ortho stitch (default: "
            "os.cpu_count). Each worker holds one group's canvas in memory, so "
            "lower this if a high-LOD tier with a large --dimension exhausts "
            "memory."
        ),
    )
    parserGenerate.add_argument(
        "--chunk-px",
        type=int,
        default=256,
        help=(
            "Pixels per exportImage request chunk for the downloads (default: "
            "256). Mirrors download-arcgis/download-elevation --chunk-px."
        ),
    )
    parserGenerate.add_argument(
        "--timeout",
        type=int,
        default=30,
        help=(
            "Per-request timeout in seconds for the downloads (default: 30). "
            "Transient HTTP failures are retried with backoff."
        ),
    )
    parserGenerate.add_argument(
        "--resampling",
        default="lanczos",
        choices=["average", "near", "bilinear", "cubic", "cubicspline", "lanczos"],
        help=(
            "gdal2tiles resampling method for the download (default: lanczos). "
            "Mirrors download-arcgis --resampling."
        ),
    )
    parserGenerate.add_argument(
        "--processes",
        type=int,
        default=32,
        help=(
            "Number of gdal2tiles tiling processes for the download (default: "
            "32). Mirrors download-arcgis --processes."
        ),
    )
    parserGenerate.add_argument(
        "--service-index",
        type=int,
        default=None,
        help=(
            "When several imagery/elevation services cover the AOI, pick one "
            "by its 0-based index. Forwarded to both download-arcgis and "
            "download-elevation. Only needed when more than one registered "
            "service fully covers the AOI."
        ),
    )


def addSplitImageArgs(subparser):
    parserGenerate = subparser.add_parser(
        "split-image",
        help=(
            "Split every gathered terrain image in a directory in half "
            "(no resizing) and update height_info.json"
        ),
        description=(
            "Takes a terrain output directory (produced by gather-ortho "
            "-src arcgis or process-terrain) containing gathered_r*_c*.png "
            "images plus a height_info.json manifest, splits every listed "
            "image in half along its longer pixel dimension (pure crop, no "
            "resizing), and writes the split halves plus an updated "
            "height_info.json to a new output directory. Each image is split "
            "in its own worker thread so all images are processed "
            "concurrently; the main thread collects the results and writes "
            "the single updated manifest. Non-image sibling files "
            "(elevation_merged.tif, Shape.json, sidecars) are copied to the "
            "output so it stays self-contained. The bounds for each half are "
            "computed in Web Mercator (EPSG:3857) because image pixels are "
            "linearly spaced in projected space, not in WGS84 lat/lon."
        ),
    )

    parserGenerate.add_argument(
        "-i",
        "--input",
        required=True,
        help=(
            "Terrain output directory containing gathered_r*_c*.png images "
            "and a height_info.json manifest. Every image listed in the "
            "manifest whose PNG exists on disk is split in half."
        ),
    )
    parserGenerate.add_argument(
        "-o",
        "--output",
        required=True,
        help=(
            "Directory to write the split images, the updated "
            "height_info.json, and copies of all sibling files. Created if "
            "it does not exist. The original images and manifest are left "
            "untouched."
        ),
    )
    parserGenerate.add_argument(
        "--axis",
        choices=["auto", "vertical", "horizontal"],
        default="auto",
        help=(
            "Split axis (default: auto). 'auto' splits each image along its "
            "own longer pixel dimension (a wide image splits left/right, a "
            "tall image splits top/bottom). 'vertical' always splits into "
            "left/right halves (dividing longitude). 'horizontal' always "
            "splits into top/bottom halves (dividing latitude)."
        ),
    )
    parserGenerate.add_argument(
        "-w",
        "--workers",
        type=int,
        default=None,
        help=(
            "Number of worker threads — one per image being split (default: "
            "os.cpu_count()). PNG encoding releases the GIL so halves from "
            "different images encode concurrently. The pool is capped at the "
            "number of images so idle threads are never created. Lower this "
            "if memory or disk I/O is constrained."
        ),
    )


def main():
    parser = argparse.ArgumentParser(
        prog="TerrainStitcher",
        description=(
            "Entrypoint for terrain stitcher tools. Typical flow: "
            "create-bounds -> gather-ortho (usgs) -> prep-ortho -> stitch-ortho, "
            "or create-bounds -> download-arcgis -> gather-ortho -src arcgis "
            "--from-download. Run refresh-services first to build the ArcGIS "
            "service registry used by the download commands."
        ),
    )

    subparser = parser.add_subparsers(dest="command")

    addCreateBoundsGeneratorArgs(subparser)
    addDownloadOrthoArgs(subparser)
    addDownloadArcgisArgs(subparser)
    addDownloadElevationArgs(subparser)
    addRefreshServicesArgs(subparser)
    addPrepOrthoImages(subparser)
    addStitchOrthoArgs(subparser)
    addPrepGeoArgs(subparser)
    addProcessTerrainArgs(subparser)
    addSplitImageArgs(subparser)

    args = parser.parse_args()

    if args.command == "create-bounds":
        main_shape(args.lat, args.lon, args.type, args.viewDistance)
    elif args.command == "gather-ortho":
        if args.source == "arcgis":
            # The arcgis source folds prep-ortho + stitch-ortho into the
            # import: one pass over the input tiles produces the final stitched
            # output + manifest. -d/-f/--resume/-e/--lod/--padding are arcgis-only.
            # Downloading is a separate `download-arcgis` command; this stage only
            # imports tiles that already exist on disk.

            if args.input is None:
                parser.error(
                    "gather-ortho -src arcgis requires -i/--input. Run "
                    "`download-arcgis` first to fetch tiles, then import them "
                    "with `gather-ortho -src arcgis -i <download_output> "
                    "--from-download`, or point -i at an ArcGIS Pro exploded "
                    "tile cache (conf.xml + _alllayers)."
                )

            from terrain_stitcher.functions.ElevationGeoPrep import (
                DEFAULT_PADDING_DEG,
            )

            elevation_padding = (
                args.padding if args.padding is not None else DEFAULT_PADDING_DEG
            )

            if args.from_download:
                # Import a gdal2tiles XYZ output produced by `download-arcgis`.
                main_ortho_arcgis_import_from_download(
                    shape_file=args.shape,
                    download_dir=args.input,
                    min_level=args.lod_min,
                    max_level=args.lod_max,
                    output_dir=args.output,
                    dimension=args.dimension,
                    scale_factor=args.scaleFactor,
                    resume=args.resume,
                    workers=args.workers,
                    elevation_data_dir=args.elevationDataDir,
                    elevation_padding_deg=elevation_padding,
                )
            else:
                # Import an ArcGIS Pro exploded tile cache (conf.xml + _alllayers).
                main_ortho_arcgis_import(
                    args.shape,
                    args.input,
                    args.output,
                    dimension=args.dimension,
                    scale_factor=args.scaleFactor,
                    resume=args.resume,
                    workers=args.workers,
                    elevation_data_dir=args.elevationDataDir,
                    lod=args.lod_min,
                    elevation_padding_deg=elevation_padding,
                )
        else:
            main_ortho(args.shape, args.output, args.input, args.workers)
    elif args.command == "download-arcgis":
        main_arcgis_downloader(
            shape_file=args.shape,
            lod=args.lod,
            outdir=args.output,
            xyz=args.xyz,
            resampling=args.resampling,
            timeout=args.timeout,
            num_workers=args.workers,
            chunk_px=args.chunk_px,
            processes=args.processes,
            service_index=args.service_index,
            skip_mosaic=args.skip_mosaic,
        )
    elif args.command == "download-elevation":
        main_elevation(
            shape_file=args.shape,
            outdir=args.output,
            res=args.res,
            timeout=args.timeout,
            num_workers=args.workers,
            chunk_px=args.chunk_px,
            service_index=args.service_index,
            padding=args.padding,
        )
    elif args.command == "refresh-services":
        from terrain_stitcher.arcgis.services import refresh_services

        refresh_services(timeout=args.timeout, from_file=args.from_file)
    elif args.command == "prep-ortho":
        # The elevation-prep stage moves every elevation file into the output
        # directory and returns the list of their filenames; record them in
        # height_info.json as elevation_files.
        elevation_files = main_prep_elevation(
            args.input, args.output, args.elevationDataDir, args.shapeFile
        )
        main_prep_ortho(
            args.input, args.output, float(args.scaleFactor), elevation_files
        )
    elif args.command == "stitch-ortho":
        main_stitch_ortho(
            args.input,
            args.output,
            args.dimension,
            verify_tile_coverage=not args.skip_coverage_check,
            scale_factor=args.scaleFactor,
            resume=args.resume,
            workers=args.workers,
        )
    elif args.command == "prep-geo":
        from terrain_stitcher.functions.ElevationGeoPrep import DEFAULT_PADDING_DEG

        padding = args.padding if args.padding is not None else DEFAULT_PADDING_DEG
        main_prep_geo(args.shape, args.input, args.output, padding_deg=padding)
    elif args.command == "process-terrain":
        if args.dimension < 2:
            parser.error(
                "process-terrain requires -d/--dimension >= 2 (chunking is "
                "enforced: dimension 1 would emit one file per tile, thousands "
                "for a real AOI)."
            )
        main_process_terrain(
            name=args.name,
            shape_file=args.shape,
            output=args.output,
            dimension=args.dimension,
            ultra=args.ultra,
            with_elevation=args.with_elevation,
            keep_tiles=args.keep_tiles,
            resume=args.resume,
            scale_factor=args.scaleFactor,
            workers=args.workers,
            processes=args.processes,
            gather_workers=args.gather_workers,
            chunk_px=args.chunk_px,
            timeout=args.timeout,
            resampling=args.resampling,
            service_index=args.service_index,
        )
    elif args.command == "split-image":
        main_split_image(
            input_dir=args.input,
            output_dir=args.output,
            axis=args.axis,
            workers=args.workers,
        )
    else:
        print("Unknown command type")
