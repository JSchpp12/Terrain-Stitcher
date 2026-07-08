from __future__ import annotations

import os
import numpy as np
from typing import List, Optional

from terrain_stitcher.arcgis.cache_xml import ArcGisCacheInfo, LevelOfDetailInfo
from terrain_stitcher.arcgis.tile_bounds import TileBoundsCalculator, TileFootprints
from terrain_stitcher.arcgis.tile_files import gather_tile_files
from terrain_stitcher.arcgis.tile_filter import ShapeTileFilter
from terrain_stitcher.arcgis.tile_info import BoundedTileInfo, TileInfo
from terrain_stitcher.arcgis.tile_zip import process_tiles
from terrain_stitcher.common import ParseArea
from terrain_stitcher.sources.acquisition import AcquisitionSource

CONF_XML = "conf.xml"
ALL_LAYERS_DIR = "_alllayers"


class ArcGisProAcquisitionSource(AcquisitionSource):
    """Acquisition source for orthoimagery exported from ArcGISPro.

    The cache layout is the standard Esri exploded tile cache: a directory
    containing ``conf.xml`` plus an ``_alllayers`` tile tree. The instance is
    built from the cache's ``conf.xml`` and exposes the parsed
    :class:`LevelOfDetailInfo` list via ``self.levels``.

    If a shape file is supplied to :meth:`acquire`, only tiles whose footprint
    overlaps the shape region are processed; otherwise every tile in the cache
    is emitted.
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

    def _build_tile_filter(
        self, shape_file: Optional[str]
    ) -> Optional[ShapeTileFilter]:
        """Build a callable tile filter from a shape file, or None to keep all."""
        if not shape_file:
            return None
        sPath = (
            shape_file
            if os.path.isabs(shape_file)
            else os.path.join(os.getcwd(), shape_file)
        )
        if not os.path.isfile(sPath):
            raise FileNotFoundError(f"Shape file not found: {sPath}")
        region = ParseArea.fromJSONFile(sPath).getTotalRegion()
        return ShapeTileFilter(self.cache_info, region)

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

        all_layers_dir = os.path.join(str(input_dir), ALL_LAYERS_DIR)

        tiles: list[str] = gather_tile_files(
            all_layers_dir, self.cache_info.cache_tile_format
        )

        tile_infos: List[TileInfo] = TileInfo.from_paths(tiles, all_layers_dir)
        discovered_count = len(tile_infos)
        print(f"Discovered {discovered_count} tile(s) in the ArcGIS cache.")

        bounds_calculator = TileBoundsCalculator(self.cache_info)
        footprints: TileFootprints = bounds_calculator.projected_footprints(tile_infos)
        tile_filter = self._build_tile_filter(shape_file)
        if tile_filter is not None:
            keep : np.ndarray = tile_filter.mask(footprints)
            before_filter = len(tile_infos)
            tile_infos = [t for t, k in zip(tile_infos, keep) if k]
            footprints = TileFootprints(
                west_x=footprints.west_x[keep],
                east_x=footprints.east_x[keep],
                south_y=footprints.south_y[keep],
                north_y=footprints.north_y[keep],
            )
            remaining = len(tile_infos)
            filtered_out = before_filter - remaining
            print(
                f"Shape filter removed {filtered_out} tile(s) outside the region; "
                f"{remaining} tile(s) remain to be processed."
            )
        else:
            remaining = discovered_count
            print("No shape file supplied; keeping every discovered tile.")

        if remaining == 0:
            print("No tiles overlap the shape region; nothing to process.")
            return

        bounded_tiles: List[BoundedTileInfo] = bounds_calculator.bounds_for_all(
            tile_infos, footprints=footprints
        )

        # Zip + sidecar per tile, in parallel. Each BoundedTileInfo carries its
        # own (tile, bounds) pair, so a single aligned list flows into the
        # threadpool -- no parallel-list bookkeeping here.
        print(f"Processing {remaining} tile(s)...")
        process_tiles(bounded_tiles, all_layers_dir, output_dir, show_progress=True)
        print(f"Done: wrote {remaining} tile archive(s) + sidecar(s) to {output_dir}.")
