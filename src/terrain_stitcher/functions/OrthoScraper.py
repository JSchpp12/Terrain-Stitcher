import os 

from terrain_stitcher.dataSources import HighResolutionOrthoImagery, Scraper
from terrain_stitcher.common import ParseArea

def main(shapeFile):
    sPath = os.path.join(os.getcwd(), shapeFile)
    if not os.path.isfile(sPath): 
        raise Exception("Shape file was not provided or does not exist")

    area = ParseArea.fromJSONFile(sPath)
    imageDataset = HighResolutionOrthoImagery("high_res_ortho")
    scraper = Scraper()
    scraper.add_parser(imageDataset)
    scraper.run(area.getTotalRegion())