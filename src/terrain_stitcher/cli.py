import argparse

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

    subparser = parser.add_argument(dest="command")

    addCreateBoundsGeneratorArgs(subparser)
    addDownloadOrthoArgs(subparser)

    if subparser.command == "create-bounds": 
        pass
    elif subparser.command == "gather-ortho": 
        pass
