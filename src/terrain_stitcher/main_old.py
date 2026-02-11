import argparse
import math

from terrain_stitcher.dataSources import HighResolutionOrthoImagery, DataSource
from terrain_stitcher.usgs import Client
from terrain_stitcher.common import World_Coordinates, Latitude, Longitude, World_Bounding_Box

class ImageChunk: 
    def __init__(self, entityID, spatialBounds): 
        self.entityID = entityID
        self.spatialBounds = spatialBounds

def main():
    parser = argparse.ArgumentParser(
        description="USGS M2M API Client — provide latitude and longitude."
    )
    parser.add_argument("--lat", type=str, required=True,
                        help="Latitude in decimal degrees (−90 to 90).")
    parser.add_argument("--lon", type=str, required=True,
                        help="Longitude in decimal degrees (−180 to 180). Alias: --lng")

    args = parser.parse_args()
    bounding_box = calculate_bounding_box(World_Coordinates(args.lat, args.lon))
    imageDataset = HighResolutionOrthoImagery("high_res_ortho")

    scraper = Scraper()
    scraper.add_parser(imageDataset)
    scraper.run(bounding_box)

if __name__ == "__main__":
    main()