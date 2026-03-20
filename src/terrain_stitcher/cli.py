import argparse
import shutil
import os

from terrain_stitcher.functions import main_ortho, main_shape, main_prep_ortho, main_prep_elevation

def addCreateBoundsGeneratorArgs(subparser): 
    parserGenerate = subparser.add_parser("create-bounds")

    parserGenerate.add_argument("-lat", "--lat", help="center latitude degrees")
    parserGenerate.add_argument("-lon", "--lon", help="center longitude degrees")
    parserGenerate.add_argument("-t", "--type", help="type of generation approach to use. Such as POINT")
    parserGenerate.add_argument("-r", "--range", type=str, help="Range in miles of coverage")

def addDownloadOrthoArgs(subparser): 
    parserGenerate = subparser.add_parser("gather-ortho")

    parserGenerate.add_argument("-o", "--output", help="Directory to place gathered files")
    parserGenerate.add_argument("-s", "--shape", help="Shape file defining area for generation")

def addPrepOrthoImages(subparser): 
    parserGenerate = subparser.add_parser("prep-ortho")

    parserGenerate.add_argument("-o", "--output", help="Output directory")
    parserGenerate.add_argument('-i', '--input', help="Input directory")
    parserGenerate.add_argument("-f", "--scaleFactor", default=1.0, help="Scale amount")
    parserGenerate.add_argument("-e", "--elevationDataDir", help="Path to full elevation file location")
    parserGenerate.add_argument("-s", "--shapeFile")

def moveShapeFile(src, outputDir): 
    fPath = os.path.join(outputDir, os.path.basename(src))
    shutil.copy2(src, fPath)

def main(): 
    parser = argparse.ArgumentParser(prog="TerrainStitcher", description="Entrypoint for terrain stitcher tools")

    subparser = parser.add_subparsers(dest="command")

    addCreateBoundsGeneratorArgs(subparser)
    addDownloadOrthoArgs(subparser)
    addPrepOrthoImages(subparser)

    args = parser.parse_args()

    if args.command == "create-bounds": 
        main_shape(args.lat, args.lon, args.type, int(args.range))
    elif args.command == "gather-ortho": 
        main_ortho(args.shape, args.output)
    elif args.command == "prep-ortho": 
        main_prep_elevation(args.input, args.output, args.elevationDataDir, args.shapeFile)
        main_prep_ortho(args.input, args.output, float(args.scaleFactor))
        moveShapeFile(args.elevationFile, args.output)
    else:
        print("Unknown command type")
