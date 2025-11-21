import argparse
import math

from api_client import Client, World_Coordinates, Latitude, Longitude, World_Bounding_Box

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

def main():
    parser = argparse.ArgumentParser(
        description="USGS M2M API Client — provide latitude and longitude."
    )
    parser.add_argument("--lat", type=str, required=True,
                        help="Latitude in decimal degrees (−90 to 90).")
    parser.add_argument("--lon", type=str, required=True,
                        help="Longitude in decimal degrees (−180 to 180). Alias: --lng")

    args = parser.parse_args()

    with Client() as usgs:
        bounding_box = calculate_bounding_box(World_Coordinates(args.lat, args.lon))
        usgs.find_datasets_for(bounding_box)

if __name__ == "__main__":
    main()