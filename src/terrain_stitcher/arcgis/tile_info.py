from __future__ import annotations

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
        file_path: str | Path,
        all_layers_dir: str | Path,
    ) -> TileInfo:
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
