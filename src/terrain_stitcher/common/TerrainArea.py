
class Latitude:
    def Parse(value: str) -> float:
        """Parse and validate latitude."""
        try:
            lat = float(value)
        except ValueError:
            raise ValueError("Latitude must be a float.")
        if not -90.0 <= lat <= 90.0:
            raise ValueError("Latitude must be between -90 and 90.")
        return lat

    def __init__(self, in_lat: str) -> None:
        self.value = Latitude.Parse(in_lat)

class Longitude:
    def Parse(value: str) -> float:
        """Parse and validate longitude."""
        try:
            lon = float(value)
        except ValueError:
            raise ValueError("Longitude must be a float.")
        if not -180.0 <= lon <= 180.0:
            raise ValueError("Longitude must be between -180 and 180.")
        return lon

    def __init__(self, in_lon: str) -> None:
        self.value = Longitude.Parse(in_lon)

class World_Coordinates:
    def __init__(self, lat: str = None, lon: str = None) -> None:
        self.lat = None
        if lat is not None:
            self.lat = lat
        self.lon = None
        if lon is not None:
            self.lon = lon

    def get_lon(self) -> float:
        if self.lon is None:
            raise ValueError("Long value is none")

        return Longitude(self.lon).value

    def get_lat(self) -> float:
        if self.lat is None: 
            raise ValueError("Lat value is none")
        
        return Latitude(self.lat).value
    
    def isValid(self) -> bool: 
        return self.lat is not None and self.lon is not None
    
    def toJSON(self): 
        return {
            "lat": self.lat, 
            "lon": self.lon
        }
    
    @classmethod
    def fromDict(cls, data):
        return cls(data['lat'], data['lon'])

class World_Bounding_Box:
    def __init__(
        self, lower_left: World_Coordinates, upper_right: World_Coordinates
    ) -> None:
        self.lower_left = lower_left
        self.upper_right = upper_right

    def get_lower_left(self) -> World_Coordinates:
        return self.lower_left

    def get_upper_right(self) -> World_Coordinates:
        return self.upper_right
