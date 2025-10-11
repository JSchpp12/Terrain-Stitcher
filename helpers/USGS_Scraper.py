import requests
from bs4 import BeautifulSoup

class USGS_ScrapedData:
    def get_main_page_content(souped): 
        content = souped.find('div', id='pageContentLayoutContainer')
        if content is None:
            raise Exception("Invalid page layout")

        return content
    
    def is_valid_response(parsed_content) -> bool: 
        # content = parsed_content.find(id="pageContentLayoutContainer")
        content = USGS_ScrapedData.get_main_page_content(parsed_content)

        if content is None:
            return False

        if "error" in content.text:
            return False

        return True
    
    def getValueForTableRowWithFirstColumnOf(parsed_content, corner_abriv):
        value = None
        content = USGS_ScrapedData.get_main_page_content(parsed_content)

        table = content.find_all('tbody')

        if len(table) > 1:
            raise Exception("Unexpected page layout")

        for row in table[0].find_all('tr'):
            columns = row.find_all('td')
            if len(columns) > 2:
                raise Exception("Unexpected table layout")

            if f"{corner_abriv}" in columns[0].text:
                return columns[1].text
            
        return None

    def getCoords_northEast(parsed_content) -> tuple[float, float]:
        lat = None
        lon = None

        lat = USGS_ScrapedData.getValueForTableRowWithFirstColumnOf(parsed_content, "NE Corner Lat dec")
        lon = USGS_ScrapedData.getValueForTableRowWithFirstColumnOf(parsed_content, "NE Corner Long dec")

        return (lat, lon)

    def getCoords_northWest(parsed_content) -> tuple[float, float]:
        lat = None
        lon = None

        lat = USGS_ScrapedData.getValueForTableRowWithFirstColumnOf(parsed_content, "NW Corner Lat dec")
        lon = USGS_ScrapedData.getValueForTableRowWithFirstColumnOf(parsed_content, "NW Corner Long dec")

        return (lat, lon)

    def getCoords_southEast(parsed_content) -> tuple[float, float]:
        lat = None
        lon = None
        
        lat = USGS_ScrapedData.getValueForTableRowWithFirstColumnOf(parsed_content, 'SE Corner Lat dec')
        lon = USGS_ScrapedData.getValueForTableRowWithFirstColumnOf(parsed_content, 'SE Corner Long dec')

        return (lat, lon)

    def getCoords_southWest(parsed_content) -> tuple[float, float]:
        lat = None
        lon = None

        lat = USGS_ScrapedData.getValueForTableRowWithFirstColumnOf(parsed_content, "SW Corner Lat dec")
        lon = USGS_ScrapedData.getValueForTableRowWithFirstColumnOf(parsed_content, 'SW Corner Long dec')

        return (lat, lon)
    
    def getCoords_center(parsed_content) -> tuple[float, float]: 
        lat = None
        lon = None

        lat = USGS_ScrapedData.getValueForTableRowWithFirstColumnOf(parsed_content, "Center Latitude dec")
        lon = USGS_ScrapedData.getValueForTableRowWithFirstColumnOf(parsed_content, "Center Longitude dec")

        return (lat, lon)

    def parse(url): 
        response = requests.get(url, timeout=5)
        html_content = response.text
        soup = BeautifulSoup(html_content, 'html.parser')

        return soup

    def __init__(self, project_id, full_chunk_name):
        self.coords_northEast = None
        self.coords_northWest = None
        self.coords_southEast = None
        self.coords_southWest = None
        self.coords_center = None

        url = f"https://earthexplorer.usgs.gov/scene/metadata/full/{project_id}/{full_chunk_name}/"
        print(f"Query url: {url}")
        self.soup = USGS_ScrapedData.parse(url)

        if not USGS_ScrapedData.is_valid_response(self.soup):
            raise Exception("USGS Failed to find the provided project and chunk")
        
        self.coords_northEast = USGS_ScrapedData.getCoords_northEast(self.soup)
        self.coords_northWest = USGS_ScrapedData.getCoords_northWest(self.soup)
        self.coords_southEast = USGS_ScrapedData.getCoords_southEast(self.soup)
        self.coords_southWest = USGS_ScrapedData.getCoords_southWest(self.soup)
        self.coords_center = USGS_ScrapedData.getCoords_center(self.soup)

        print("Done")

if __name__ == "__main__": 
    pass 