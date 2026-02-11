import argparse

from terrain_stitcher.functions import main_ortho

def addCreateBoundsGeneratorArgs(subparser): 
    parserGenerate = subparser.add_parser("create-bounds")

    parserGenerate.add_argument("-lat", "--lat", help="center latitude degrees")
    parserGenerate.add_argument("-lon", "--lon", help="center longitude degrees")
    parserGenerate.add_argument("-t", "--type", help="type of generation approach to use. Such as POINT")

def addDownloadOrthoArgs(subparser): 
    parserGenerate = subparser.add_parser("gather-ortho")
    
    parserGenerate.add_argument("-s", "--shape", help="Shape file defining area for generation")

def main(): 
    parser = argparse.ArgumentParser(prog="TerrainStitcher", description="Entrypoint for terrain stitcher tools")

    subparser = parser.add_subparsers(dest="command")

    addCreateBoundsGeneratorArgs(subparser)
    addDownloadOrthoArgs(subparser)

    args = parser.parse_args()

    if args.command == "create-bounds": 
        pass
    elif args.command == "gather-ortho": 
        main_ortho(args)
    else:
        print("Unknown command type")
