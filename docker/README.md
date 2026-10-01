# Running Terrain-Stitcher in Docker

Everything Docker-related lives in this directory:

| File | Purpose |
| --- | --- |
| `Dockerfile` | Builds the self-contained app image |
| `Dockerfile.dockerignore` | Build-context ignore (Docker reads it from here, next to the Dockerfile) |
| `docker-compose.yml` | **VPN** stack: Gluetun (Surfshark/WireGuard) + the app |
| `docker-compose.novpn.yml` | **No-VPN** stack: the app on your normal connection |
| `README.md` | This file |

The shared configuration lives in the **repo-root `.env`** (not in this
directory), because the app also reads it when run locally. Start by copying
the example:

```bash
cp .env.example .env
```

It holds the Surfshark/WireGuard settings (used by the Gluetun gateway in the
VPN stack) and the optional USGS credentials (used by the app). Each stack —
and the local Python setup — ignores the settings that don't apply to it. The
file is git-ignored, so the private key stays out of version control.

The `Dockerfile` builds a self-contained image with everything the project needs
at runtime:

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

## Running the commands

The compose files reference the repo root (`..`) for the build context and the
`/data` mount, so they work regardless of where you invoke them. The examples
below run **from the repository root** with `-f`. You can equally `cd docker`
and drop the `-f docker/...` part.

```bash
# Build the image
docker compose -f docker/docker-compose.yml build

# Build the ArcGIS service registry (prerequisite for the download commands)
docker compose -f docker/docker-compose.yml run --rm terrain-stitcher refresh-services

# Define the AOI (writes Shape.json into the mounted repo root)
docker compose -f docker/docker-compose.yml run --rm terrain-stitcher \
  create-bounds -lat 39.0 -lon -105.0 -t POINT -vd 5

# Full download + gather for one LOD
docker compose -f docker/docker-compose.yml run --rm terrain-stitcher \
  process-terrain --name perryville -s Shape.json -d 75 --lod 12 --with-elevation
```

Output is written to `<mounted-dir>/perryville_12/`.

The repo root is mounted at `/data` by default. To use a different data
directory, override the mount per run with a path relative to your shell's
current directory (run these from the repo root):

```bash
docker compose -f docker/docker-compose.yml run --rm \
  -v "./data/den:/data" \
  terrain-stitcher process-terrain --name perryville -s Shape.json -d 75 --lod 12
```

## Routing through a VPN (Gluetun + Surfshark WireGuard)

The `terrain-stitcher` service in `docker-compose.yml` uses
`network_mode: service:gluetun`, so it shares Gluetun's network namespace and
**all** of its traffic exits through Surfshark over WireGuard. Gluetun's
firewall is a kill switch: if the tunnel drops, the app loses network access
instead of silently leaking over your ISP.

There is also a separate no-VPN stack, `docker-compose.novpn.yml` (see
[No VPN](#no-vpn) below).

### One-time Surfshark setup

1. Log in to Surfshark → **VPN** → **Manual Setup** → **Desktop or mobile** →
   **WireGuard**.
2. Choose **"I don't have a keypair"**, give it a name, and
   **Generate a new keypair**. Copy the **Private key**.
3. Pick a location and download the config file. In the `[Interface]` section,
   copy the `Address =` value (for example `10.14.0.2/16`) — this is the
   private tunnel IP, **not** the server/endpoint IP.
4. Fill in the repo-root `.env`:

   ```env
   WIREGUARD_PRIVATE_KEY=<your-private-key>
   WIREGUARD_ADDRESSES=10.14.0.2/16
   SERVER_HOSTNAMES=us-den.prod.surfshark.com
   ```

   Use exactly one server selector: `SERVER_HOSTNAMES` (narrowest),
   `SERVER_CITIES`, or `SERVER_COUNTRIES`. To list the hostnames Gluetun knows
   about:

   ```bash
   docker run --rm qmcgaw/gluetun format-servers -surfshark
   ```

5. Build and run as usual — no extra flags are needed:

   ```bash
   docker compose -f docker/docker-compose.yml build
   docker compose -f docker/docker-compose.yml run --rm terrain-stitcher refresh-services
   ```

### Verify the tunnel

Print the public IP the app sees. It should be a Surfshark exit IP, not your
home IP:

```bash
docker compose -f docker/docker-compose.yml run --rm \
  --entrypoint python3 terrain-stitcher -c \
  "import urllib.request; print(urllib.request.urlopen('https://ipinfo.io/ip').read().decode())"
```

Gluetun also logs the selected server and keeps its server list fresh:

```bash
docker compose -f docker/docker-compose.yml logs gluetun
```

### Notes

- The WireGuard interface address (`WIREGUARD_ADDRESSES`) belongs to your
  **keypair**, not the server, so it stays the same for every location.
- On Windows/Docker Desktop (WSL2) no kernel WireGuard module is needed;
  Gluetun falls back to the bundled userspace implementation. `/dev/net/tun`
  and `NET_ADMIN` are already set in the compose file.
- After changing the VPN values in `.env`, recreate the gateway:
  `docker compose -f docker/docker-compose.yml up -d --force-recreate gluetun`.
  That includes switching endpoints: change `SERVER_HOSTNAMES` to another
  hostname (e.g. `us-sea.prod.surfshark.com`) and recreate. Only one endpoint
  is active at a time.

## No VPN

`docker-compose.novpn.yml` is a standalone stack with no Gluetun:

```bash
docker compose -f docker/docker-compose.novpn.yml build
docker compose -f docker/docker-compose.novpn.yml run --rm terrain-stitcher refresh-services
docker compose -f docker/docker-compose.novpn.yml run --rm terrain-stitcher \
  process-terrain --name perryville -s Shape.json -d 75 --lod 12
```

It reuses the same image and the same repo-root `.env`, but ignores the
`WIREGUARD_*` settings. It runs under a different Compose project name
(`terrain-stitcher-novpn`), so it can run alongside the VPN stack without
colliding on containers or networks.

## WeatherCams

```bash
docker compose -f docker/docker-compose.yml run --rm terrain-stitcher refresh-services
docker compose -f docker/docker-compose.yml run --rm terrain-stitcher weathercams-sync
docker compose -f docker/docker-compose.yml run --rm terrain-stitcher weathercams-prepare --view-distance 5 --limit 1
docker compose -f docker/docker-compose.yml run --rm terrain-stitcher weathercams-run --dimension 75 --lod 12 --only-site 117
```

The `weathercams/ledger.json` progress file and outputs live under the mounted
`/data`, so they persist across runs.

## Portability (other machines / directories)

The compose files use only relative paths (`context: ..`, `..:/data`,
`env_file: ../.env`), all resolved against **this `docker/` directory** — not
your shell's current directory. So you can clone the repo to any path on any
machine (e.g. `D:\projects\Terrain-Stitcher`) and run the same commands
unchanged. On a fresh checkout you only need to:

1. Recreate `.env` from `.env.example` (it is git-ignored).
2. Build the image on that machine (the `terrain-stitcher:latest` tag is local,
   not pulled): `docker compose -f docker/docker-compose.yml build`.
3. Make sure the repo's drive/folder is shared with Docker Desktop (on
   Windows: Settings → Resources → File sharing).

For the optional data-dir override, prefer relative paths such as
`-v "./data/den:/data"` (resolved against your current directory) rather than
absolute `C:/...` paths, so the commands keep working from anywhere.

## Development notes

The image installs a **copy** of `src/`. After editing code, rebuild from the
repo root:

```bash
docker compose -f docker/docker-compose.yml build
```

If you prefer live code edits without rebuilding, run an interactive shell with
the repo mounted:

```bash
docker compose -f docker/docker-compose.novpn.yml run --rm --entrypoint bash terrain-stitcher
```