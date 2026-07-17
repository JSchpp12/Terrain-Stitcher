import os
import shutil


def gatherAllElevationFiles(elevationDataDir: os.PathLike) -> list:
    elevationFiles = []

    for ele in os.listdir(elevationDataDir):
        root, ext = os.path.splitext(ele)
        if ext == ".tif":
            elevationFiles.append(os.path.join(elevationDataDir, ele))

    return elevationFiles


def main(
    inputDir, outputDir, elevationDataDir: os.PathLike, shapeFile: os.PathLike
) -> list:
    """Copy every elevation file into ``outputDir`` and return their basenames.

    The prep-ortho elevation stage no longer selects/combines a single TIF
    covering the shape area. Instead it copies all elevation files found in
    ``elevationDataDir`` straight into the output directory, and returns the
    list of their basenames so the prep-ortho manifest (height_info.json) can
    record them under the ``elevation_files`` key for downstream consumers.
    """
    if inputDir is None or (inputDir is not None and not os.path.isdir(inputDir)):
        raise Exception("Input dir is not defined")

    shapeFilePath = os.path.join(os.getcwd(), shapeFile)
    if (
        shapeFilePath is None
        or shapeFilePath is not None
        and not os.path.exists(shapeFilePath)
    ):
        raise Exception(f"Shape file is invalid: {shapeFilePath}")

    if not os.path.isdir(outputDir):
        os.mkdir(outputDir)

    # Pass the shape file through to the output directory so downstream
    # stages (stitch-ortho, etc.) can consume it without the original CLI
    # argument being re-supplied. Copied early, before elevation processing,
    # so it lands in the output even if a later step raises.
    shapeDest = os.path.join(outputDir, os.path.basename(shapeFilePath))
    shutil.copy2(shapeFilePath, shapeDest)

    # Copy every elevation file into the output directory instead of
    # selecting/combining a single covering TIF. The source directory is left
    # untouched so a re-run is not destructive. The downstream flow reads
    # the list of filenames from height_info.json["elevation_files"].
    elevationFiles = gatherAllElevationFiles(elevationDataDir)
    copied = []
    for src in elevationFiles:
        dst = os.path.join(outputDir, os.path.basename(src))
        shutil.copy2(src, dst)
        copied.append(os.path.basename(src))

    return copied
