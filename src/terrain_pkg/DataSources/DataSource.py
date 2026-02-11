from abc import ABC, abstractmethod
from typing import List
import datetime

from terrain_pkg.usgs import Client
from terrain_pkg.common import World_Coordinates

import threading
import os
import time
import requests
import re

MAX_THREADS = 5
SEMAPHORES = threading.Semaphore(value=MAX_THREADS)
THREADS = []
FAILED_DOWNLOADS = []

class DataInfoWriter: 
    def __init__(self) -> None:
        self.url = None
        pass

    def setURL(self, url):
        self.url = url
    
    def getURLFilePath(self):
        if self.url is None:
            raise Exception("URL was never set")
        return self.url.split("https://")[1].replace('/', '_') + ".txt"
    
    @abstractmethod
    def writeFileContents(self, downloadDirPath, downloadedFileName : str): 
        pass

    @abstractmethod
    def hasDataAlreadyBeenDownloaded(self, downloadDirPath): 
        pass

class DataInfo: 
    def __init__(self, entityId, infoWriter : DataInfoWriter): 
        self.entityId = entityId
        self.infoWriter = infoWriter
        self.url = None
    
    def setURL(self, url): 
        self.url = url
        self.infoWriter.setURL(url)

    def writeDataInfoFileContents(self, downloadDirPath, downloadedFileName): 
        self.infoWriter.writeFileContents(downloadDirPath, downloadedFileName)
        
class DataDownloadRequest:
    def __init__(self, datasetName):
        self.datasetName = datasetName
        self.dataInfos = []

    def addDataInfo(self, dataInfo : DataInfo):
        self.dataInfos.append(dataInfo)
    
class DownloadAttempt:
    def __init__(self, url, dataInfo : DataInfo) -> None:
        self.dataInfo = dataInfo
        self.dataInfo.setURL(url)
        self.numAttempts = 0

# make this class virtual
class DataSource:
    def __init__(self):
        pass

    @abstractmethod
    def getDownloadRequests(
        self, usgsClient: Client, coords: World_Coordinates
    ) -> DataDownloadRequest:
        pass

    @staticmethod
    def DownloadFile(download : DownloadAttempt, path : str):
        SEMAPHORES.acquire()
        try:
            print(f"Downloading: {download.dataInfo.url}")
            response = requests.get(download.dataInfo.url)
            print(f"Response received: {download.dataInfo.url}")

            disposition = response.headers['content-disposition']
            filename = re.findall("filename=(.+)", disposition)[0].strip("\"")

            fPath = os.path.join(path, filename)
            with open(fPath, 'wb') as file: 
                file.write(response.content)
            print(f"Download Done: {filename}")

            print(f"Writing data info file: {download.dataInfo.infoWriter.getURLFilePath()}")
            download.dataInfo.infoWriter.writeFileContents(path, filename)
            print(f"Done: {download.dataInfo.infoWriter.getURLFilePath()}")

            SEMAPHORES.release()
        except Exception as ex: 
            download.numAttempts += 1
            SEMAPHORES.release()
            
            print(f"Error ocurred during download: {ex}")
            
            if download.numAttempts < 5:
                print("Will retry") 
                DataSource.QueueDownload(download, path)
            else:
                print(f"Maximum attempt for object with url: {download.dataInfo.url}")
                FAILED_DOWNLOADS.append(download)

    @staticmethod
    def HasDownloadBeenProcessed(download : DownloadAttempt, path) -> bool: 
        return download.dataInfo.infoWriter.hasDataAlreadyBeenDownloaded(path)
    
    @staticmethod
    def QueueDownload(download : DownloadAttempt, path): 
        if not DataSource.HasDownloadBeenProcessed(download, path): 
            thread = threading.Thread(target=DataSource.DownloadFile, args=(download,path, ))
            THREADS.append(thread)
            thread.start()
        else:
            print(f"Skipping already downloaded file detected for {download.dataInfo.url}")

    @staticmethod
    def WaitForDone(): 
        allThreadsDone = False
        while not allThreadsDone: 
            aliveThreadCount = 0
            for thread in THREADS: 
                if thread.is_alive(): 
                    aliveThreadCount += 1

            if aliveThreadCount != 0: 
                print(f"Download heartbeat check. Remaining downloads: {aliveThreadCount}")
                time.sleep(15)
            else:
                allThreadsDone = True

    def requestAndProcessAllDownloads(self, usgsClient : Client, downloads, requests : DataDownloadRequest) -> int: 
        requestEntityIdToDownloadRequest = {}

        for dataInfo in requests.dataInfos: 
           requestEntityIdToDownloadRequest[dataInfo.entityId] = dataInfo

        path = os.path.join(os.getcwd(), "tmpDownloads")
        if not os.path.isdir(path):
            os.mkdir(path)

        numRequestedDownload = len(downloads)
        timeLabel = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")  # Customized label using date timeLabel = 
        downloadRetrievePayload = {"downloads": downloads, "label": timeLabel}

        downloadIds = []
        print("Gathering download status")
        requestResults = usgsClient.submitRequest("download-request", downloadRetrievePayload)
        print("Done")

        # some downloads are not available right away...wait for them
        if (requestResults["preparingDownloads"] != None and len(requestResults["preparingDownloads"]) > 0):
            moreURLRequestPayload = {"label": timeLabel}
            print("Gathering downloads")
            moreDownloadUrls = usgsClient.submitRequest("download-retrieve", moreURLRequestPayload)
            
            downloadIds = []
            for download in moreDownloadUrls["available"]:
                downloadId = str(download['downloadId'])
                if downloadId in requestResults['newRecords'] or downloadId in requestResults['duplicateProducts']: 
                    downloadIds.append(downloadId)
                    mappedDataRequestRecord = requestEntityIdToDownloadRequest[download['entityId']]

                    DataSource.QueueDownload(DownloadAttempt(download['url'], mappedDataRequestRecord), path)

            for download in moreDownloadUrls["requested"]: 
                downloadId = str(download["downloadId"])
                if downloadId in requestResults['newRecords'] or downloadId in requestResults['duplicateProducts']: 
                    mappedDataRequestRecord = requestEntityIdToDownloadRequest[download['entityId']]

                    DataSource.QueueDownload(DownloadAttempt(download['url'], mappedDataRequestRecord), path)

            while len(downloadIds) < (numRequestedDownload - (len(requestResults['failed']) + len(FAILED_DOWNLOADS))): 
                preparingDownloads = numRequestedDownload - len(downloadIds) - len(requestResults['failed'])
                print("\n", preparingDownloads, "downloads are not available. Waiting for 30 seconds.\n")
                time.sleep(30)
                print("Trying again")
                moreDownloadUrls = usgsClient.submitRequest("download-retrieve", moreURLRequestPayload)
                for download in moreDownloadUrls['available']: 
                    downloadId = str(download['downloadId'])
                    if downloadId not in downloadIds and (downloadId in requestResults['newRecords'] or downloadId in requestResults['duplicateProducts']):
                        downloadIds.append(downloadId)
                        mappedDataRequestRecord = requestEntityIdToDownloadRequest[download['entityId']]

                        DataSource.QueueDownload(DownloadAttempt(download['url'], mappedDataRequestRecord), path)

        else:
            #just download all of them
            for download in requestResults['availableDownloads']: 
                mappedDataRequestRecord = requestEntityIdToDownloadRequest[download['entityId']]
                DataSource.QueueDownload(DownloadAttempt(download['url'], mappedDataRequestRecord), path)

        print("Downloading files... Please do not close the program\n")
        DataSource.WaitForDone()
        print("Download complete")
     
    def processDownloads(self, usgsClient: Client, request: DataDownloadRequest):
        entityIDs = []
        for r in request.dataInfos: 
            entityIDs.append(r.entityId)

        payload = {"datasetName": request.datasetName, "entityIds": entityIDs}
        print("Gathering available products")
        downloadOptions = usgsClient.submitRequest("download-options", payload)
        print("Done")
        downloads = []
        for product in downloadOptions:
            if product["available"] == True:
                downloads.append({"entityId": product["entityId"], "productId": product["id"]})

        if downloads:
            self.requestAndProcessAllDownloads(usgsClient, downloads, request)

    def execute(self, usgsClient: Client, coords: World_Coordinates):
        requests = self.getDownloadRequests(usgsClient, coords)

        if len(requests.dataInfos) >= 50000:
            raise Exception("Max allowed is 50k")

        print(f"Num of requests to process: {len(requests.dataInfos)}")

        self.processDownloads(usgsClient, requests)
