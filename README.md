# Terrain-Stitcher

All terrains are prepared as 7zip archives and are available in the releases. Place the extracted 7zip archive into a directory named "terrains" next to the main.py file. Each terrain release will contain a run.bat. Execute that script. After running, it is safe to delete the tmp directory which was created.

## Requirenments

The following python packages are required: 
- rasterio
- pillow
- pyproj
- shapely
- rtree
- beautifulsoup4

```cmd
python -m pip install rasterio pillow pyproj shapely rtree beautifulsoup4
```

