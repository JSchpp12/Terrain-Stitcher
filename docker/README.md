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

**The stacks mount nothing by default.** Every `run` binds the directory you
want to work in at `/data` with `-v` — see
[Mounting a data directory](#mounting-a-data-directory). Without a mount,
anything the app writes under `/data` is discarded along with the container
(`run --rm` deletes it afterwards).

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

The compose files reference the repo root (`..`) only for the build context
and the `.env`, so the stacks work regardless of where you invoke them. The
examples below run **from the repository root**; `-v` paths resolve against
your shell's current directory.

```bash
# Build the image
docker compose -f docker/docker-compose.yml build

# Build the ArcGIS service registry (prerequisite for the download commands)
docker compose -f docker/docker-compose.yml run --rm \
  -v "./data/perryville:/data" terrain-stitcher refresh-services

# Define the AOI (writes Shape.json into the mounted directory)
docker compose -f docker/docker-compose.yml run --rm \
  -v "./data/perryville:/data" \
  terrain-stitcher create-bounds -lat 39.0 -lon -105.0 -t POINT -vd 5

# Full download + gather for one LOD
docker compose -f docker/docker-compose.yml run --rm \
  -v "./data/perryville:/data" \
  terrain-stitcher process-terrain --name perryville -s Shape.json -d 75 --lod 12 --with-elevation
```

Output is written to `<mounted-dir>/perryville_12/`.

## Mounting a data directory

No volumes are baked into the compose files — bring your own data directory
on every run:

```bash
docker compose -f docker/docker-compose.yml run --rm \
  -v "<host-dir>:/data" terrain-stitcher <command>
```

- Relative `-v` paths resolve against your shell's current directory. In
  PowerShell and cmd, `-v "./data/perryville:/data"` works as written.
- **Git Bash users must disable MSYS path conversion**. Otherwise the `-v`
  argument is mangled (the `:/data` suffix is mistaken for a POSIX path
  list), `/data` never binds, and all output is **silently lost** — the app
  writes to the container's ephemeral `/data`, which `--rm` discards:

  ```bash
  MSYS_NO_PATHCONV=1 docker compose -f docker/docker-compose.yml run --rm \
    -v "./data/perryville:/data" terrain-stitcher process-terrain ...
  ```

  An absolute Windows path (`-v "C:/data/perryville:/data"`) also avoids the
  problem.
- A source path of just `.` or `./` does **not** bind-mount — Docker quietly
  treats it as an anonymous volume and `/data` stays empty. To mount the repo
  root itself (e.g. for an interactive dev shell), use an absolute path:
  `-v "$PWD:/data"` (PowerShell) or `-v "C:/repos/Terrain-Stitcher:/data"`.
- Use the same mounted directory for every step of a pipeline:
  `refresh-services` caches the service registry at
  `<mounted-dir>/.cache/terrain-stitcher/services.json` and `create-bounds`
  writes `Shape.json` into it, so reusing the mount avoids redoing work.
- Because nothing is mounted by default, the repo root (and its `.env`) is
  not visible inside the app container; the USGS credentials reach it via
  Compose's `env_file` instead.

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
   docker compose -f docker/docker-compose.yml run --rm \
     -v "./data/perryville:/data" terrain-stitcher refresh-services
   ```

### Verify the tunnel

Print the public IP the app sees. It should be a Surfshark exit IP, not your
home IP (no mount needed — the command only prints the exit IP):

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
docker compose -f docker/docker-compose.novpn.yml run --rm \
  -v "./data/perryville:/data" terrain-stitcher refresh-services
docker compose -f docker/docker-compose.novpn.yml run --rm \
  -v "./data/perryville:/data" \
  terrain-stitcher process-terrain --name perryville -s Shape.json -d 75 --lod 12
```

It reuses the same image and the same repo-root `.env`, but ignores the
`WIREGUARD_*` settings. It runs under a different Compose project name
(`terrain-stitcher-novpn`), so it can run alongside the VPN stack without
colliding on containers or networks.

## WeatherCams

```bash
docker compose -f docker/docker-compose.yml run --rm \
  -v "./data/weathercams:/data" terrain-stitcher refresh-services
docker compose -f docker/docker-compose.yml run --rm \
  -v "./data/weathercams:/data" terrain-stitcher weathercams-sync
docker compose -f docker/docker-compose.yml run --rm \
  -v "./data/weathercams:/data" terrain-stitcher weathercams-prepare --view-distance 5 --limit 1
docker compose -f docker/docker-compose.yml run --rm \
  -v "./data/weathercams:/data" terrain-stitcher weathercams-run --dimension 75 --lod 12 --only-site 117
```

The `weathercams/ledger.json` progress file and outputs live under the mounted
`/data`, so they persist across runs.

## Portability (other machines / directories)

The compose files use only relative paths (`context: ..`, `env_file: ../.env`),
all resolved against **this `docker/` directory** — not your shell's current
directory — so the stacks work from anywhere. The `-v` mount path is the
exception: it resolves against your shell's current directory (see
[Mounting a data directory](#mounting-a-data-directory)). On a fresh checkout
you only need to:

1. Recreate `.env` from `.env.example` (it is git-ignored).
2. Build the image on that machine (the `terrain-stitcher:latest` tag is local,
   not pulled): `docker compose -f docker/docker-compose.yml build`.
3. Make sure the directory you plan to mount is shared with Docker Desktop
   (on Windows: Settings → Resources → File sharing).

## Development notes

The image installs a **copy** of `src/`. After editing code, rebuild from the
repo root:

```bash
docker compose -f docker/docker-compose.yml build
```

If you prefer live code edits without rebuilding, run an interactive shell with
the repo mounted (use an absolute path — a bare `.` does not bind-mount):

```bash
# PowerShell
docker compose -f docker/docker-compose.novpn.yml run --rm \
  -v "$PWD:/data" --entrypoint bash terrain-stitcher
```

```bash
# Git Bash
MSYS_NO_PATHCONV=1 docker compose -f docker/docker-compose.novpn.yml run --rm \
  -v "C:/repos/Terrain-Stitcher:/data" --entrypoint bash terrain-stitcher
```