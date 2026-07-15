from __future__ import annotations

import os

ALL_LAYERS_DIR = "_alllayers"

# The ArcGIS CacheTileFormat supported by this importer.
# Stored as a string so additional formats can be added later without
# changing the storage type.
SUPPORTED_TILE_FORMAT = "PNG"
TILE_EXTENSION = ".png"


def all_layers_path(cache_dir: str) -> str:
    """Resolve the ``_alllayers`` directory for a cache root."""
    return os.path.join(cache_dir, ALL_LAYERS_DIR)


def discover_row_dirs(alllayers_dir: str) -> list[str]:
    """Return sorted paths to every row directory under ``alllayers_dir``.

    The exploded cache layout is ``_alllayers/<L##>/<R######>/<C######.png>``;
    each ``<R######>`` folder holds all column tiles for one cache row at one
    level. This enumerates those row folders (one directory per cache row,
    across every level) so the acquisition source can process one row at a
    time instead of materialising every tile path up front -- keeping memory
    bounded when a cache contains millions of tiles.

    Directories are sorted by name (level, then row) for deterministic
    processing order. Non-directory entries are skipped. Returns an empty
    list when ``alllayers_dir`` does not exist.
    """
    row_dirs: list[str] = []
    if not os.path.isdir(alllayers_dir):
        return row_dirs
    for level_name in sorted(os.listdir(alllayers_dir)):
        level_dir = os.path.join(alllayers_dir, level_name)
        if not os.path.isdir(level_dir):
            continue
        for row_name in sorted(os.listdir(level_dir)):
            row_dir = os.path.join(level_dir, row_name)
            if os.path.isdir(row_dir):
                row_dirs.append(row_dir)
    return row_dirs


def gather_tile_files(alllayers_dir: str, cache_tile_format: str) -> list[str]:
    """Walk an ArcGIS ``_alllayers`` directory and collect every tile file
    matching the cache's tile format.

    The exploded cache layout is ``_alllayers/<level>/<row>/<col><ext>``;
    this filters by extension only, so the exact level/row naming
    convention does not matter at this stage. Only PNG tiles are supported
    for now.
    """
    fmt = cache_tile_format.strip().upper()
    if fmt != SUPPORTED_TILE_FORMAT:
        raise ValueError(
            f"Unsupported cache tile format: {cache_tile_format!r} "
            f"(only {SUPPORTED_TILE_FORMAT} is supported)"
        )
    matches: list[str] = []
    for root, _dirs, files in os.walk(alllayers_dir):
        for name in files:
            if name.lower().endswith(TILE_EXTENSION):
                matches.append(os.path.join(root, name))
    return matches
