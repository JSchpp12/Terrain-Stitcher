"""Minimal FAA WeatherCams batch workflow.

The workflow is split across dedicated modules:

* :mod:`terrain_stitcher.weathercams.client` fetches FAA site records.
* :mod:`terrain_stitcher.weathercams.ledger` manages the site-ID/boolean ledger.
* :mod:`terrain_stitcher.weathercams.shapes` writes per-site Shape.json files.
* :mod:`terrain_stitcher.weathercams.pipeline` runs the terrain passes.
* :mod:`terrain_stitcher.weathercams.cli` wires the commands into the main CLI.
"""

from terrain_stitcher.weathercams.client import (
    WeathercamSite,
    fetch_alaska_weathercam_sites,
    fetch_weathercam_sites,
)
from terrain_stitcher.weathercams.ledger import (
    LedgerImportResult,
    LedgerSummary,
    LedgerSyncResult,
    load_weathercam_ledger,
    save_weathercam_ledger,
)
from terrain_stitcher.weathercams.shapes import (
    ShapePreparationResult,
    prepare_weathercam_shapes,
)

__all__ = [
    "WeathercamSite",
    "fetch_alaska_weathercam_sites",
    "fetch_weathercam_sites",
    "LedgerImportResult",
    "LedgerSummary",
    "LedgerSyncResult",
    "load_weathercam_ledger",
    "save_weathercam_ledger",
    "ShapePreparationResult",
    "prepare_weathercam_shapes",
]
