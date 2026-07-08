import math
import json

from .TerrainArea import World_Bounding_Box, World_Coordinates

from enum import Enum

#enum definition of the type of center used to calculate bounds of terrains 
class TerrainBoundsCalculateType(Enum): 
    POINT = "1"

def terrainBoundsTypeToString(type : TerrainBoundsCalculateType) -> str:
    if (type == TerrainBoundsCalculateType.POINT): 
        return "POINT"

def nameToTerrainBoundsType(name : str) -> TerrainBoundsCalculateType: 
    if (name == "POINT"): 
        return TerrainBoundsCalculateType.POINT
    
def calculate_bounding_box_around_point(center : World_Coordinates, radius_miles : float = 10.0) -> World_Bounding_Box:
    # Extract latitude and longitude
    lat = center.get_lat()
    lon = center.get_lon()
    radius_miles = float(radius_miles)
    
    # Convert miles to degrees
    lat_offset = radius_miles / 69.0
    lon_offset = radius_miles / (69.0 * math.cos(math.radians(lat)))
    
    # Calculate bounding box
    min_lat = lat - lat_offset
    max_lat = lat + lat_offset
    min_lon = lon - lon_offset
    max_lon = lon + lon_offset

    return World_Bounding_Box(World_Coordinates(min_lat, min_lon), World_Coordinates(max_lat, max_lon))

class ParseArea: 
    def __init__(self, boundsType : TerrainBoundsCalculateType, center, view_distance = None) -> None: 
        self.boundsType = boundsType
        self.center = center
        # Radius (in miles) used to calculate the bounding box around the
        # center point. Defaults to 10 to preserve historical behavior.
        # Coerced to float so the serialized JSON value is always a float.
        self.view_distance = float(view_distance) if view_distance is not None else 10.0

    @classmethod
    def fromJSONFile(cls, filePath): 
        with open(filePath, 'r') as file:
            jData = json.load(file)
            bounds = nameToTerrainBoundsType(jData['boundsType'])
            view_distance = jData.get('view_distance', jData.get('range', 10.0))
            return cls(bounds, World_Coordinates.fromDict(jData['center']), view_distance)
        
    def toJSON(self) -> dict: 
        return {
            'boundsType': terrainBoundsTypeToString(self.boundsType),
            'center': self.center.toJSON(),
            'view_distance': self.view_distance
        }
    
    def getTotalRegion(self) -> World_Bounding_Box: 
        if self.boundsType is TerrainBoundsCalculateType.POINT: 
            return calculate_bounding_box_around_point(self.center, self.view_distance)
        else:
            raise Exception("Unhandled boundsType declaration")
    