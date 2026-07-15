import argparse

from terrain_stitcher.functions import (
    main_ortho,
    main_shape,
    main_prep_ortho,
    main_prep_elevation,
    main_stitch_ortho,
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


def addPrepOrthoImages(subparser):
    parserGenerate = subparser.add_parser("prep-ortho")

    parserGenerate.add_argument("-o", "--output", help="Output directory")
    parserGenerate.add_argument("-i", "--input", help="Input directory")
    parserGenerate.add_argument("-f", "--scaleFactor", default=1.0, help="Scale amount")
    parserGenerate.add_argument(
        "-e", "--elevationDataDir", help="Directory containing elevation files to move into the output"
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


def main():
    parser = argparse.ArgumentParser(
        prog="TerrainStitcher", description="Entrypoint for terrain stitcher tools"
    )

    subparser = parser.add_subparsers(dest="command")

    addCreateBoundsGeneratorArgs(subparser)
    addDownloadOrthoArgs(subparser)
    addPrepOrthoImages(subparser)
    addStitchOrthoArgs(subparser)

    args = parser.parse_args()

    if args.command == "create-bounds":
        main_shape(args.lat, args.lon, args.type, args.viewDistance)
    elif args.command == "gather-ortho":
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
        main_stitch_ortho(args.input, args.output, args.dimension)
    else:
        print("Unknown command type")

