from dotenv import load_dotenv
import os

load_dotenv()
USGS_APPLICATION_KEY = os.getenv("USGS_APPLICATION_KEY")
USGS_USERNAME = os.getenv("USGS_USERNAME")