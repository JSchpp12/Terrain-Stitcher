import requests
import json
import sys

from .config import USGS_APPLICATION_KEY, USGS_USERNAME
from terrain_stitcher.common import World_Bounding_Box

SERVICE_URL = "https://m2m.cr.usgs.gov/api/api/json/stable/"

class Client:
    def Send_Request(endpoint, data, apiKey=None):
        url = SERVICE_URL + endpoint
        pos = url.rfind("/") + 1
        endpoint = url[pos:]
        json_data = json.dumps(data)

        if apiKey == None:
            response = requests.post(url, json_data, timeout=2)
        else:
            headers = {"X-Auth-Token": apiKey}
            response = requests.post(url, json_data, headers=headers)

        try:
            httpStatusCode = response.status_code
            if response == None:
                print("No output from service")
                sys.exit()
            output = json.loads(response.text)
            if output["errorCode"] != None:
                print("Failed Request ID", output["requestId"])
                print(output["errorCode"], "-", output["errorMessage"])
                sys.exit()
            if httpStatusCode == 404:
                print("404 Not Found")
                sys.exit()
            elif httpStatusCode == 401:
                print("401 Unauthorized")
                sys.exit()
            elif httpStatusCode == 400:
                print("Error Code", httpStatusCode)
                sys.exit()
        except Exception as e:
            response.close()
            pos = SERVICE_URL.find("api")
            print(
                f"Failed to parse request {endpoint} response. Re-check the input {json_data}. The input examples can be found at {url[:pos]}api/docs/reference/#{endpoint}\n"
            )
            sys.exit()
        response.close()
        print(f"Finished request {endpoint} with request ID {output['requestId']}\n")

        return output["data"]

    def submitRequest(self, endpoint, data): 
        if self.api_key is None: 
            raise Exception("Session not valid")
        
        return Client.Send_Request(endpoint, data, self.api_key)
    
    def Authorize_Login():
        payload = {"username": USGS_USERNAME, "token": USGS_APPLICATION_KEY}
        endpoint = "login-token"
        print(f"Attempting to authorize client with username: {USGS_USERNAME}")
        print(f"Key: {USGS_APPLICATION_KEY}")
        api_key = Client.Send_Request(endpoint, payload)
        return api_key

    def Logout(api_key):
        endpoint = "logout"
        if Client.Send_Request(endpoint, None, api_key) == None:
            print("Successful logout")
        else:
            print("Logout Failed")

    def __init__(self):
        self.api_key = None

    def __enter__(self):
        if not self.has_active_login():
            self.attempt_login()

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.has_active_login():
            print("Logging out...")
            self.attempt_logout()

        return False

    def attempt_login(self):
        try:
            new_session = Client.Authorize_Login()
            self.api_key = new_session
            print("Authorization successful")
        except Exception as e:
            print(f"Failed to login: {e}")
            self.api_key = None

    def attempt_logout(self):
        Client.Logout(self.api_key)

        self.api_key = None

    def has_active_login(self) -> bool:
        if self.api_key is not None:
            return True

        return False

    def find_datasets_for(self, bounding_box: World_Bounding_Box, target_dataset=None):
        name = None
        if target_dataset is not None:
            name = target_dataset

        spatial_filter = {
            "filterType": "mbr",
            "lowerLeft": {
                "latitude": bounding_box.get_lower_left().get_lat(),
                "longitude": bounding_box.get_lower_left().get_lon(),
            },
            "upperRight": {
                "latitude": bounding_box.get_upper_right().get_lat(),
                "longitude": bounding_box.get_upper_right().get_lon(),
            },
        }
        payload = None
        if name is not None:
            payload = {"datasetName": name, "spatialFilter": spatial_filter}
        else:
            payload = {"spatialFilter": spatial_filter}

        print("Searching for datasets")

        datasets = Client.Send_Request("dataset-search", payload, self.api_key)
        return datasets

    def find_scenes(self, dataset, bounding_box: World_Bounding_Box):
        spatial_filter = {
            "filterType": "mbr",
            "lowerLeft": {
                "latitude": bounding_box.get_lower_left().get_lat(),
                "longitude": bounding_box.get_lower_left().get_lon(),
            },
            "upperRight": {
                "latitude": bounding_box.get_upper_right().get_lat(),
                "longitude": bounding_box.get_upper_right().get_lon(),
            },
        }

        name = dataset["datasetAlias"]
        payload = {
            "datasetName": name,
            "maxResults": 1000,
            "sceneFilter": {"spatialFilter": spatial_filter}}

        scenes = Client.Send_Request("scene-search", payload, self.api_key)

        return scenes
