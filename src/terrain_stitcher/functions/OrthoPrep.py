import os
import json
import concurrent.futures
import shutil

from zipfile import ZipFile
from PIL import Image as pImage

from terrain_stitcher.dataSources import ImageDataWriter
from terrain_stitcher.util import find_file

NUM_WORKERS = 12

class ExtractData:
    def __init__(self, compressedFilePath, outputDir) -> None:
        self.compressedFilePath = compressedFilePath
        self.outputDir = outputDir

class CopyData: 
    def __init__(self, extractedFileRootDir, outputDir, chunkName, scaleFactor, compressedDataInfo) -> None: 
        self.extractedFileRootDir = extractedFileRootDir
        self.outputDir = outputDir
        self.chunkName = chunkName
        self.scaleFactor = scaleFactor
        self.compressedDataInfo = compressedDataInfo

def extractImageDataFile(data : ExtractData) -> str: 
    if not os.path.isfile(data.compressedFilePath):
        raise Exception("File not found")
    
    base = os.path.basename(data.compressedFilePath)
    chunk_name, _ = os.path.splitext(base)
    result_path = os.path.join(data.outputDir, chunk_name)

    # Check if the file is a .zip file
    if data.compressedFilePath.endswith('.zip'):
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
        results = list(executor.map(extractImageDataFile, extractDatas))

    return results

def gatherTerrainInfoFromFiles(inputDir): 
    imageFileNameToData = {}
    for file in os.listdir(inputDir): 
        if file.lower().endswith((".json", ".txt")):
            name = str(file).removesuffix(".txt").split('-')[1].lower()
            fPath = os.path.join(inputDir, file)
            with open(fPath) as f: 
                jData = json.load(f)
                imageInfo = ImageDataWriter.fromDict(jData)

                imageFileNameToData[name] = imageInfo

    return imageFileNameToData

def gatherCompressedFiles(inputDir) -> list: 
    cFiles = []
    for file in os.listdir(inputDir): 
        if ".zip" in file: 
            fPath = os.path.join(inputDir, file)
            cFiles.append(fPath)

    return cFiles

def copyOrthoImage(copyData : CopyData) -> str:
    shortened_chunk_name = copyData.chunkName.split('_')[-1]

    #work through directory to find file
    src_ortho_path = find_file(copyData.extractedFileRootDir, f"{shortened_chunk_name}.tif")
    dst_ortho_path = os.path.join(copyData.outputDir, f"{copyData.chunkName}.png")

    im = pImage.open(src_ortho_path)

    if copyData.scaleFactor != 1.0:
        new_width = im.width * copyData.scaleFactor
        new_height = im.height * copyData.scaleFactor
        im = im.resize((int(new_width), int(new_height)), resample=pImage.LANCZOS)

    im.save(dst_ortho_path)

    #also copy the data file too in case its needed later
    infoFileName = copyData.chunkName + ".json"
    finalInfoPath = os.path.join(copyData.outputDir, infoFileName)
    with open(finalInfoPath, 'w') as fJson: 
        json.dump(copyData.compressedDataInfo.toJSON(), fJson)

    return dst_ortho_path

def copyAllOrthoImages(extractedImageRootDirPaths, outputDir, nameToImageWriteData, scaleFactor=1.0): 
    copyDatas = []
    for path in extractedImageRootDirPaths:
        file = os.path.basename(path)
        chunkName = file.split('.')[0]
        copyDatas.append(CopyData(path, outputDir, chunkName, scaleFactor, nameToImageWriteData[chunkName]))

    with concurrent.futures.ThreadPoolExecutor(max_workers=NUM_WORKERS) as executor:
        results = list(executor.map(copyOrthoImage, copyDatas))

    return results

def createInfoFile(infoFilePath, chunkInfos, imageFileNameToImageInfo): 
    data = {}

    data['images'] = []
    data['full_terrain_file'] = "USGS_13_n40w083_20230911.tif"
    

    for info in chunkInfos: 
        infoName = str(os.path.basename(info)).removesuffix(".png")
        imageInfo = imageFileNameToImageInfo[infoName]

        imageData = {
            "name": info, 
            "bounds": imageInfo.bounds.toJSON()
        }
        data['images'].append(imageData)
    
    with open(infoFilePath, 'w') as file: 
        json.dump(data, file)

def main(inputDir, outputDir):
    if not os.path.isdir(inputDir): 
        raise Exception("Input directory does not exist")

    if not os.path.isdir(outputDir):
        os.mkdir(outputDir)

    tmpDir = os.path.join(os.getcwd(), "tmp")
    if not os.path.isdir(tmpDir):
        os.mkdir(tmpDir)

    imageFileNameToImageInfo = gatherTerrainInfoFromFiles(inputDir)

    #extract ortho files
    compressedFiles = gatherCompressedFiles(inputDir)
    print("Extracting compressed archives...")
    extractedPaths = extractAll(compressedFiles, tmpDir)
    print("Done")

    #copy orthoimage files
    print("Processing image files...")
    copyFiles = copyAllOrthoImages(extractedPaths, outputDir, imageFileNameToImageInfo, 0.1)
    print("Done")

    print("Finalizing dataset info...")
    #prepare data for starlight application
    infoFile = os.path.join(outputDir, "height_info.json")
    createInfoFile(infoFile, copyFiles, imageFileNameToImageInfo)
    print("Done")

    print(f"Deleting tmp dir: {tmpDir}")
    shutil.rmtree(tmpDir)
