import os
import shutil

from terrain_stitcher.util import find_files_with_extension

def getTotalElevationFile(inputDir) -> str: 
    files = find_files_with_extension(inputDir, ".tif")

    if len(files) == 0: 
        raise Exception("Unable to find file")
    
    return files[0]

def copyTotalElevationFile(path, outputDir) -> None: 
    if not os.path.isfile(path): 
        raise Exception("Elevation file does not exist")
    
    name = os.path.basename(path)
    fPath = os.path.join(outputDir, name)
    
    shutil.copy(path, fPath)

def main(inputDir, outputDir): 
    if inputDir is None or (inputDir is not None and not os.path.isdir(inputDir)): 
        raise Exception("Input dir is not defined")
    
    if not os.path.isdir(outputDir): 
        os.mkdir(outputDir)

    totalElevationFile = getTotalElevationFile(inputDir)
    copyTotalElevationFile(totalElevationFile, outputDir)

    