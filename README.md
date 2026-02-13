# Terrain-Stitcher

All terrains are prepared as 7zip archives and are available in the releases. Place the extracted 7zip archive into a directory named "terrains" next to the main.py file. Each terrain release will contain a run.bat. Execute that script. After running, it is safe to delete the tmp directory which was created.

## Requirements

The following python packages are required: 
- rasterio
- pillow
- pyproj
- shapely
- rtree
- beautifulsoup4
- requests

```cmd
python -m pip install rasterio pillow pyproj shapely rtree beautifulsoup4 requests
```

## Setup
Create a `.env` file with:
USGS_APPLICATION_KEY=your_api_key_here
USGS_USERNAME=

### Troubleshooting

#### Windows

Missing DLL error on rasterio import. The most straightforward way to properly setup rasterio on windows is to first install gdal with conda 

```cmd
conda install -c conda-forge gdal
```

Then install rasterio with pip

```cmd
python -m pip install rasterio
```