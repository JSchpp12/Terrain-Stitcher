# Running Terrain-Stitcher in Docker

The included `Dockerfile` builds a self-contained image with everything the
project needs at runtime:

| Component | Provided by |
| --- | --- |
| Python 3.12 | `ghcr.io/osgeo/gdal:ubuntu-small-3.10.3` |
| GDAL + Python `osgeo` bindings | base image |
| `gdal2tiles`, `gdal_translate`, `gdalbuildvrt`, `gdalinfo` | base image |
| rasterio, pyproj, shapely, rtree, pillow, requests, … | `pip` from `pyproject.toml` |
| `terrain_stitcher` CLI + `python -m terrain_stitcher` | installed from `src/` |

NumPy is pinned to `<2` and rasterio to `<1.5` because the distro `osgeo`
bindings (`osgeo.gdal_array`, used by `gdal2tiles`) are built against NumPy 1.x,
while rasterio 1.5+ requires NumPy 2. rasterio 1.4.4 still satisfies the
project's `>=1.4.3` requirement.

## 1. Build

```bash
docker build -t terrain-stitcher:latest .
```

Or with Compose:

```bash
docker compose build
```

## 2. Provide USGS credentials (optional)

Only the USGS source (`gather-ortho -src usgs`, `prep-ortho`, …) needs these.
The ArcGIS and elevation paths do not.

Create `.env` in the project root:

```env
USGS_APPLICATION_KEY=your_api_key_here
USGS_USERNAME=
```

## 3. Run

The image's `ENTRYPOINT` is `terrain_stitcher`, so append any subcommand. Mount
a directory at `/data` (the image `WORKDIR`) so inputs and outputs persist.

### Plain `docker run`

```bash
# Windows (Git Bash / cmd): use an absolute host path
docker run --rm -v "C:/repos/Terrain-Stitcher:/data" terrain-stitcher:latest --help
docker run --rm -v "C:/repos/Terrain-Stitcher:/data" terrain-stitcher:latest refresh-services
docker run --rm -v "C:/repos/Terrain-Stitcher:/data" terrain-stitcher:latest \
  process-terrain --name perryville -s Shape.json -d 75 --lod 12
```

Set `XDG_CACHE_HOME` (already set to `/data/.cache` in the image) so the
`refresh-services` registry survives between invocations. The cache lands in
`<mounted-dir>/.cache/terrain-stitcher/services.json`.

### Docker Compose

`docker-compose.yml` mounts the project at `/data` for you:

```bash
docker compose run --rm terrain-stitcher refresh-services
docker compose run --rm terrain-stitcher create-bounds -lat 39.0 -lon -105.0 -t POINT -vd 5
docker compose run --rm terrain-stitcher process-terrain --name perryville -s Shape.json -d 75 --lod 12
```

## Typical first run

```bash
# Build the service registry once (persisted in ./.cache):
docker compose run --rm terrain-stitcher refresh-services

# Define the AOI (writes Shape.json to the mounted directory):
docker compose run --rm terrain-stitcher create-bounds -lat 39.0 -lon -105.0 -t POINT -vd 5

# Full download + gather for one LOD:
docker compose run --rm terrain-stitcher process-terrain \
  --name perryville -s Shape.json -d 75 --lod 12 --with-elevation
```

Output is written to `<mounted-dir>/perryville_12/`.

## WeatherCams

```bash
docker compose run --rm terrain-stitcher refresh-services
docker compose run --rm terrain-stitcher weathercams-sync
docker compose run --rm terrain-stitcher weathercams-prepare --view-distance 5 --limit 1
docker compose run --rm terrain-stitcher weathercams-run --dimension 75 --lod 12 --only-site 117
```

The `weathercams/ledger.json` progress file and outputs live under the mounted
`/data`, so they persist across runs.

## Development notes

The image installs a **copy** of `src/`. After editing code, rebuild:

```bash
docker compose build
```

If you prefer live code edits without rebuilding, run an interactive shell with
the repo mounted and use the installed CLI for dependencies:

```bash
docker run --rm -it -v "C:/repos/Terrain-Stitcher:/data" --entrypoint bash terrain-stitcher:latest
```
