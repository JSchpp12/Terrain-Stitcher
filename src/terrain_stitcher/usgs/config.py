from dotenv import load_dotenv
import os

path = os.path.join(os.getcwd(), ".env")
load_dotenv(path)
USGS_APPLICATION_KEY = os.getenv("USGS_APPLICATION_KEY")
USGS_USERNAME = os.getenv("USGS_USERNAME")