from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from terrain_stitcher.sources import Bounds


@dataclass
class TileInfo:
    """Metadata parsed from a single ArcGIS exploded-cache tile file path.

    Attributes:
        path: the tile path relative to the cache's ``_alllayers`` directory
            (e.g. ``L23/R0027e3a0/C00075f6b.png``).
        layer_number: integer level id parsed from the ``L##`` folder.
        row_number: integer row index parsed from the ``R<hex>`` folder
            (hexadecimal).
        col_number: integer column index parsed from the ``C<hex>`` file stem
            (hexadecimal).
    """

    path: Path
    layer_number: int
    row_number: int
    col_number: int

    @classmethod
    def from_path(
        cls,
        file_path: "str | Path",
        all_layers_dir: "str | Path",
    ) -> "TileInfo":
        """Parse a single tile file path into a :class:`TileInfo`.

        The expected layout, relative to ``all_layers_dir``, is::

            <L##>/<R<hex>>/<C<hex>.png>

        where the row folder and the column file name encode their index as
        (zero-padded) hexadecimal strings, and the level folder encodes the
        level id as a decimal integer after the ``L`` prefix.
        """
        rel = Path(file_path).relative_to(Path(all_layers_dir))
        parts = rel.parts
        if len(parts) != 3:
            raise ValueError(
                f"Expected <L##>/<Rhex>/<Chex>.png relative to all_layers_dir, "
                f"got: {rel}"
            )

        layer_part, row_part, col_file = parts

        if not layer_part.startswith("L"):
            raise ValueError(f"Bad level folder (expected 'L' prefix): {layer_part!r}")
        if not row_part.startswith("R"):
            raise ValueError(f"Bad row folder (expected 'R' prefix): {row_part!r}")

        col_stem = Path(col_file).stem
        if not col_stem.startswith("C"):
            raise ValueError(f"Bad column file (expected 'C' prefix): {col_file!r}")

        return cls(
            path=rel,
            layer_number=int(layer_part[1:]),
            row_number=int(row_part[1:], 16),
            col_number=int(col_stem[1:], 16),
        )

    @classmethod
    def from_paths(
        cls,
        file_paths: list,
        all_layers_dir: str | Path,
    ) -> list["TileInfo"]:
        """Bulk-construct :class:`TileInfo` from many file paths at once.

        Faster than calling :meth:`from_path` per tile for the thousands-of-
        files run: it avoids constructing a :class:`Path` per file and calling
        ``Path.relative_to``. Instead it strips the ``_alllayers`` prefix as a
        string, splits on the OS separator, and uses C-level ``int()`` for the
        level (decimal) and row/col (hex).
        """
        base = os.fspath(all_layers_dir)
        prefix = base.rstrip(os.sep) + os.sep
        infos: list[TileInfo] = []

        for fp in file_paths:
            fp = os.fspath(fp)
            if fp.startswith(prefix):
                rel = fp[len(prefix) :]
            else:
                rel = os.path.relpath(fp, base)
            parts = rel.split(os.sep)
            if len(parts) != 3:
                raise ValueError(
                    f"Expected <L##>/<Rhex>/<Chex>.png relative to all_layers_dir, "
                    f"got: {rel}"
                )
            level_str, row_str, col_file = parts
            infos.append(
                cls(
                    path=Path(rel),
                    layer_number=int(level_str[1:]),
                    row_number=int(row_str[1:], 16),
                    col_number=int(os.path.splitext(col_file)[0][1:], 16),
                )
            )

        return infos


@dataclass
class BoundedTileInfo:
    """A :class:`TileInfo` paired with its computed WGS84 :class:`Bounds`.

    Produced by :meth:`TileBoundsCalculator.bounds_for_all` and consumed by
    the zip/sidecar processing in :mod:`terrain_stitcher.arcgis.tile_zip`.
    Bundling the two keeps the (tile, bounds) pair from drifting out of sync
    as it flows through the parallel processing pipeline -- instead of two
    parallel lists that must be kept length-aligned by hand, a single list
    of these dataclasses carries each tile together with its own bounds.
    """

    tile: "TileInfo"
    bounds: "Bounds"
