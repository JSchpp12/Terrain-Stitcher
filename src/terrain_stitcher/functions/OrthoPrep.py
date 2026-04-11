import os
import logging
import json
import concurrent.futures
import shutil

import numpy as np
import rasterio
from zipfile import ZipFile
from PIL import Image as pImage
from tqdm import tqdm

from terrain_stitcher.dataSources import ImageDataWriter
from terrain_stitcher.util import find_file
from terrain_stitcher.common import World_Bounding_Box, ParseArea

pImage.MAX_IMAGE_PIXELS = 933129999
NUM_WORKERS = 12


class ExtractData:
    def __init__(self, compressedFilePath, outputDir) -> None:
        self.compressedFilePath = compressedFilePath
        self.outputDir = outputDir


class CopyData:
    def __init__(
        self,
        extractedFileRootDir,
        outputDir,
        chunkName,
        scaleFactor,
        compressedDataInfo,
    ) -> None:
        self.extractedFileRootDir = extractedFileRootDir
        self.outputDir = outputDir
        self.chunkName = chunkName
        self.scaleFactor = scaleFactor
        self.compressedDataInfo = compressedDataInfo


def extractImageDataFile(data: ExtractData) -> str:
    if not os.path.isfile(data.compressedFilePath):
        raise Exception("File not found")

    base = os.path.basename(data.compressedFilePath)
    chunk_name, _ = os.path.splitext(base)
    result_path = os.path.join(data.outputDir, chunk_name)

    # Check if the file is a .zip file
    if data.compressedFilePath.endswith(".zip"):
        # Open the .zip file and extract its contents into the target tmp directory
        if not os.path.exists(result_path):
            with ZipFile(data.compressedFilePath) as zf:
                zf.extractall(result_path)
    else:
        raise Exception("File type not supported")

    return result_path


def extractAll(allTerrainFiles, tmpDir):
    extractDatas = []
    for file in allTerrainFiles:
        extractDatas.append(ExtractData(file, tmpDir))

    with concurrent.futures.ThreadPoolExecutor(max_workers=NUM_WORKERS) as executor:
        results = list(
            tqdm(
                executor.map(extractImageDataFile, extractDatas),
                total=len(extractDatas),
                desc="Extracting",
            )
        )

    return results


def gatherTerrainInfoFromFiles(inputDir, targetArea: World_Bounding_Box):
    imageFileNameToData = {}

    upperRight = targetArea.get_upper_right()
    lowerLeft = targetArea.get_lower_left()

    for file in os.listdir(inputDir):
        if file.lower().endswith((".json", ".txt")):
            fPath = os.path.join(inputDir, file)
            with open(fPath) as f:
                jData = json.load(f)
                imageInfo = ImageDataWriter.fromDict(jData)

                chunkLL = imageInfo.bounds.coords_southWest
                chunkUR = imageInfo.bounds.coords_northEast

                # Include if the chunk's box intersects the target area at all
                lat_overlap = (
                    chunkLL.get_lat() < upperRight.get_lat()
                    and chunkUR.get_lat() > lowerLeft.get_lat()
                )
                lon_overlap = (
                    chunkLL.get_lon() < upperRight.get_lon()
                    and chunkUR.get_lon() > lowerLeft.get_lon()
                )

                if lat_overlap and lon_overlap:
                    name = imageInfo.imageFileName.replace(".zip", "")
                    imageFileNameToData[name] = imageInfo

    return imageFileNameToData


def gatherCompressedFiles(inputDir, selectedChunksMap) -> list:
    cFiles = []
    for file in os.listdir(inputDir):
        if ".zip" in file:
            name = file.split(".")[0]
            if name in selectedChunksMap:
                fPath = os.path.join(inputDir, file)
                cFiles.append(fPath)

    return cFiles


def compareExtension(element: os.PathLike, key: str) -> bool:
    root, extension = os.path.splitext(element)
    return extension is not None and extension == key


def copyOrthoImage(copyData: CopyData) -> str:
    # work through directory to find file
    src_ortho_path = find_file(copyData.extractedFileRootDir, ".tif", compareExtension)
    dst_ortho_path = os.path.join(copyData.outputDir, f"{copyData.chunkName}.png")

    if src_ortho_path is None:
        raise Exception("Unable to find target source file")

    im = None
    try:
        with rasterio.Env(GTIFF_SRS_SOURCE="EPSG"):
            with rasterio.open(src_ortho_path) as src:
                # Read all bands and transpose from (bands, H, W) to (H, W, bands)
                img_array = np.transpose(src.read(), (1, 2, 0))

        # Normalize to uint8 if needed (e.g. 16-bit imagery)
        if img_array.dtype != np.uint8:
            img_array = img_array.astype(np.float32)
            img_min, img_max = img_array.min(), img_array.max()
            if img_max > img_min:
                img_array = (img_array - img_min) / (img_max - img_min) * 255
            img_array = img_array.astype(np.uint8)

        # Squeeze single-band arrays so PIL picks the right mode
        if img_array.shape[2] == 1:
            img_array = img_array[:, :, 0]

        im = pImage.fromarray(img_array)
    except Exception as e:
        logging.exception(
            f"Failed to open provided file: {src_ortho_path}. With the following exception: {e}"
        )
        return

    if copyData.scaleFactor != 1.0:
        new_width = im.width * copyData.scaleFactor
        new_height = im.height * copyData.scaleFactor
        im = im.resize((int(new_width), int(new_height)), resample=pImage.LANCZOS)

    im.save(dst_ortho_path)

    # also copy the data file too in case its needed later
    infoFileName = copyData.chunkName + ".json"
    finalInfoPath = os.path.join(copyData.outputDir, infoFileName)
    with open(finalInfoPath, "w") as fJson:
        json.dump(copyData.compressedDataInfo.toJSON(), fJson)

    return dst_ortho_path


def copyAllOrthoImages(
    extractedImageRootDirPaths, outputDir, nameToImageWriteData, scaleFactor
):
    copyDatas = []
    for path in extractedImageRootDirPaths:
        file = os.path.basename(path)

        copyDatas.append(
            CopyData(path, outputDir, file, scaleFactor, nameToImageWriteData[file])
        )

    with concurrent.futures.ProcessPoolExecutor(max_workers=NUM_WORKERS) as executor:
        futures = {
            executor.submit(copyOrthoImage, cd): cd.chunkName for cd in copyDatas
        }
        results = []
        with tqdm(total=len(futures), desc="Processing images", unit="img") as pbar:
            for future in concurrent.futures.as_completed(futures):
                results.append(future.result())
                pbar.update(1)

    return results


def createInfoFile(infoFilePath, chunkInfos, imageFileNameToImageInfo):
    data = {}

    data["images"] = []
    data["full_terrain_file"] = "elevation_merged.tif"
    for info in chunkInfos:
        infoName = str(os.path.basename(info)).removesuffix(".png")
        imageInfo = imageFileNameToImageInfo[infoName]

        imageData = {"name": infoName, "bounds": imageInfo.bounds.toJSON()}
        data["images"].append(imageData)

    with open(infoFilePath, "w") as file:
        json.dump(data, file)


def main(inputDir, outputDir, scaleFactor, shapeFile: os.PathLike):
    if not os.path.isdir(inputDir):
        raise Exception("Input directory does not exist")

    if not os.path.isdir(outputDir):
        os.mkdir(outputDir)

    tmpDir = os.path.join(os.getcwd(), "tmp")
    if not os.path.isdir(tmpDir):
        os.mkdir(tmpDir)

    targetArea: World_Bounding_Box = ParseArea.fromJSONFile(shapeFile).getTotalRegion()
    imageFileNameToImageInfo = gatherTerrainInfoFromFiles(inputDir, targetArea)
    print(f"Total number of ortho images: {len(imageFileNameToImageInfo)}")

    # extract ortho files
    compressedFiles = gatherCompressedFiles(inputDir, imageFileNameToImageInfo)
    print("Extracting compressed archives...")
    extractedPaths = extractAll(compressedFiles, tmpDir)
    print("Done")

    # copy orthoimage files
    print("Processing image files...")
    copyFiles = copyAllOrthoImages(
        extractedPaths, outputDir, imageFileNameToImageInfo, scaleFactor
    )
    print("Done")

    print("Finalizing dataset info...")
    # prepare data for starlight application
    infoFile = os.path.join(outputDir, "height_info.json")
    createInfoFile(infoFile, copyFiles, imageFileNameToImageInfo)

    print(f"Deleting tmp dir: {tmpDir}")
    shutil.rmtree(tmpDir)
