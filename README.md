# Terrain-Stitcher

All terrains are prepared as 7zip archives and are available in the releases. Place the extracted 7zip archive into a directory named "terrains" next to the main.py file. Each terrain release will contain a run.bat. Execute that script. After running, it is safe to delete the tmp directory which was created.

## FAA WeatherCams workflow

WeatherCams sites come from the FAA JSON API. Progress is kept in a local
`weathercams/ledger.json` file that maps site IDs to boolean completion flags:

```json
{
  "133": true,
  "134": false
}
```

### Download one site

Build the ArcGIS service registry once, then sync the site ledger:

```cmd
terrain_stitcher refresh-services
terrain_stitcher weathercams-sync
```

Prepare one incomplete site:

```cmd
terrain_stitcher weathercams-prepare --view-distance 5 --limit 1
```

This creates one file in `weathercams/shapes`, such as `117.json`. Use that
site ID in the download command:

```cmd
terrain_stitcher weathercams-run --dimension 75 --lod 12 --only-site 117
```

A successful run writes the requested LOD output under:

```text
weathercams/outputs/weathercam_117_12
```

and marks the site `true` in the ledger.

### Download every incomplete site

```cmd
terrain_stitcher weathercams-prepare --view-distance 5
terrain_stitcher weathercams-run --dimension 75 --lod 12
```

### Track and transfer progress

```cmd
terrain_stitcher weathercams-status
terrain_stitcher weathercams-complete --site 117
terrain_stitcher weathercams-retry --site 117
terrain_stitcher weathercams-export --out weathercams/exports/machine-a.json
terrain_stitcher weathercams-import --input weathercams/exports/machine-a.json
```

Each machine has its own ledger, so import another machine's completed-site
export before starting work there. Use `--only-site` or `--limit` to partition
work between machines.

## Full terrain pass (`process-terrain`)

Run a complete download + gather pass for one requested LOD. The output is
written to `<output>/<name>_<lod>` using the same `gathered_r*_c*.png` +
`height_info.json` schema the manual commands produce.

Prerequisite: build the service registry once with `refresh-services`.

```cmd
terrain_stitcher process-terrain --name perryville -s Shape.json -d 75 --lod 12
```

Options:

- `--name` (required): base name for the output directory.
- `-s/--shape` (required): Shape.json defining the AOI.
- `-o/--output`: base output directory (default: current dir).
- `-d/--dimension` (required, `>= 2`): tiles per output image side.
- `--lod` (required): target LOD to download and gather.
- `--with-elevation`: also download and merge a continuous elevation GeoTIFF.
- `--keep-tiles`: retain the intermediate tile pyramid for inspection or
  manual re-runs.
- `-f/--scaleFactor`: downscale each output tile (default 1.0; only
  downscaling).
- `-w/--workers`, `--gather-workers`, `--chunk-px`, `--timeout`,
  `--resampling`, `--processes`, `--service-index`: download/stitch tuning.

This replaces the manual `download-arcgis` -> `gather-ortho` sequence in
`run.bat`.

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
