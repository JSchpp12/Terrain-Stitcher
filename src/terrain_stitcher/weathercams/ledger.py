"""Minimal, machine-local WeatherCams progress ledger.

The ledger is intentionally a single JSON object mapping site IDs to boolean
completion flags. No site metadata, timestamps, or event history is stored.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping


@dataclass(frozen=True)
class LedgerSyncResult:
    total_sites: int
    added_sites: int
    preserved_sites: int


@dataclass(frozen=True)
class LedgerImportResult:
    marked_complete: int
    already_complete: int


@dataclass(frozen=True)
class LedgerSummary:
    total_sites: int
    complete_sites: int
    pending_sites: int


def load_weathercam_ledger(path: Path) -> dict[str, bool]:
    """Load and validate a WeatherCams ledger."""
    try:
        raw_ledger = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"WeatherCams ledger does not exist: {path}. Run weathercams-sync first."
        ) from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"WeatherCams ledger is not valid JSON: {path}") from exc
    return _validate_and_normalize_ledger(raw_ledger)


def save_weathercam_ledger(path: Path, ledger: Mapping[str, bool]) -> None:
    """Atomically write a ledger, sorted by numeric site ID."""
    normalized_ledger = _validate_and_normalize_ledger(dict(ledger))
    path.parent.mkdir(parents=True, exist_ok=True)

    temporary_path = path.with_name(f".{path.name}.tmp")
    temporary_path.write_text(
        json.dumps(normalized_ledger, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def synchronize_weathercam_ledger(
    path: Path, site_ids: Iterable[int]
) -> LedgerSyncResult:
    """Add new site IDs as incomplete while preserving existing completion flags.

    Sites that disappear from the FAA response remain in the ledger by default.
    This keeps completed work from being lost when the FAA temporarily changes
    its site list.
    """
    normalized_site_ids = {_parse_site_id(site_id) for site_id in site_ids}
    existing_ledger = load_weathercam_ledger(path) if path.exists() else {}

    existing_site_ids = {int(site_id) for site_id in existing_ledger}
    added_sites = len(normalized_site_ids - existing_site_ids)
    preserved_sites = len(normalized_site_ids & existing_site_ids)

    for site_id in normalized_site_ids:
        existing_ledger.setdefault(str(site_id), False)

    save_weathercam_ledger(path, existing_ledger)
    return LedgerSyncResult(
        total_sites=len(existing_ledger),
        added_sites=added_sites,
        preserved_sites=preserved_sites,
    )


def pending_site_ids(ledger: Mapping[str, bool]) -> list[int]:
    return sorted(int(site_id) for site_id, complete in ledger.items() if not complete)


def complete_site_ids(ledger: Mapping[str, bool]) -> list[int]:
    return sorted(int(site_id) for site_id, complete in ledger.items() if complete)


def mark_site_complete(path: Path, site_id: int) -> None:
    ledger = load_weathercam_ledger(path)
    normalized_site_id = _parse_site_id(site_id)
    key = str(normalized_site_id)
    if key not in ledger:
        raise ValueError(
            f"Unknown WeatherCams site ID {normalized_site_id}. "
            "Run weathercams-sync first."
        )
    ledger[key] = True
    save_weathercam_ledger(path, ledger)


def mark_site_pending(path: Path, site_id: int) -> None:
    ledger = load_weathercam_ledger(path)
    normalized_site_id = _parse_site_id(site_id)
    key = str(normalized_site_id)
    if key not in ledger:
        raise ValueError(
            f"Unknown WeatherCams site ID {normalized_site_id}. "
            "Run weathercams-sync first."
        )
    ledger[key] = False
    save_weathercam_ledger(path, ledger)


def export_weathercam_ledger(
    source_path: Path, destination_path: Path, *, include_pending: bool = False
) -> int:
    """Export completed sites, or the entire ledger with ``include_pending``."""
    ledger = load_weathercam_ledger(source_path)
    exported_ledger = (
        dict(ledger)
        if include_pending
        else {site_id: complete for site_id, complete in ledger.items() if complete}
    )
    save_weathercam_ledger(destination_path, exported_ledger)
    return len(exported_ledger)


def import_weathercam_ledger(
    path: Path, imported_ledger: Mapping[str, bool]
) -> LedgerImportResult:
    """Mark every completed site in an export as complete in the local ledger.

    Only ``true`` values are applied. This makes an import safe even if someone
    exports an entire ledger containing pending sites.
    """
    normalized_import = _validate_and_normalize_ledger(dict(imported_ledger))
    ledger = load_weathercam_ledger(path)

    marked_complete = 0
    already_complete = 0
    for site_id, complete in normalized_import.items():
        if not complete:
            continue
        if ledger.get(site_id):
            already_complete += 1
            continue
        ledger[site_id] = True
        marked_complete += 1

    save_weathercam_ledger(path, ledger)
    return LedgerImportResult(
        marked_complete=marked_complete,
        already_complete=already_complete,
    )


def summarize_weathercam_ledger(ledger: Mapping[str, bool]) -> LedgerSummary:
    complete_sites = complete_site_ids(ledger)
    return LedgerSummary(
        total_sites=len(ledger),
        complete_sites=len(complete_sites),
        pending_sites=len(ledger) - len(complete_sites),
    )


def _validate_and_normalize_ledger(raw_ledger: object) -> dict[str, bool]:
    if not isinstance(raw_ledger, dict):
        raise ValueError("WeatherCams ledger must be a JSON object")

    ledger: dict[str, bool] = {}
    for raw_site_id, complete in raw_ledger.items():
        site_id = _parse_site_id(raw_site_id)
        if not isinstance(complete, bool):
            raise ValueError(
                f"WeatherCams site {site_id} must have a boolean completion flag"
            )
        ledger[str(site_id)] = complete
    return ledger


def _parse_site_id(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("WeatherCams site ID must be an integer")
    try:
        site_id = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"WeatherCams site ID is invalid: {value!r}") from exc
    if site_id <= 0:
        raise ValueError(f"WeatherCams site ID must be positive: {site_id}")
    return site_id
