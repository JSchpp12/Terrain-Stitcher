# Terrain-Stitcher container image
#
# Base image bundles GDAL (CLI tools + Python `osgeo` bindings + gdal2tiles)
# on Ubuntu 24.04 / Python 3.12. rasterio has prebuilt cp312 wheels, so the
# whole stack installs without compiling anything.
FROM ghcr.io/osgeo/gdal:ubuntu-small-3.10.3

# --- System packages --------------------------------------------------------
# The GDAL image ships python3 + the `osgeo` bindings in
# /usr/lib/python3/dist-packages, but no pip/venv. Install those (and
# ca-certificates for TLS) and clean the apt lists in the same layer.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        python3-pip \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# --- Python environment -----------------------------------------------------
# --system-site-packages makes the OS-provided `osgeo` / `osgeo_utils`
# (gdal2tiles) importable from inside the venv while pip installs the
# project's pinned dependencies into the venv.
RUN python3 -m venv --system-site-packages /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
# The distro `osgeo` bindings (osgeo.gdal_array, used by gdal2tiles) are
# compiled against NumPy 1.x, so pin NumPy <2 and rasterio <1.5 (rasterio
# 1.5+ requires NumPy>=2). rasterio still satisfies the project's >=1.4.3.
# Install these first so the project install leaves them untouched.
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir "numpy<2" "rasterio>=1.4.3,<1.5" \
    && pip install --no-cache-dir .

# --- Runtime ----------------------------------------------------------------
# Work and write outputs under /data (mount the repo or a data dir there).
# XDG_CACHE_HOME keeps the `refresh-services` registry cache
# (~/.cache/terrain-stitcher/services.json) on the mounted volume so it
# survives between `docker run` invocations.
ENV XDG_CACHE_HOME=/data/.cache
WORKDIR /data

ENTRYPOINT ["terrain_stitcher"]
CMD ["--help"]
