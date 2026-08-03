from terrain_stitcher.usgs_acquisition import UsgsAcquisitionSource


def main(shapeFile, outputDir, inputDir=None, num_workers=None):
    """Dispatch orthoimagery acquisition to the requested source.

    Defaults to the USGS API path to preserve existing behaviour when the
    ``--source`` CLI flag is omitted.
    """
    UsgsAcquisitionSource().acquire(shapeFile, outputDir, inputDir, num_workers)
