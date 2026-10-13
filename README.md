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

### Docker (recommended)

A self-contained image (Python, GDAL, `gdal2tiles`, and all Python
requirements) is provided. See [docker/README.md](docker/README.md) for the
full guide. Two Compose stacks ship in `docker/`: `docker-compose.yml` routes
all traffic through Surfshark over WireGuard via
[Gluetun](https://github.com/qdm12/gluetun), while `docker-compose.novpn.yml`
runs directly with no VPN.

```bash
docker build -f docker/Dockerfile -t terrain-stitcher:latest .
docker run --rm -v "$PWD:/data" terrain-stitcher:latest refresh-services
```

Or with Compose (from the repo root — the stacks mount nothing by default, so
bring a data directory with `-v`; see [docker/README.md](docker/README.md)):

```bash
docker compose -f docker/docker-compose.yml run --rm \
  -v "./data:/data" terrain-stitcher refresh-services
```

### Local Python

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

Create a `.env` file with your optional USGS credentials (it is git-ignored;
copy `.env.example` for the full template):

```env
USGS_APPLICATION_KEY=your_api_key_here
USGS_USERNAME=
```

The same file holds the Surfshark/WireGuard settings for the Docker VPN stack
— see [docker/README.md](docker/README.md). They are only needed for the USGS
ortho source; ArcGIS/elevation work without them.

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

The chunks are anchored to the standard Web Mercator tile grid instead of the
AOI bbox corner, so the merged GeoTIFF is EPSG:3857 and is pixel-for-pixel
aligned with the ortho tile pyramid at the same LOD: elevation pixel `(i, j)`
corresponds exactly to ortho tile pixel `(i, j)`, with no reprojection or
resampling anywhere on the client. The raster covers the tile-aligned
envelope of the (padded) AOI, slightly larger than the shape region itself,
just like the ortho pyramid. A twin `elevation_merged_info.json` sidecar next
to the GeoTIFF records the projection system, so renderers never have to
guess between projected meters (EPSG:3857) and degrees (EPSG:4326).

> Web Mercator stretches ground distances by 1/cos(latitude): at 65°N a pixel
> is ~2.37x wider than its true ground size. Apply a horizontal cos(lat)
> correction at the render site; the elevation values themselves are
> unaffected.

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
- `--lod <N>`: Web Mercator level of detail to fetch at. The pixel size is
  156543.03392804097 / 2^lod m/px -- the same grid the ortho tile pyramid
  uses -- so elevation pixels align 1:1 with ortho tile pixels at the same
  LOD. Defaults to the LOD closest to the service's native resolution (~LOD
  14 for 3DEP's 10 m/px). A finer LOD only warns, since 3DEP resamples on
  the fly (the extra detail is interpolated, not real).
- `--chunk-px`, `--timeout`, `-w/--workers`: download tuning (same defaults
  as `download-arcgis`).
- `--service-index <N>`: pick one when several elevation services cover the AOI.
- `--padding <deg>`: optional degrees of padding around the AOI (default 0).

`download-arcgis` continues to select only `"imagery"` services, so it never
picks the elevation endpoint. `download-elevation` never produces a tile
pyramid; elevation is a continuous raster, so it writes a single merged
GeoTIFF instead of running gdal2tiles.

### Merging elevation into a gather (prep-geo / gather-ortho -e)

`prep-geo` and `gather-ortho -e` detect when every intersecting source
already sits on the Web Mercator tile grid (which `download-elevation`
produces) and composite those sources directly on that grid: the merged
output stays EPSG:3857 and stays 1:1 aligned with the ortho tiles, with no
reprojection or resampling. Heterogeneous inputs (e.g. mixed USGS tiles in
various CRSs) keep the legacy behavior of warping everything into one
continuous EPSG:4326 GeoTIFF. Both paths write the
`elevation_merged_info.json` sidecar recording which projection was used.
