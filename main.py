import argparse
import os
import json
from zipfile import ZipFile

class terrain_data:
    def __init__(self, lat_north, lon_east, lat_south, lon_west, texture_path):
        self.lat_north = lat_north
        self.lon_east = lon_east
        self.lat_south = lat_south
        self.lon_west = lon_west
        self.texture_path = texture_path

    def __str__(self):
        return f"Lat North: {self.lat_north}, Lon East: {self.lon_east}, Lat South: {self.lat_south}, Lon West: {self.lon_west}, Texture Path: {self.texture_path}"
    
    def to_json(self):
        return json.dumps(self.__dict__)

def main(directory_path, tmp_dir):
    # Create the target tmp directory if it does not exist
    if not os.path.exists(tmp_dir):
        os.makedirs(tmp_dir)

    # Go through each file in the provided directory
    for filename in os.listdir(directory_path):
        # Check if the file is a .zip file
        if filename.endswith('.zip'):
            # Define the full path to the .zip file
            zipfile_path = os.path.join(directory_path, filename)

            # Open the .zip file and extract its contents into the target tmp directory
            with ZipFile(zipfile_path) as zf:
                zf.extractall(tmp_dir)

    print(f"All .zip files in {directory_path} have been decompressed to {tmp_dir}.")
    
def cleanup():
    # Delete all of the extrated file in the tmp directory after the program is done
    for filename in os.listdir(tmp_dir):
        file_path = os.path.join(tmp_dir, filename)
        try:
            if os.path.isfile(file_path) or os.path.islink(file_path):
                os.unlink(file_path)
            elif os.path.isdir(file_path):
                os.rmdir(file_path)
        except Exception as e:
            print(f'Failed to delete {file_path}. Reason: {e}')
    
if __name__ == "__main__":
    # Create an ArgumentParser object
    parser = argparse.ArgumentParser(description="Process some integers.")
    
    # Add arguments for the directory path and output directory
    parser.add_argument('-input', '--inputTerrainsDir', type=str, nargs='?', const='workingDir', help='The path to the directory containing the terrain data (default: workingDir)')
    parser.add_argument('-output', '--outputDir', type=str, default='result', help='The path to the output directory for the results (default: result)')
    
    # Parse the command line arguments
    args = parser.parse_args()
    
    # Access the directory path and output directory from the parsed arguments
    directory_path = args.inputTerrainsDir
    output_dir = args.outpoutputDirut 
    
    # Define the target tmp directory for decompression
    tmp_dir = 'tmp'

    main(directory_path, tmp_dir)
    cleanup(tmp_dir)
