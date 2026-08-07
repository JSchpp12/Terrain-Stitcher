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

## Elevation from the USGS National Map (download-elevation)

The National Map platform exposes a `3DEPElevation` ImageServer that returns
georeferenced Float32 elevation GeoTIFFs through the same `exportImage`
mechanism the ortho path uses. This repo registers it as an `"elevation"`
service and provides a `download-elevation` command that fetches F32 DEM
chunks over the shape AOI and mosaics them into one continuous GeoTIFF
(`elevation_merged.tif`) that feeds the existing `gather-ortho -src arcgis -e`
/ `prep-geo` pipeline.

### Prereqs: build the service registry

The service registry (coverage + native resolution + capability kinds per
layer) is fetched from the shipped endpoint list and cached:

```cmd
python -m terrain_stitcher refresh-services
```

### Download a continuous elevation raster

```cmd
python -m terrain_stitcher download-elevation -s Shape.json -o elevation_merged.tif
```

Options:

- `-s/--shape` (required): shape file defining the AOI.
- `-o/--output`: path for the merged GeoTIFF (default `elevation_merged.tif`).
- `--res <m/px>`: fetch resolution (defaults to the service's registered
  native pixel size, 10.0 m for 3DEP). A finer value only warns, since 3DEP
  resamples on the fly.
- `--chunk-px`, `--timeout`, `-w/--workers`: download tuning (same defaults
  as `download-arcgis`).
- `--service-index <N>`: pick one when several elevation services cover the AOI.
- `--padding <deg>`: optional degrees of padding around the AOI (default 0).

`download-arcgis` continues to select only `"imagery"` services, so it never
picks the elevation endpoint. `download-elevation` never produces a tile
pyramid; elevation is a continuous raster, so it writes a single merged
GeoTIFF instead of running gdal2tiles.
