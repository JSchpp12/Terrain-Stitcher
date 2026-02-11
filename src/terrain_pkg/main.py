import argparse
import math

from terrain_pkg.DataSources import HighResolutionOrthoImagery, DataSource
from terrain_pkg.usgs import Client
from terrain_pkg.common import World_Coordinates, Latitude, Longitude, World_Bounding_Box

def calculate_bounding_box(center : World_Coordinates, radius_miles : int = 10) -> World_Bounding_Box:
    # Extract latitude and longitude
    lat = center.get_lat()
    lon = center.get_lon()
    
    # Convert miles to degrees
    lat_offset = radius_miles / 69.0
    lon_offset = radius_miles / (69.0 * math.cos(math.radians(lat)))
    
    # Calculate bounding box
    min_lat = lat - lat_offset
    max_lat = lat + lat_offset
    min_lon = lon - lon_offset
    max_lon = lon + lon_offset

    return World_Bounding_Box(World_Coordinates(min_lat, min_lon), World_Coordinates(max_lat, max_lon))

class ImageChunk: 
    def __init__(self, entityID, spatialBounds): 
        self.entityID = entityID
        self.spatialBounds = spatialBounds

class Scraper:
    def __init__(self): 
        self.parsers = []

    def add_parser(self, parser : DataSource): 
        self.parsers.append(parser)

    def run(self, bounding_box : World_Coordinates):
        with Client() as usgsClient: 
            for parser in self.parsers: 
                parser.execute(usgsClient, bounding_box)

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