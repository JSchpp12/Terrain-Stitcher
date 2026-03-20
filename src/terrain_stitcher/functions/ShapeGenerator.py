import os
import json

from terrain_stitcher.common import ParseArea, World_Coordinates, nameToTerrainBoundsType

def main(lat :str, lon:str, type : str, range : int): 
    area = ParseArea(nameToTerrainBoundsType(type), World_Coordinates(lat, lon), range)

    jPath = os.path.join(os.getcwd(), "Shape.json")
    areaData = area.toJSON()
    print(f"Creating file: {jPath}")
    with open(jPath, 'w') as file: 
        json.dump(areaData, file, indent=4)

    print("Done")