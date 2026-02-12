import argparse

from terrain_stitcher.functions import main_ortho, main_shape, main_prep_ortho

def addCreateBoundsGeneratorArgs(subparser): 
    parserGenerate = subparser.add_parser("create-bounds")

    parserGenerate.add_argument("-lat", "--lat", help="center latitude degrees")
    parserGenerate.add_argument("-lon", "--lon", help="center longitude degrees")
    parserGenerate.add_argument("-t", "--type", help="type of generation approach to use. Such as POINT")

def addDownloadOrthoArgs(subparser): 
    parserGenerate = subparser.add_parser("gather-ortho")
    
    parserGenerate.add_argument("-s", "--shape", help="Shape file defining area for generation")

def addPrepOrthoImages(subparser): 
    parserGenerate = subparser.add_parser("prep-ortho")

    parserGenerate.add_argument("-o", "--output", help="Output directory")
    parserGenerate.add_argument('-i', '--input', help="Input directory")

def main(): 
    parser = argparse.ArgumentParser(prog="TerrainStitcher", description="Entrypoint for terrain stitcher tools")

    subparser = parser.add_subparsers(dest="command")

    addCreateBoundsGeneratorArgs(subparser)
    addDownloadOrthoArgs(subparser)
    addPrepOrthoImages(subparser)

    args = parser.parse_args()

    if args.command == "create-bounds": 
        main_shape(args.lat, args.lon, args.type)
    elif args.command == "gather-ortho": 
        main_ortho(args.shape)
    elif args.command == "prep-ortho": 
        main_prep_ortho(args.input, args.output)
    else:
        print("Unknown command type")
