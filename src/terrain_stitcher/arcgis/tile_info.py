from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


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
    def from_xyz_path(
        cls,
        file_path: "str | Path",
        tiles_root_dir: "str | Path",
    ) -> "TileInfo":
        """Parse a single ``z/x/y.png`` (gdal2tiles / XYZ slippy-map) tile
        path into a :class:`TileInfo`.

        Unlike :meth:`from_path` (ArcGIS Pro's exploded ``L##/Rhex/Chex.png``
        cache layout), this expects the standard XYZ layout produced by
        gdal2tiles: ``<z>/<x>/<y>.png``, decimal, zoom-then-column-then-row.
        Assumes the same Web Mercator / 256px tile scheme as ArcGIS's default
        "ArcGIS Online/Bing/Google Maps" cache scheme, so level, row, and
        column line up directly with layer_number/row_number/col_number.
        """
        rel = Path(file_path).relative_to(Path(tiles_root_dir))
        parts = rel.parts
        if len(parts) != 3:
            raise ValueError(
                f"Expected <z>/<x>/<y>.png relative to tiles_root_dir, got: {rel}"
            )

        z_part, x_part, y_file = parts
        y_stem = Path(y_file).stem

        return cls(
            path=rel,
            layer_number=int(z_part),
            row_number=int(y_stem),
            col_number=int(x_part),
        )

    @classmethod
    def from_xyz_paths(
        cls,
        file_paths: list,
        tiles_root_dir: "str | Path",
    ) -> list["TileInfo"]:
        """Bulk version of :meth:`from_xyz_path`, mirroring the string-based
        fast path used in :meth:`from_paths`."""
        base = os.fspath(tiles_root_dir)
        prefix = base.rstrip(os.sep) + os.sep
        infos: list[TileInfo] = []

        for fp in file_paths:
            fp = os.fspath(fp)
            rel = (
                fp[len(prefix) :]
                if fp.startswith(prefix)
                else os.path.relpath(fp, base)
            )
            parts = rel.split(os.sep)
            if len(parts) != 3:
                raise ValueError(
                    f"Expected <z>/<x>/<y>.png relative to tiles_root_dir, got: {rel}"
                )
            z_part, x_part, y_file = parts
            infos.append(
                cls(
                    path=Path(rel),
                    layer_number=int(z_part),
                    row_number=int(os.path.splitext(y_file)[0]),
                    col_number=int(x_part),
                )
            )

        return infos

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


