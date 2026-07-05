from __future__ import annotations

from abc import ABC, abstractmethod


class AcquisitionSource(ABC):
    """Common contract for everything that can populate an output directory
    with orthoimagery archives + sidecar metadata for the prep phase.

    Implementations are responsible for producing, in ``output_dir``:
      * image archives consumable by the prep phase (e.g. ``<name>.zip``)
      * ``ImageDataWriter``-schema sidecar JSON files describing each image's
        geographic ``Bounds``.

    ``input_dir`` is only meaningful for sources that import from a local
    directory (e.g. ArcGISPro exports); API-backed sources such as USGS may
    ignore it.
    """

    @abstractmethod
    def acquire(
        self,
        shape_file: str,
        output_dir: str,
        input_dir: str | None = None,
    ) -> None:
        pass


def get_acquisition_source(name: str) -> AcquisitionSource:
    """Factory mapping a source name to its AcquisitionSource implementation.

    Imports are deferred so an unused source does not pull in its dependencies
    (e.g. the USGS API client is not imported when running the arcgis path).
    """
    if name == "usgs":
        from terrain_stitcher.usgs_acquisition import UsgsAcquisitionSource

        return UsgsAcquisitionSource()

    if name == "arcgis":
        from terrain_stitcher.arcgis.acquisition import ArcGisProAcquisitionSource

        return ArcGisProAcquisitionSource()

    raise ValueError(f"Unknown acquisition source: {name}")