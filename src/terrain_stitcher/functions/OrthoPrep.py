import enum
import os
import json
import concurrent.futures
from concurrent.futures import as_completed
import shutil
from dataclasses import dataclass

from tqdm import tqdm
from zipfile import ZipFile
from PIL import Image as pImage

from terrain_stitcher.sources import ImageDataWriter
from terrain_stitcher.util import find_file

NUM_WORKERS = 12


@dataclass
class ExtractInfo:
    compressedFilePath: str
    outputDir: str


@dataclass
class CopyInfo:
    extractedFileRootDir: str
    outputDir: str
    chunkName: str
    scaleFactor: float
    compressedDataInfo: ImageDataWriter


def extractImageDataFile(extractInfo: ExtractInfo) -> str:
    if not os.path.isfile(extractInfo.compressedFilePath):
        raise Exception("File not found")

    base = os.path.basename(extractInfo.compressedFilePath)
    chunk_name, _ = os.path.splitext(base)
    result_path = os.path.join(extractInfo.outputDir, chunk_name)

    # Check if the file is a .zip file
    if extractInfo.compressedFilePath.endswith(".zip"):
        # Open the .zip file and extract its contents into the target tmp directory
        if not os.path.exists(result_path):
            with ZipFile(extractInfo.compressedFilePath) as zf:
                zf.extractall(result_path)
    else:
        raise Exception("File type not supported")

    return result_path


def extractAll(allTerrainFiles, tmpDir):
    extractInfos = []
    for file in allTerrainFiles:
        extractInfos.append(ExtractInfo(file, tmpDir))

    results = []
    with tqdm(total=len(extractInfos), desc="Extracting Compressed Archives") as pbar:
        with concurrent.futures.ThreadPoolExecutor(max_workers=NUM_WORKERS) as executor:
            futures = {
                executor.submit(extractImageDataFile, info): info
                for info in extractInfos
            }
            for future, info in futures.items():
                results.append(future.result())
                pbar.update(1)

    return results


def gatherTerrainInfoFromFiles(inputDir):
    imageFileNameToData = {}
    for file in os.listdir(inputDir):
        if file.lower().endswith((".json", ".txt")):
            fPath = os.path.join(inputDir, file)
            with open(fPath) as f:
                jData = json.load(f)
                imageInfo = ImageDataWriter.fromDict(jData)

                name = imageInfo.imageFileName.replace(".zip", "")
                imageFileNameToData[name] = imageInfo

    return imageFileNameToData


def gatherCompressedFiles(inputDir) -> list:
    cFiles = []
    for file in os.listdir(inputDir):
        if ".zip" in file:
            fPath = os.path.join(inputDir, file)
            cFiles.append(fPath)

    return cFiles


# Image file extensions accepted inside an extracted ortho archive.
# The ArcGIS import path ships PNGs; the USGS path ships TIFFs.
class ImageExtensionType(enum.Enum):
    TIF = ".tif"
    TIFF = ".tiff"
    PNG = ".png"


def _extension_values(key) -> set:
    """Normalize a compare key to a set of lowercase extension strings.

    Accepts an :class:`ImageExtensionType` enum class (any of its members),
    a single enum member, a single extension string, or a collection of
    any of those.
    """
    if isinstance(key, type) and issubclass(key, enum.Enum):
        return {m.value.lower() for m in key}
    if isinstance(key, enum.Enum):
        return {key.value.lower()}
    if isinstance(key, (tuple, list, set, frozenset)):
        out = set()
        for k in key:
            out.add(k.value.lower() if isinstance(k, enum.Enum) else str(k).lower())
        return out
    return {str(key).lower()}


def compareExtension(element: os.PathLike, key) -> bool:
    """Match a file's extension against ``key``.

    ``key`` may be an :class:`ImageExtensionType` enum class (matches any of
    its members), a single enum member, a single extension string, or a
    collection of those. Comparison is case-insensitive.
    """
    _, extension = os.path.splitext(element)
    if not extension:
        return False
    return extension.lower() in _extension_values(key)


def copyOrthoImage(copyInfo: CopyInfo) -> str:
    # work through directory to find file
    src_ortho_path = find_file(
        copyInfo.extractedFileRootDir, ImageExtensionType, compareExtension
    )
    dst_ortho_path = os.path.join(copyInfo.outputDir, f"{copyInfo.chunkName}.png")

    if src_ortho_path is None:
        raise Exception("Unable to find target source file")

    im = pImage.open(src_ortho_path)

    if copyInfo.scaleFactor != 1.0:
        new_width = im.width * copyInfo.scaleFactor
        new_height = im.height * copyInfo.scaleFactor
        im = im.resize((int(new_width), int(new_height)), resample=pImage.LANCZOS)

    im.save(dst_ortho_path)

    # also copy the data file too in case its needed later
    infoFileName = copyInfo.chunkName + ".json"
    finalInfoPath = os.path.join(copyInfo.outputDir, infoFileName)
    with open(finalInfoPath, "w") as fJson:
        json.dump(copyInfo.compressedDataInfo.toJSON(), fJson)

    return dst_ortho_path


def copyAllOrthoImages(
    extractedImageRootDirPaths, outputDir, nameToImageWriteData, scaleFactor=1.0
):
    copyInfos = []
    for path in extractedImageRootDirPaths:
        file = os.path.basename(path)

        copyInfos.append(
            CopyInfo(path, outputDir, file, scaleFactor, nameToImageWriteData[file])
        )

    results = []
    with tqdm(total=len(copyInfos), desc="Copying Ortho Images") as pbar:
        with concurrent.futures.ThreadPoolExecutor(max_workers=NUM_WORKERS) as executor:
            futures = {
                executor.submit(copyOrthoImage, info): info for info in copyInfos
            }
            for future, info in futures.items():
                results.append(future.result())
                pbar.update(1)

    return results


def createInfoFile(infoFilePath, chunkInfos, imageFileNameToImageInfo,
                     full_terrain_file=None):
    data = {}

    # Reference to the full-elevation TIF copied into the output
    if full_terrain_file:
        data["full_terrain_file"] = full_terrain_file

    data["images"] = []
    for info in chunkInfos:
        infoName = str(os.path.basename(info)).removesuffix(".png")
        imageInfo = imageFileNameToImageInfo[infoName]

        imageData = {"name": infoName, "bounds": imageInfo.bounds.toJSON()}
        data["images"].append(imageData)

    with open(infoFilePath, "w") as file:
        json.dump(data, file)


def main(inputDir, outputDir, scaleFactor, full_terrain_file=None):
    if not os.path.isdir(inputDir):
        raise Exception("Input directory does not exist")

    if not os.path.isdir(outputDir):
        os.mkdir(outputDir)

    tmpDir = os.path.join(os.getcwd(), "tmp")
    if not os.path.isdir(tmpDir):
        os.mkdir(tmpDir)

    imageFileNameToImageInfo = gatherTerrainInfoFromFiles(inputDir)

    # extract ortho files
    compressedFiles = gatherCompressedFiles(inputDir)
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
    createInfoFile(infoFile, copyFiles, imageFileNameToImageInfo, full_terrain_file)

    print(f"Deleting tmp dir: {tmpDir}")
    shutil.rmtree(tmpDir)
