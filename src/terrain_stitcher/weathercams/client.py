"""FAA WeatherCams API client.

The WeatherCams map is a JavaScript application, but the site information it
renders is available as JSON. These helpers intentionally keep only the fields
needed to create terrain AOIs; the persistent ledger stores only site IDs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

FAA_WEATHERCAMS_SITES_URL = "https://weathercams.faa.gov/api/sites"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30

_ALASKA_STATE_VALUES = {"AK", "ALASKA"}

_REQUEST_HEADERS = {
    "Accept": "application/json",
    "Referer": "https://weathercams.faa.gov/",
    "User-Agent": "Terrain-Stitcher/0.2.8",
}


@dataclass(frozen=True)
class WeathercamSite:
    """The minimal WeatherCams record needed to create a Shape.json file."""

    site_id: int
    latitude: float
    longitude: float
    state: str


def fetch_weathercam_sites(
    state: str = "AK",
    *,
    session: requests.Session | None = None,
    timeout: int = DEFAULT_REQUEST_TIMEOUT_SECONDS,
) -> list[WeathercamSite]:
    """Fetch WeatherCams sites from the FAA API and filter them by state."""
    response = _get_sites_response(session=session, timeout=timeout)
    return _sites_from_response(response, state=state)


def fetch_alaska_weathercam_sites(
    *,
    session: requests.Session | None = None,
    timeout: int = DEFAULT_REQUEST_TIMEOUT_SECONDS,
) -> list[WeathercamSite]:
    """Fetch every FAA WeatherCams site located in Alaska."""
    return fetch_weathercam_sites("AK", session=session, timeout=timeout)


def _get_sites_response(
    *, session: requests.Session | None, timeout: int
) -> dict[str, Any] | list[Any]:
    active_session = session if session is not None else requests.Session()
    try:
        response = active_session.get(
            FAA_WEATHERCAMS_SITES_URL,
            headers=_REQUEST_HEADERS,
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()
    finally:
        if session is None:
            active_session.close()


def _extract_site_records(
    payload: dict[str, Any] | list[Any],
) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        records = None
        for key in ("payload", "sites", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                records = value
                break
        if records is None:
            raise ValueError(
                "Unexpected WeatherCams API response: no site list was found"
            )
    else:
        raise ValueError("Unexpected WeatherCams API response: expected JSON")

    if not all(isinstance(record, dict) for record in records):
        raise ValueError(
            "Unexpected WeatherCams API response: site records must be objects"
        )
    return records


def _sites_from_response(response: object, *, state: str) -> list[WeathercamSite]:
    """Parse and state-filter one FAA API response."""
    site_records = _extract_site_records(response)
    requested_state = _normalize_state(state)

    return [
        _parse_site(record)
        for record in site_records
        if _states_match(_record_state(record), requested_state)
    ]


def _states_match(record_state: str, requested_state: str) -> bool:
    """Match a state code/name, treating AK and Alaska as equivalent."""
    normalized_record_state = _normalize_state(record_state)
    if requested_state in _ALASKA_STATE_VALUES:
        return normalized_record_state in _ALASKA_STATE_VALUES
    return normalized_record_state == requested_state


def _record_state(record: dict[str, Any]) -> str:
    for key in ("state", "stateCode", "stateAbbreviation"):
        value = record.get(key)
        if value is not None:
            return str(value)
    return ""


def _parse_site(record: dict[str, Any]) -> WeathercamSite:
    site_id = _parse_site_id(record.get("siteId", record.get("id")))
    latitude = _parse_coordinate(record, "latitude")
    longitude = _parse_coordinate(record, "longitude")
    state = _normalize_state(_record_state(record))

    if not -90.0 <= latitude <= 90.0:
        raise ValueError(
            f"WeatherCam site {site_id} has an invalid latitude: {latitude}"
        )
    if not -180.0 <= longitude <= 180.0:
        raise ValueError(
            f"WeatherCam site {site_id} has an invalid longitude: {longitude}"
        )

    return WeathercamSite(
        site_id=site_id,
        latitude=latitude,
        longitude=longitude,
        state=state,
    )


def _parse_site_id(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("WeatherCams site ID must be an integer")
    try:
        site_id = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"WeatherCams site ID is missing or invalid: {value!r}"
        ) from exc
    if site_id <= 0:
        raise ValueError(f"WeatherCams site ID must be positive: {site_id}")
    return site_id


def _parse_coordinate(record: dict[str, Any], key: str) -> float:
    value = record.get(key)
    if value is None:
        raise ValueError(f"WeatherCams site is missing {key}")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        site_id = record.get("siteId", record.get("id"))
        raise ValueError(
            f"WeatherCams {key} is invalid for site {site_id}: {value!r}"
        ) from exc


def _normalize_state(value: Any) -> str:
    return str(value or "").strip().upper()
