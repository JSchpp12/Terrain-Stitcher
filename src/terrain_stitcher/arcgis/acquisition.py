from __future__ import annotations

import os
from typing import Optional

from terrain_stitcher.sources.acquisition import AcquisitionSource
from terrain_stitcher.arcgis.cache_xml import ArcGisCacheInfo, LevelOfDetailInfo

CONF_XML = "conf.xml"


class ArcGisProAcquisitionSource(AcquisitionSource):
    """Acquisition source for orthoimagery exported from ArcGISPro.

    The cache layout is the standard Esri exploded tile cache: a directory
    containing ``conf.xml`` plus per-level tile folders. The instance is
    built from the cache's ``conf.xml`` and exposes the parsed
    :class:`LevelOfDetailInfo` list via ``self.levels``.

    Tile gathering / sidecar emission is not implemented yet; only the
    cache metadata (XML) parsing is wired up at this stage.
    """

    def __init__(self, cache_xml_path: Optional[str] = None) -> None:
        self._cache_info: Optional[ArcGisCacheInfo] = None
        self.levels: list[LevelOfDetailInfo] = []
        if cache_xml_path is not None:
            self._load_from_xml(cache_xml_path)

    def _load_from_xml(self, cache_xml_path: str) -> None:
        self._cache_info = ArcGisCacheInfo.from_xml(cache_xml_path)
        self.levels = list(self._cache_info.levels)

    @property
    def cache_info(self) -> Optional[ArcGisCacheInfo]:
        return self._cache_info

    @classmethod
    def from_cache_dir(cls, cache_dir: str) -> "ArcGisProAcquisitionSource":
        """Construct from a cache directory by reading its ``conf.xml``."""
        xml_path = os.path.join(cache_dir, CONF_XML)
        if not os.path.isfile(xml_path):
            raise FileNotFoundError(
                f"ArcGIS cache directory missing conf.xml: {cache_dir}"
            )
        return cls(xml_path)

    def acquire(
        self,
        shape_file: str,
        output_dir: str,
        input_dir: Optional[str] = None,
    ) -> None:
        # Ensure cache metadata is loaded from the input directory.
        if self._cache_info is None:
            if input_dir is None:
                raise ValueError(
                    "ArcGISPro acquisition requires an input cache directory (-i/--input)"
                )
            self._load_from_xml(os.path.join(input_dir, CONF_XML))

        # TODO: walk per-level tile folders -> ImageFileInfo -> threadpool -> zips + sidecars
        raise NotImplementedError(
            "ArcGISPro tile gathering is not implemented yet. "
            f"Cache metadata parsed: {len(self.levels)} levels loaded."
        )