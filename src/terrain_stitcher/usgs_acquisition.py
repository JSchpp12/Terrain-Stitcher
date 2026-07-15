from __future__ import annotations

import os

from terrain_stitcher.sources.acquisition import AcquisitionSource
from terrain_stitcher.usgs import HighResolutionOrthoImagery, Scraper
from terrain_stitcher.common import ParseArea


class UsgsAcquisitionSource(AcquisitionSource):
    """Acquisition source backed by the USGS Earth Explorer M2M API.

    This is a direct lift of the original ``functions/OrthoScraper.main``
    behaviour: build the high-res ortho data source, attach it to a scraper
    and run it against the shape region.
    """

    def acquire(
        self,
        shape_file: str,
        output_dir: str,
        input_dir: str | None = None,
        num_workers: int | None = None,
    ) -> None:
        sPath = os.path.join(os.getcwd(), shape_file)
        if not os.path.isfile(sPath):
            raise Exception("Shape file was not provided or does not exist")

        area = ParseArea.fromJSONFile(sPath)
        imageDataset = HighResolutionOrthoImagery("high_res_ortho")
        scraper = Scraper()
        scraper.add_parser(imageDataset)

        region = area.getTotalRegion()
        scraper.run(region, output_dir)
