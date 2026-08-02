#!/usr/bin/env python3
"""
Export imagery from an ArcGIS ImageServer within a mile radius of a point,
downloading in parallel chunks (with retries on transient server errors),
mosaicking them, and cutting the result into z/x/y tiles.

Requires:
    pip install pyproj requests
    GDAL command-line tools (gdal_translate, gdalbuildvrt) on PATH,
    plus the gdal2tiles module (ships with the GDAL Python bindings).

    conda install -c conda-forge gdal   # most reliable way to get all of the above

Usage:
    python export_imageserver_tiles.py --lat 55.9096 --lon -159.1595 --radius-miles 1 --zoom 10-17
"""







def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lon", type=float, required=True)
    parser.add_argument("--radius-miles", type=float, required=True)
    parser.add_argument("--zoom", default="10-17")
    parser.add_argument("--outdir", default="tiles_output")

    parser.add_argument(
        "--chunk-px",
        type=int,
        default=2048,
        help="chunk size in pixels per exportImage request (max ~4000 given service limits)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=6,
        help="concurrent download threads (keep modest - this is a public server)",
    )
    parser.add_argument(
        "--retries", type=int, default=5, help="retries per chunk on transient errors"
    )
    parser.add_argument(
        "--timeout", type=int, default=60, help="per-request timeout in seconds"
    )
    parser.add_argument(
        "--img-format", default="tiff", choices=["tiff", "png", "jpgpng"]
    )

    parser.add_argument("--xyz", action="store_true")
    parser.add_argument("--tms", dest="xyz", action="store_false")
    parser.set_defaults(xyz=True)
    parser.add_argument(
        "--resampling",
        default="average",
        choices=["average", "near", "bilinear", "cubic", "cubicspline", "lanczos"],
    )
    parser.add_argument("--processes", type=int, default=4)
    parser.add_argument(
        "--webviewer", default="none", choices=["none", "leaflet", "openlayers", "all"]
    )
    parser.add_argument("--keep-temp", action="store_true")
    args = parser.parse_args()



if __name__ == "__main__":
    main()
