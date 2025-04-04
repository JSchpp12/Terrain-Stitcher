import argparse
import os
import json
import rasterio
import shutil

from zipfile import ZipFile
from PIL import Image as pImage

from constants import USGS_Helpers
from helpers import USGS_Scraper

class Coordinates:
    def __init__(self, lat : float, lon : float):
        self.lat = float(lat)
        self.lon = float(lon)

class Bounds:
    def __init__(self,coords_northEast : Coordinates, coords_southEast : Coordinates, coords_southWest : Coordinates, coords_northWest : Coordinates, coords_center : Coordinates): 
        self.coords_northEast = coords_northEast
        self.coords_southEast = coords_southEast
        self.coords_southWest = coords_southWest
        self.coords_northWest = coords_northWest
        self.coords_center = coords_center

class Terrain_Data:
    def __init__(self, chunk_name : str, orthoTexturePath : str, sectionedHeightPath : str, bounds : Bounds):
        self.chunk_name = chunk_name
        self.orthoTexturePath = orthoTexturePath
        self.sectionedHeightPath = sectionedHeightPath
        self.bounds = bounds
    
def find_file(directory, file_name):
    for ele in os.listdir(directory):
        full_ele = os.path.join(directory, ele)

        if file_name == ele:
            return os.path.join(directory, file_name)
        elif os.path.isdir(full_ele):
            deep_search = find_file(full_ele, file_name)
            if deep_search is not None:
                return deep_search
    return None


def latlon_to_pixel(lat, lon, bbox, image_size):
    """Convert lat/lon coordinates to pixel coordinates
    """
    y_min = bbox.bottom
    y_max = bbox.top
    x_min = bbox.left
    x_max = bbox.right
    
    # x_dist = abs(x_max - x_min)
    # x = int(image_size[0] * abs(lon - x_min) / (x_dist))
    # y = abs(image_size[1] - int(image_size[1] * abs(lat - y_min) / (abs(y_max - y_min))))
    # return x, y

    # x_min, y_min, x_max, y_max = bbox
    x = int(image_size[0] * abs(lon - x_min) / (abs(x_max - x_min)))
    y = abs(image_size[1] - int(image_size[1] * abs(lat - y_min) / (abs(y_max - y_min))))
    return x, y

def extract_terrain_data(compressed_file_path : str, output_dir : str, chunk_name : str) -> None: 
    if not os.path.isfile(compressed_file_path):
        raise Exception("File not found")
    
    result_path = os.path.join(output_dir, chunk_name)

    # Check if the file is a .zip file
    if compressed_file_path.endswith('.zip'):

        # Open the .zip file and extract its contents into the target tmp directory
        with ZipFile(compressed_file_path) as zf:
            zf.extractall(result_path)
    else:
        raise Exception("File type not supported")
    
    return result_path

def get_full_bounds_for_terrain(full_terrain_chunk_name : str) -> Bounds: 
    #usually have an _ inside of the file name
    shortened_chunk_name = full_terrain_chunk_name.split('_')[-1]

    #check if this is in the known usgs datasets
    USGS_project_helper = USGS_Helpers.USGS_Known_Projects()

    # project_id = USGS_project_helper.getProjectID(shortened_chunk_name)
    # if project_id is None:
    #     raise Exception("Failed to find known project for the provided file")

    modified_full_chunk_name = full_terrain_chunk_name.split('s')[0] + 'S' + full_terrain_chunk_name.split('s')[-1]
    scraped_data = USGS_Scraper.USGS_ScrapedData("5e83a2397d63a400", modified_full_chunk_name)
    
    return Bounds( 
        Coordinates(scraped_data.coords_northEast[0], scraped_data.coords_northEast[1]),
        Coordinates(scraped_data.coords_southEast[0], scraped_data.coords_southEast[1]),
        Coordinates(scraped_data.coords_southWest[0], scraped_data.coords_southWest[1]),
        Coordinates(scraped_data.coords_northWest[0], scraped_data.coords_northWest[1]),
        Coordinates(scraped_data.coords_center[0], scraped_data.coords_center[1])
    )

def cutout_terrain_area_from_main_elevation_data(full_elevation_data_path : str, result_root_dir : str, focus_terrain_bounds : Bounds, focus_terrain_name : str) -> str: 
    if not os.path.isfile(full_elevation_data_path): 
        raise Exception("Provided path to main elevation data not found")
    
    with rasterio.open(full_elevation_data_path) as full_ele_file: 
        x_min, y_min = latlon_to_pixel(focus_terrain_bounds.coords_southWest.lat, focus_terrain_bounds.coords_southWest.lon, full_ele_file.bounds, (full_ele_file.width, full_ele_file.height))
        x_max, y_max = latlon_to_pixel(focus_terrain_bounds.coords_northEast.lat, focus_terrain_bounds.coords_northEast.lon, full_ele_file.bounds, (full_ele_file.width, full_ele_file.height))

        window = rasterio.windows.Window(x_min, y_min - abs(y_max-y_min), abs(x_max - x_min), abs(y_max - y_min))

        #prepare metadata for copy
        meta = full_ele_file.meta.copy()
        meta['width'], meta['height'] = abs(x_min - x_max), abs(y_max - y_min)
        meta['transform'] = rasterio.windows.transform(window, full_ele_file.transform)
        cropped_geotiff = full_ele_file.read(window=window)

        cropped_path = os.path.join(result_root_dir, f"{focus_terrain_name}_geo.tif")

        with rasterio.open(cropped_path, 'w', **meta) as dest: 
            dest.write(cropped_geotiff)

    return cropped_path

def copy_ortho_image(result_dir : str, working_dir : str, chunk_name : str) -> str:
    shortened_chunk_name = chunk_name.split('_')[-1]

    #work through directory to find file
    src_ortho_path = find_file(working_dir, f"{shortened_chunk_name}.tif")
    dst_ortho_path = os.path.join(result_dir, f"{chunk_name}.jpg")

    im = pImage.open(src_ortho_path)
    im.thumbnail(im.size)
    im.save(dst_ortho_path, "JPEG", quality=90)

    return dst_ortho_path

def create_json_info_file(result_dir : str, chunk_infos) -> None: 
    json_data = {}
    json_data['images'] = []

    for info in chunk_infos:
        image_data = {}

        image_data["name"] = info.chunk_name
        image_data["texture_file"] = info.orthoTexturePath
        image_data["height_file"] = info.sectionedHeightPath
        image_data["corners"] = {}
        image_data["corners"]["NE"] = {}
        image_data["corners"]["NE"]["lat"] = info.bounds.coords_northEast.lat
        image_data["corners"]["NE"]["lon"] = info.bounds.coords_northEast.lon
        image_data["corners"]["SE"] = {}
        image_data["corners"]["SE"]["lat"] = info.bounds.coords_southEast.lat
        image_data["corners"]["SE"]["lon"] = info.bounds.coords_southEast.lon
        image_data["corners"]["SW"] = {}
        image_data["corners"]["SW"]["lat"] = info.bounds.coords_southWest.lat
        image_data["corners"]["SW"]["lon"] = info.bounds.coords_southWest.lon
        image_data["corners"]["NW"] = {}
        image_data["corners"]["NW"]["lat"] = info.bounds.coords_northWest.lat
        image_data["corners"]["NW"]["lon"] = info.bounds.coords_northWest.lon
        image_data["corners"]["center"] = {}
        image_data["corners"]["center"]["lat"] = info.bounds.coords_center.lat
        image_data["corners"]["center"]["lon"] = info.bounds.coords_center.lon 

        json_data['images'].append(image_data)

    json_file_path = os.path.join(result_dir, "height_info.json")

    with open(json_file_path, 'w') as file:
        json.dump(json_data, file)
    
if __name__ == "__main__":
    # Create an ArgumentParser object
    parser = argparse.ArgumentParser(description="Process terrain chunks")
    
    # Add arguments for the texture directory, elevation file, and output directory
    parser.add_argument('-textureDir', '--terrainTextureDir', type=str, required=True, help='The path to the directory containing the terrain textures')
    parser.add_argument('-elevationFile', '--terrainElevationFile', type=str, required=True, help='The path to the elevation file for terrain processing')
    parser.add_argument('-output', '--outputDir', type=str, default='result', help='The path to the output directory for the results (default: result)')
    
    # Parse the command line arguments
    args = parser.parse_args()
    
    # Access the texture directory, elevation file, and output directory from the parsed arguments
    terrain_texture_dir = os.path.join(os.getcwd(), args.terrainTextureDir)
    if not os.path.exists(terrain_texture_dir):
        raise Exception("Terrain texture directory must be provided")
    
    elevation_file = os.path.join(os.getcwd(), args.terrainElevationFile)
    if not os.path.exists(elevation_file):
        raise Exception("Terrain elevation file must be provided")

    output_dir = os.path.join(os.getcwd(), args.outputDir) 
    if not os.path.exists(output_dir):
        os.mkdir(output_dir)

    # Define the target tmp directory for decompression
    tmp_dir = os.path.join(os.getcwd(), 'tmp')
    if not os.path.exists(tmp_dir):
        os.mkdir(tmp_dir)
    
    processed_chunk_data = []
    
    for file in os.listdir(terrain_texture_dir):
        file_path = os.path.join(terrain_texture_dir, file)
        extracted_file_working_path = os.path.join(tmp_dir, file[:-4])
        chunk_name = file.split('.')[0]
        
        extracted_chunk_dir = extract_terrain_data(file_path, tmp_dir, chunk_name)

        bounds = get_full_bounds_for_terrain(chunk_name)
        terrain_cutout_path = cutout_terrain_area_from_main_elevation_data(elevation_file, output_dir, bounds, chunk_name)
        terrain_result_ortho = copy_ortho_image(output_dir, extracted_chunk_dir, chunk_name)

        #create terrain data
        
        processed_chunk_data.append(Terrain_Data(
            chunk_name,
            os.path.relpath(terrain_result_ortho, start=output_dir), 
            os.path.relpath(terrain_cutout_path, start=output_dir), 
            bounds))
    
    create_json_info_file(output_dir, processed_chunk_data)
    shutil.rmtree(tmp_dir)
