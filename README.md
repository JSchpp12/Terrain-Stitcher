# Terrain-Stitcher

All terrains are prepared as 7zip archives and are available in the releases. Place the extracted 7zip archive into a directory named "terrains" next to the main.py file. Each terrain release will contain a run.bat. Execute that script. After running, it is safe to delete the tmp directory which was created.

## Full terrain pass (`process-terrain`)

Run a complete download + gather pass for a shape AOI in one command, producing
one directory per quality tier. Low quality is LOD 17, high quality is LOD 18,
and an optional ultra-quality LOD 19 tier is added with `--ultra`. Each tier is
written to `<output>/<name>_<lod>` (e.g. `perryville_17`, `perryville_18`,
`perryville_19`) with the same `gathered_r*_c*.png` + `height_info.json` schema
the manual commands produce, so downstream consumers need no changes.

The command downloads the orthoimagery once at the highest requested LOD
(LOD 19 with `--ultra`, otherwise LOD 18) -- gdal2tiles builds a full 0..N
pyramid -- and gathers each tier out of that shared pyramid with
`gather-ortho --from-download --lod_min <lod>`. Chunking is mandatory:
`-d/--dimension` must be `>= 2`, because dimension 1 would emit one file per
cache tile (thousands for a real AOI).

Prerequisite: build the service registry once with `refresh-services` (see the
Elevation section).

```cmd
:: low (LOD 17) + high (LOD 18)
terrain_stitcher process-terrain --name perryville -s Shape.json -d 75

:: also produce ultra (LOD 19), and a continuous elevation GeoTIFF merged into
:: every tier via gather-ortho -e
terrain_stitcher process-terrain --name perryville -s Shape.json -d 75 --ultra --with-elevation
```

Options:

- `--name` (required): base name for the output directories -> `<name>_<lod>`.
- `-s/--shape` (required): Shape.json defining the AOI.
- `-o/--output`: base directory for the tier directories (default: current dir).
- `-d/--dimension` (required, `>= 2`): tiles per output image side
  (2 = 2x2 -> 1 image). Enforced to keep output file counts sane.
- `--ultra`: also produce the LOD 19 tier; the download runs at LOD 19.
- `--with-elevation`: download a continuous Float32 elevation GeoTIFF once
  (`download-elevation`) into `<name>_elevation` and merge it into every tier
  (`gather-ortho -e`). Off by default (ortho-only, like `run.bat`).
- `--keep-tiles`: keep the intermediate `<name>_tiles` pyramid (and any
  per-LOD fallback pyramids) after gathering. Deleted by default; keep them to
  `--resume` gathers without re-downloading.
- `--resume`: skip already-stitched groups in each tier (forwarded to
  `gather-ortho`); combine with `--keep-tiles`.
- `-f/--scaleFactor`: downscale each tile per tier (default 1.0; only
  downscaling). Forwarded to every tier's `gather-ortho`.
- `-w/--workers`, `--gather-workers`, `--chunk-px`, `--timeout`,
  `--resampling`, `--processes`, `--service-index`: download/stitch tuning,
  forwarded to `download-arcgis` / `download-elevation` / `gather-ortho`.

This replaces the manual `download-arcgis` -> `gather-ortho` sequence in
`run.bat`. If the installed gdal2tiles emits only the top LOD (rather than the
full pyramid), a tier whose LOD is missing from the shared pyramid falls back
to a dedicated download at that LOD, so the command is correct regardless of
the gdal2tiles `-z` semantics.

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
