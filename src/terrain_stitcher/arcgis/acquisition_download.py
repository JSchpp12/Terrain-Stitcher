from terrain_stitcher.sources.acquisition import AcquisitionSource

class ArcGisDownloadAcquisitonSource(AcquisitionSource):
    """
    Acquisition source for ArcGIS Pro exploded cache downloads.
    """

    def __init__(self):
        super().__init__("arcgis_import")