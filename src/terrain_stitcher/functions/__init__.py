from .OrthoScraper import main as main_ortho
from .ShapeGenerator import main as main_shape
from .OrthoPrep import main as main_prep_ortho
from .OrthoGather import main as main_stitch_ortho

try:
    from .ElevationTIFPrep import main as main_prep_elevation
except ImportError:
    # gdal/osgeo is a heavyweight dependency used only by the prep-ortho
    # elevation stage. Keep the package importable without it so other
    # commands (and tests) work in environments lacking GDAL bindings.
    main_prep_elevation = None
