"""Argument parsers and dispatchers for the WeatherCams workflow."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import requests

from terrain_stitcher.weathercams.client import (
    FAA_WEATHERCAMS_SITES_URL,
    fetch_weathercam_sites,
)
from terrain_stitcher.weathercams.ledger import (
    complete_site_ids,
    export_weathercam_ledger,
    import_weathercam_ledger,
    load_weathercam_ledger,
    mark_site_complete,
    mark_site_pending,
    pending_site_ids,
    summarize_weathercam_ledger,
    synchronize_weathercam_ledger,
)
from terrain_stitcher.weathercams.pipeline import (
    ProcessTerrainOptions,
    run_weathercams,
)
from terrain_stitcher.weathercams.shapes import prepare_weathercam_shapes

DEFAULT_LEDGER_PATH = Path("weathercams/ledger.json")
DEFAULT_SHAPE_DIR = Path("weathercams/shapes")
DEFAULT_OUTPUT_ROOT = Path("weathercams/outputs")
DEFAULT_EXPORT_PATH = Path("weathercams/exports/complete.json")


def add_weathercams_subcommands(subparsers: argparse._SubParsersAction) -> None:
    _add_sync_command(subparsers)
    _add_prepare_command(subparsers)
    _add_run_command(subparsers)
    _add_complete_command(subparsers)
    _add_export_command(subparsers)
    _add_import_command(subparsers)
    _add_status_command(subparsers)
    _add_retry_command(subparsers)


def handle_weathercams_command(args: argparse.Namespace) -> bool:
    """Dispatch a WeatherCams command. Returns False for other commands."""
    if args.command == "weathercams-sync":
        _run_sync_command(args)
    elif args.command == "weathercams-prepare":
        _run_prepare_command(args)
    elif args.command == "weathercams-run":
        _run_run_command(args)
    elif args.command == "weathercams-complete":
        _run_complete_command(args)
    elif args.command == "weathercams-export":
        _run_export_command(args)
    elif args.command == "weathercams-import":
        _run_import_command(args)
    elif args.command == "weathercams-status":
        _run_status_command(args)
    elif args.command == "weathercams-retry":
        _run_retry_command(args)
    else:
        return False
    return True


def _add_sync_command(subparsers: argparse._SubParsersAction) -> None:
    command = subparsers.add_parser(
        "weathercams-sync",
        help="Create or update the minimal WeatherCams completion ledger",
        description=(
            "Fetch WeatherCams sites from the FAA API and write a local JSON "
            "ledger mapping site IDs to boolean completion flags. Existing "
            "true values are preserved and new sites are added as false."
        ),
    )
    command.add_argument(
        "--state",
        default="AK",
        help="WeatherCams state code or Alaska name (default: AK)",
    )
    command.add_argument(
        "--ledger",
        type=Path,
        default=DEFAULT_LEDGER_PATH,
        help="Ledger JSON path (default: weathercams/ledger.json)",
    )
    command.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="FAA API request timeout in seconds (default: 30)",
    )


def _add_prepare_command(subparsers: argparse._SubParsersAction) -> None:
    command = subparsers.add_parser(
        "weathercams-prepare",
        help="Create Shape.json files for incomplete WeatherCams sites",
        description=(
            "Fetch WeatherCams coordinates from the FAA API and create one "
            "Shape.json file per incomplete site. Sites already marked true in "
            "the ledger are skipped."
        ),
    )
    command.add_argument(
        "--ledger",
        type=Path,
        default=DEFAULT_LEDGER_PATH,
        help="Ledger JSON path (default: weathercams/ledger.json)",
    )
    command.add_argument(
        "--shape-dir",
        type=Path,
        default=DEFAULT_SHAPE_DIR,
        help="Directory for per-site Shape.json files (default: weathercams/shapes)",
    )
    command.add_argument(
        "-vd",
        "--view-distance",
        type=float,
        default=10.0,
        help="View distance in miles around each webcam (default: 10)",
    )
    command.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of incomplete sites to prepare (default: all)",
    )
    command.add_argument(
        "--state",
        default="AK",
        help="WeatherCams state code or Alaska name (default: AK)",
    )
    command.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="FAA API request timeout in seconds (default: 30)",
    )


def _add_run_command(subparsers: argparse._SubParsersAction) -> None:
    command = subparsers.add_parser(
        "weathercams-run",
        help="Run terrain passes for incomplete WeatherCams sites",
        description=(
            "Process every incomplete WeatherCams site through the existing "
            "process-terrain pipeline. Each successful site is immediately "
            "marked true in the local JSON ledger."
        ),
    )
    command.add_argument(
        "--ledger",
        type=Path,
        default=DEFAULT_LEDGER_PATH,
        help="Ledger JSON path (default: weathercams/ledger.json)",
    )
    command.add_argument(
        "--shape-dir",
        type=Path,
        default=DEFAULT_SHAPE_DIR,
        help=(
            "Directory containing per-site Shape.json files "
            "(default: weathercams/shapes)"
        ),
    )
    command.add_argument(
        "-o",
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="Root directory for terrain outputs (default: weathercams/outputs)",
    )
    command.add_argument(
        "--name-prefix",
        default="weathercam",
        help="Prefix used for process-terrain output names (default: weathercam)",
    )
    command.add_argument(
        "-d",
        "--dimension",
        type=int,
        required=True,
        help="Tiles per output image side; must be >= 2",
    )
    command.add_argument(
        "--lod", type=int, required=True, help="Target LOD to download"
    )
    command.add_argument(
        "--with-elevation",
        action="store_true",
        help="Also download and merge elevation for each site",
    )
    command.add_argument(
        "--keep-tiles",
        action="store_true",
        help="Keep intermediate tile pyramids for inspection or manual re-runs",
    )
    command.add_argument(
        "-f",
        "--scale-factor",
        type=float,
        default=1.0,
        help="Downscale factor for gathered images (default: 1.0)",
    )
    command.add_argument(
        "-w",
        "--workers",
        type=int,
        default=32,
        help="Download worker count (default: 32)",
    )
    command.add_argument(
        "--processes",
        type=int,
        default=32,
        help="gdal2tiles process count (default: 32)",
    )
    command.add_argument(
        "--gather-workers",
        type=int,
        default=None,
        help="Gather worker count (default: os.cpu_count())",
    )
    command.add_argument(
        "--chunk-px",
        type=int,
        default=256,
        help="ArcGIS export chunk size in pixels (default: 256)",
    )
    command.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="ArcGIS request timeout in seconds (default: 30)",
    )
    command.add_argument(
        "--resampling",
        default="lanczos",
        help="Resampling method forwarded to gdal2tiles (default: lanczos)",
    )
    command.add_argument(
        "--service-index",
        type=int,
        default=None,
        help="Explicit ArcGIS service index when multiple services cover an AOI",
    )
    command.add_argument(
        "--only-site",
        type=int,
        default=None,
        help="Process only this site ID, if it is still incomplete",
    )
    command.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of incomplete sites to process in this run",
    )
    command.add_argument(
        "--stop-on-error",
        action="store_true",
        help="Stop the batch immediately when a site fails",
    )


def _add_complete_command(subparsers: argparse._SubParsersAction) -> None:
    command = subparsers.add_parser(
        "weathercams-complete",
        help="Manually mark one WeatherCams site complete",
        description="Set a site's completion flag to true in the local JSON ledger.",
    )
    command.add_argument(
        "--ledger",
        type=Path,
        default=DEFAULT_LEDGER_PATH,
        help="Ledger JSON path (default: weathercams/ledger.json)",
    )
    command.add_argument("--site", type=int, required=True, help="WeatherCams site ID")


def _add_export_command(subparsers: argparse._SubParsersAction) -> None:
    command = subparsers.add_parser(
        "weathercams-export",
        help="Export completed WeatherCams sites for another machine",
        description=(
            "Write a portable JSON ledger containing completed site IDs. By "
            "default only true entries are exported."
        ),
    )
    command.add_argument(
        "--ledger",
        type=Path,
        default=DEFAULT_LEDGER_PATH,
        help="Ledger JSON path (default: weathercams/ledger.json)",
    )
    command.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_EXPORT_PATH,
        help="Export JSON path (default: weathercams/exports/complete.json)",
    )
    command.add_argument(
        "--all",
        action="store_true",
        help="Export false entries too; import still applies only true values",
    )


def _add_import_command(subparsers: argparse._SubParsersAction) -> None:
    command = subparsers.add_parser(
        "weathercams-import",
        help="Import completed WeatherCams sites from another machine",
        description=(
            "Read an exported WeatherCams ledger and mark every completed site "
            "true in the local ledger. Pending entries in the export are ignored."
        ),
    )
    command.add_argument(
        "--ledger",
        type=Path,
        default=DEFAULT_LEDGER_PATH,
        help="Ledger JSON path (default: weathercams/ledger.json)",
    )
    command.add_argument(
        "--input",
        type=Path,
        required=True,
        help="Exported WeatherCams ledger JSON path",
    )


def _add_status_command(subparsers: argparse._SubParsersAction) -> None:
    command = subparsers.add_parser(
        "weathercams-status",
        help="Show WeatherCams completion counts",
        description="Read the local JSON ledger and print completion counts.",
    )
    command.add_argument(
        "--ledger",
        type=Path,
        default=DEFAULT_LEDGER_PATH,
        help="Ledger JSON path (default: weathercams/ledger.json)",
    )
    command.add_argument(
        "--pending",
        action="store_true",
        help="Print pending site IDs instead of the summary",
    )
    command.add_argument(
        "--complete",
        action="store_true",
        help="Print completed site IDs instead of the summary",
    )
    command.add_argument(
        "--site", type=int, default=None, help="Show one site's status"
    )


def _add_retry_command(subparsers: argparse._SubParsersAction) -> None:
    command = subparsers.add_parser(
        "weathercams-retry",
        help="Mark one WeatherCams site incomplete",
        description="Set a site's completion flag back to false in the local ledger.",
    )
    command.add_argument(
        "--ledger",
        type=Path,
        default=DEFAULT_LEDGER_PATH,
        help="Ledger JSON path (default: weathercams/ledger.json)",
    )
    command.add_argument("--site", type=int, required=True, help="WeatherCams site ID")


def _get_weathercam_sites(args: argparse.Namespace):
    try:
        return fetch_weathercam_sites(
            args.state,
            timeout=args.timeout,
        )
    except requests.RequestException as exc:
        print(
            f"Unable to reach the FAA WeatherCams API ({FAA_WEATHERCAMS_SITES_URL}): {exc}",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc


def _run_sync_command(args: argparse.Namespace) -> None:
    sites = _get_weathercam_sites(args)
    result = synchronize_weathercam_ledger(
        args.ledger,
        (site.site_id for site in sites),
    )
    print(
        f"WeatherCams ledger synchronized: {result.total_sites} site(s), "
        f"{result.added_sites} added, {result.preserved_sites} preserved."
    )


def _run_prepare_command(args: argparse.Namespace) -> None:
    sites = _get_weathercam_sites(args)
    result = prepare_weathercam_shapes(
        sites,
        ledger_path=args.ledger,
        shape_dir=args.shape_dir,
        view_distance=args.view_distance,
        limit=args.limit,
    )
    print(
        f"WeatherCams shapes prepared: {len(result.created_site_ids)} created, "
        f"{len(result.skipped_site_ids)} skipped as complete."
    )


def _run_run_command(args: argparse.Namespace) -> None:
    options = ProcessTerrainOptions(
        dimension=args.dimension,
        lod=args.lod,
        with_elevation=args.with_elevation,
        keep_tiles=args.keep_tiles,
        scale_factor=args.scale_factor,
        workers=args.workers,
        chunk_px=args.chunk_px,
        timeout=args.timeout,
        resampling=args.resampling,
        service_index=args.service_index,
    )
    result = run_weathercams(
        ledger_path=args.ledger,
        shape_dir=args.shape_dir,
        output_root=args.output_root,
        options=options,
        name_prefix=args.name_prefix,
        only_site=args.only_site,
        limit=args.limit,
        stop_on_error=args.stop_on_error,
    )
    print(
        f"WeatherCams run complete: {len(result.processed_site_ids)} processed, "
        f"{len(result.failed_site_ids)} failed."
    )
    if result.failed_site_ids:
        failed_list = ", ".join(str(site_id) for site_id in result.failed_site_ids)
        print(f"Failed site IDs: {failed_list}")


def _run_complete_command(args: argparse.Namespace) -> None:
    mark_site_complete(args.ledger, args.site)
    print(f"WeatherCams site {args.site} marked complete.")


def _run_export_command(args: argparse.Namespace) -> None:
    count = export_weathercam_ledger(
        args.ledger,
        args.out,
        include_pending=args.all,
    )
    print(f"Exported {count} WeatherCams ledger entries to {args.out}.")


def _run_import_command(args: argparse.Namespace) -> None:
    imported_ledger = load_weathercam_ledger(args.input)
    result = import_weathercam_ledger(args.ledger, imported_ledger)
    print(
        f"Imported WeatherCams completions: {result.marked_complete} marked complete, "
        f"{result.already_complete} already complete."
    )


def _run_status_command(args: argparse.Namespace) -> None:
    ledger = load_weathercam_ledger(args.ledger)

    if args.site is not None:
        key = str(args.site)
        if key not in ledger:
            raise ValueError(f"Unknown WeatherCams site ID {args.site}")
        status = "complete" if ledger[key] else "pending"
        print(f"WeatherCams site {args.site}: {status}")
        return

    if args.pending and args.complete:
        raise ValueError("Choose either --pending or --complete, not both")
    if args.pending:
        pending = pending_site_ids(ledger)
        print(
            "\n".join(str(site_id) for site_id in pending)
            if pending
            else "No pending sites."
        )
        return
    if args.complete:
        complete = complete_site_ids(ledger)
        print(
            "\n".join(str(site_id) for site_id in complete)
            if complete
            else "No completed sites."
        )
        return

    summary = summarize_weathercam_ledger(ledger)
    print("WeatherCams ledger status")
    print(f"Total:     {summary.total_sites}")
    print(f"Complete:  {summary.complete_sites}")
    print(f"Pending:   {summary.pending_sites}")


def _run_retry_command(args: argparse.Namespace) -> None:
    mark_site_pending(args.ledger, args.site)
    print(f"WeatherCams site {args.site} marked pending.")
