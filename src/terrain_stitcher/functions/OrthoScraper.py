from terrain_stitcher.sources.acquisition import get_acquisition_source


def main(shapeFile, outputDir, source="usgs", inputDir=None):
    """Dispatch orthoimagery acquisition to the requested source.

    Defaults to the USGS API path to preserve existing behaviour when the
    ``--source`` CLI flag is omitted.
    """
    get_acquisition_source(source).acquire(shapeFile, outputDir, inputDir)
