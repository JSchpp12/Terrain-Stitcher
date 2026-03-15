import os
import shutil

from terrain_stitcher.util import find_files_with_extension

def getTotalElevationFile(input) -> str: 
    if os.path.isfile(input):
        return input

def copyTotalElevationFile(path, outputDir) -> None: 
    if not os.path.isfile(path): 
        raise Exception("Elevation file does not exist")
    
    name = os.path.basename(path)
    fPath = os.path.join(outputDir, name)
    
    shutil.copy(path, fPath)

def main(inputDir, outputDir, elevationFile): 
    if inputDir is None or (inputDir is not None and not os.path.isdir(inputDir)): 
        raise Exception("Input dir is not defined")
    
    if not os.path.isdir(outputDir): 
        os.mkdir(outputDir)

    totalElevationFile = getTotalElevationFile(elevationFile)
    copyTotalElevationFile(totalElevationFile, outputDir)

    