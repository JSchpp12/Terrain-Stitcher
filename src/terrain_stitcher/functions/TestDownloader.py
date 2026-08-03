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

    xmin, ymin, xmax, ymax = bbox_from_radius(args.lat, args.lon, args.radius_miles)
    print(f"AOI bbox (EPSG:3857): {xmin:.1f}, {ymin:.1f}, {xmax:.1f}, {ymax:.1f}")

    tmp_dir = Path("aoi_chunks")
    tmp_dir.mkdir(exist_ok=True)

    chunks = build_chunk_grid(xmin, ymin, xmax, ymax, args.chunk_px)

    print(f"Downloading {len(chunks)} chunks with {args.workers} workers...")
    chunk_paths = download_all_chunks(
        chunks, args.img_format, args.retries, args.timeout, args.workers, tmp_dir
    )

    if not chunk_paths:
        print("No chunks downloaded successfully - aborting.")
        sys.exit(1)

    print("Building mosaic...")
    mosaic_path = build_mosaic(chunk_paths, tmp_dir)

    print(f"Tiling (zoom {args.zoom})...")
    run_gdal2tiles(
        mosaic_path,
        args.outdir,
        args.zoom,
        args.xyz,
        args.resampling,
        args.processes,
        args.webviewer,
    )

    if not args.keep_temp:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    print(
        f"Done. Tiles written to: {args.outdir} ({'XYZ' if args.xyz else 'TMS'} numbering)"
    )


if __name__ == "__main__":
    main()
