import argparse

from terrain_stitcher.functions import (
    main_ortho,
    main_ortho_arcgis,
    main_shape,
    main_prep_ortho,
    main_prep_elevation,
    main_prep_geo,
)


def addCreateBoundsGeneratorArgs(subparser):
    parserGenerate = subparser.add_parser("create-bounds")

    parserGenerate.add_argument("-lat", "--lat", help="center latitude degrees")
    parserGenerate.add_argument("-lon", "--lon", help="center longitude degrees")
    parserGenerate.add_argument(
        "-t", "--type", help="type of generation approach to use. Such as POINT"
    )
    parserGenerate.add_argument(
        "-vd",
        "--viewDistance",
        type=float,
        default=None,
        help="View distance in miles used to size the bounds region (default: 10)",
    )


def addDownloadOrthoArgs(subparser):
    parserGenerate = subparser.add_parser("gather-ortho")

    parserGenerate.add_argument(
        "-o", "--output", help="Directory to place gathered files"
    )
    parserGenerate.add_argument(
        "-s", "--shape", help="Shape file defining area for generation"
    )
    parserGenerate.add_argument(
        "-src",
        "--source",
        choices=["usgs", "arcgis"],
        default="usgs",
        help="Acquisition source to use (default: usgs)",
    )
    parserGenerate.add_argument(
        "-i",
        "--input",
        help="Source directory for local imports (required for arcgis)",
    )
    parserGenerate.add_argument(
        "-w",
        "--workers",
        type=int,
        default=None,
        help="Number of long-lived worker subprocesses for arcgis gather (default: os.cpu_count()).",
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
        help="arcgis only: square side length to combine (2 = 2x2 -> 1 image). "
        "1 = passthrough (default).",
    )
    parserGenerate.add_argument(
        "-f",
        "--scaleFactor",
        type=float,
        default=1.0,
        help="arcgis only: downscale each tile by this fraction during "
        "stitching (0.0 < value <= 1.0; 1.0 = no scaling).",
    )
    parserGenerate.add_argument(
        "--resume",
        action="store_true",
        default=False,
        help="arcgis only: skip groups whose stitched output already exists.",
    )
    parserGenerate.add_argument(
        "-e",
        "--elevationDataDir",
        default=None,
        help="arcgis only: directory of elevation GeoTIFFs covering the shape "
        "AOI. They are clipped and composited into one continuous GeoTIFF "
        "(elevation_merged.tif) in the output and recorded in the manifest, "
        "mirroring prep-geo. Fails if no tile intersects the AOI.",
    )
    parserGenerate.add_argument(
        "--lod",
        type=int,
        default=None,
        help="arcgis only: cache level of detail to stitch. When omitted the "
        "highest LOD with surviving tiles is used.",
    )
    parserGenerate.add_argument(
        "--padding",
        type=float,
        default=None,
        help="arcgis only: degrees of padding around the shape AOI when merging "
        "elevation GeoTIFFs (-e) into one continuous GeoTIFF (default: 0.1, ~1km "
        "at mid-latitudes). Mirrors prep-geo --padding.",
    )


def addPrepOrthoImages(subparser):
    parserGenerate = subparser.add_parser("prep-ortho")

    parserGenerate.add_argument("-o", "--output", help="Output directory")
    parserGenerate.add_argument("-i", "--input", help="Input directory")
    parserGenerate.add_argument("-f", "--scaleFactor", default=1.0, help="Scale amount")
    parserGenerate.add_argument(
        "-e",
        "--elevationDataDir",
        help="Directory containing elevation files to move into the output",
    )
    parserGenerate.add_argument("-s", "--shapeFile")


def addStitchOrthoArgs(subparser):
    parserGenerate = subparser.add_parser("stitch-ortho")

    parserGenerate.add_argument(
        "-i",
        "--input",
        required=True,
        help="prep-ortho output directory (PNGs + height_info.json)",
    )
    parserGenerate.add_argument(
        "-o",
        "--output",
        required=True,
        help="Directory for stitched output",
    )
    parserGenerate.add_argument(
        "-d",
        "--dimension",
        type=int,
        default=1,
        help="Square side length to combine (2 = 2x2 -> 1 image). "
        "1 = passthrough (default).",
    )
    parserGenerate.add_argument(
        "--skip-coverage-check",
        action="store_true",
        default=False,
        help="Skip the tile coverage validation (touching/overlap checks). "
        "By default the checks run.",
    )
    parserGenerate.add_argument(
        "-f",
        "--scaleFactor",
        type=float,
        default=1.0,
        help="Downscale each input tile by this fraction during stitching "
        "(0.0 < value <= 1.0; 1.0 = no scaling). Only downscaling is supported.",
    )
    parserGenerate.add_argument(
        "--resume",
        action="store_true",
        default=False,
        help="Skip groups whose stitched output already exists in the output "
        "directory. Outputs are written atomically (temp-then-replace), so an "
        "existing file is always a complete write from a prior run. Use this to "
        "continue an interrupted stitch instead of re-doing finished groups.",
    )

    parserGenerate.add_argument(
        "-w",
        "--workers",
        type=int,
        default=None,
        help="Number of worker processes for stitching (default: os.cpu_count). "
        "Each worker holds one group's canvas in memory, so lower this if a "
        "run exhausts memory at large canvases (e.g. scale_factor=1.0 with a "
        "big --dimension).",
    )


def addPrepGeoArgs(subparser):
    parserGenerate = subparser.add_parser("prep-geo")

    parserGenerate.add_argument(
        "-s",
        "--shape",
        required=True,
        help="Shape file (Shape.json) defining the coverage region to merge "
        "elevation GeoTIFFs for.",
    )
    parserGenerate.add_argument(
        "-i",
        "--input",
        required=True,
        help="Directory of elevation GeoTIFFs (.tif) to merge.",
    )
    parserGenerate.add_argument(
        "-o",
        "--output",
        default="elevation_merged.tif",
        help="Path for the merged continuous GeoTIFF (default: "
        "elevation_merged.tif in the current directory).",
    )
    parserGenerate.add_argument(
        "--padding",
        type=float,
        default=None,
        help="Degrees of padding added around the shape region when clipping "
        "(default: 0.1, ~1km at mid-latitudes).",
    )


def main():
    parser = argparse.ArgumentParser(
        prog="TerrainStitcher", description="Entrypoint for terrain stitcher tools"
    )

    subparser = parser.add_subparsers(dest="command")

    addCreateBoundsGeneratorArgs(subparser)
    addDownloadOrthoArgs(subparser)
    addPrepOrthoImages(subparser)
    addStitchOrthoArgs(subparser)
    addPrepGeoArgs(subparser)

    args = parser.parse_args()

    if args.command == "create-bounds":
        main_shape(args.lat, args.lon, args.type, args.viewDistance)
    elif args.command == "gather-ortho":
        if args.source == "arcgis":
            # The arcgis source folds prep-ortho + stitch-ortho into the
            # import: one pass over the cache produces the final stitched
            # output + manifest. -d/-f/--resume/-e/--lod/--padding are arcgis-only.
            from terrain_stitcher.functions.ElevationGeoPrep import DEFAULT_PADDING_DEG

            elevation_padding = (
                args.padding if args.padding is not None else DEFAULT_PADDING_DEG
            )
            main_ortho_arcgis(
                args.shape,
                args.input,
                args.output,
                dimension=args.dimension,
                scale_factor=args.scaleFactor,
                resume=args.resume,
                workers=args.workers,
                elevation_data_dir=args.elevationDataDir,
                lod=args.lod,
                elevation_padding_deg=elevation_padding,
            )
        else:
            main_ortho(args.shape, args.output, args.source, args.input, args.workers)
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
    else:
        print("Unknown command type")
