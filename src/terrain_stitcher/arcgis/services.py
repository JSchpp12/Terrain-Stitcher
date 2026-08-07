from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP_NAME = "terrain-stitcher"
CACHE_FILENAME = "services.json"
SCAN_FILENAME = "services_to_scan.json"

# Valid capability kinds a registered ArcGIS raster service may declare. A
# service can carry one or both; ``"imagery"`` feeds download-arcgis and
# ``"elevation"`` feeds download-elevation.
KIND_IMAGERY = "imagery"
KIND_ELEVATION = "elevation"
DEFAULT_KINDS = (KIND_IMAGERY,)


@dataclass(frozen=True)
class ImageryService:
    """A single ArcGIS ImageServer raster layer (orthoimagery or elevation).

    ``coverage`` is a WGS84 bounding box stored as
    ``(min_lat, min_lon, max_lat, max_lon)``. It is used both to select the
    right layer for a given AOI (by center containment) and to validate that
    the whole AOI fits inside the layer's footprint.

    ``kinds`` lists the capabilities this layer supports -- any subset of
    ``["imagery", "elevation"]``. Imagery services are selected by
    ``download-arcgis``, elevation services by ``download-elevation``. It
    defaults to ``["imagery"]`` so legacy cache entries (written before this
    field existed) keep resolving as imagery-only.
    """

    key: str
    label: str
    base_url: str
    native_pixel_size_m: float
    srs: int
    coverage: tuple[float, float, float, float]
    kinds: list[str] = field(default_factory=lambda: list(DEFAULT_KINDS))

    @classmethod
    def from_dict(cls, d: dict) -> "ImageryService":
        cov = d["coverage"]
        kinds = d.get("kinds")
        if kinds is None:
            kinds = list(DEFAULT_KINDS)
        return cls(
            key=d["key"],
            label=d["label"],
            base_url=d["base_url"],
            native_pixel_size_m=float(d["native_pixel_size_m"]),
            srs=int(d["srs"]),
            coverage=(float(cov[0]), float(cov[1]), float(cov[2]), float(cov[3])),
            kinds=list(kinds),
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["coverage"] = list(self.coverage)
        return d

    def supports_kind(self, kind: str) -> bool:
        return kind in self.kinds

    def contains_point(self, lat: float, lon: float) -> bool:
        min_lat, min_lon, max_lat, max_lon = self.coverage
        return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon

    def contains_bbox(self, aoi: tuple[float, float, float, float]) -> bool:
        min_lat, min_lon, max_lat, max_lon = aoi
        c_min_lat, c_min_lon, c_max_lat, c_max_lon = self.coverage
        return (
            min_lat >= c_min_lat
            and max_lat <= c_max_lat
            and min_lon >= c_min_lon
            and max_lon <= c_max_lon
        )


def bbox_latlon_from_radius(
    lat: float, lon: float, radius_miles: float
) -> tuple[float, float, float, float]:
    """WGS84 bbox ``(min_lat, min_lon, max_lat, max_lon)`` for a radius in
    miles around ``lat/lon``. Mirrors ``OrthoDownloader.bbox_from_radius``
    but stays in lat/lon for service selection / coverage checks."""
    radius_m = float(radius_miles) * 1609.344
    meters_per_deg_lat = 111_320.0
    meters_per_deg_lon = 111_320.0 * math.cos(math.radians(lat))
    dlat = radius_m / meters_per_deg_lat
    dlon = radius_m / meters_per_deg_lon
    return (lat - dlat, lon - dlon, lat + dlat, lon + dlon)


class AmbiguousServiceError(ValueError):
    """Raised by :func:`select_service` when more than one registered
    service of the requested kind fully covers the AOI and no ``index`` was
    supplied to disambiguate. The ordered candidate list is attached as
    ``candidates`` so the caller can present it for selection."""

    def __init__(self, candidates, kind: str = "imagery", message: str = ""):
        self.candidates = list(candidates)
        self.kind = kind
        super().__init__(
            message
            or f"AOI is covered by multiple {kind} services; pass an index "
            "to select one. Candidates: " + ", ".join(s.key for s in self.candidates)
        )


def _kind_label(kind: str) -> str:
    return kind or "imagery"


def _covering_services(
    services: dict[str, ImageryService],
    aoi_bbox: tuple[float, float, float, float],
    kind: str = KIND_IMAGERY,
) -> list[ImageryService]:
    """Return the registered services of *kind* that fully cover the AOI,
    ordered by ``key`` for stable index-based selection.

    Only services whose ``kinds`` includes *kind* are considered. Raises
    ``ValueError`` if no such service contains the AOI center, or if the
    center is inside one or more of them but the whole AOI extends outside
    every one of them (i.e. the view distance is too large for any single
    service).
    """
    label = _kind_label(kind)
    candidates = [s for s in services.values() if s.supports_kind(kind)]
    if not candidates:
        raise ValueError(
            f"No {label} services are cached. Run `terrain-stitcher "
            "refresh-services` to build the registry from the shipped "
            "endpoint list (services_to_scan.json), then re-run your "
            "download command."
        )
    min_lat, min_lon, max_lat, max_lon = aoi_bbox
    cx_lat = (min_lat + max_lat) / 2.0
    cx_lon = (min_lon + max_lon) / 2.0

    hits = [s for s in candidates if s.contains_point(cx_lat, cx_lon)]
    if not hits:
        raise ValueError(
            f"AOI center ({cx_lat:.4f}, {cx_lon:.4f}) is not inside any "
            f"registered {label} service coverage. Registered {label} "
            f"services: " + ", ".join(s.key for s in candidates)
        )
    fully = [s for s in hits if s.contains_bbox(aoi_bbox)]
    if not fully:
        raise ValueError(
            f"AOI center ({cx_lat:.4f}, {cx_lon:.4f}) is inside "
            f"{', '.join(s.key for s in hits)}, but the AOI extends "
            f"outside every covering {label} service (AOI {aoi_bbox}). "
            f"Reduce the view distance or move the center so the whole AOI "
            f"fits inside a single service."
        )
    return sorted(fully, key=lambda s: s.key)


def select_service(
    services: dict[str, ImageryService],
    aoi_bbox: tuple[float, float, float, float],
    index: int | None = None,
    kind: str = KIND_IMAGERY,
) -> ImageryService:
    """Pick the service to download from for an AOI, restricted to those
    that support *kind* (``"imagery"`` or ``"elevation"``).

    Candidates are the services of that kind whose coverage fully contains
    the AOI (center inside the footprint and the whole AOI bbox inside it).
    When exactly one service qualifies it is returned. When more than one
    qualifies the caller must disambiguate: pass ``index`` (0-based, into
    the ordered candidate list) to pick one, or omit it to raise
    :class:`AmbiguousServiceError` carrying the candidates so the CLI can
    print them and ask the user to re-run with ``--service-index``.

    Raises ``ValueError`` if no service of that kind covers the AOI center,
    if the AOI extends outside every covering service, or if ``index`` is
    out of range.
    """
    candidates = _covering_services(services, aoi_bbox, kind=kind)
    if index is not None:
        if not (0 <= index < len(candidates)):
            raise ValueError(
                f"service index {index} is out of range; "
                f"{len(candidates)} service(s) cover this AOI "
                f"(valid indices 0..{len(candidates) - 1}): "
                + ", ".join(s.key for s in candidates)
            )
        return candidates[index]
    if len(candidates) == 1:
        return candidates[0]
    raise AmbiguousServiceError(candidates, kind=kind)


# --- Registry persistence ---------------------------------------------------


def _cache_path() -> Path | None:
    try:
        from platformdirs import user_cache_dir
    except ImportError:
        return None
    return Path(user_cache_dir(APP_NAME)) / CACHE_FILENAME


def _shipped_scan_items() -> list:
    """Read the endpoint scan list shipped with the package
    (``services_to_scan.json``) and return its array of entries."""
    from importlib.resources import files

    raw = files("terrain_stitcher.arcgis").joinpath(SCAN_FILENAME).read_bytes()
    data = json.loads(raw)
    if isinstance(data, dict):
        items = data.get("folders") or data.get("endpoints") or []
    else:
        items = data
    if not isinstance(items, list):
        raise ValueError(
            "shipped services_to_scan.json must contain a JSON array of "
            "endpoints (or an object with a 'folders' array)"
        )
    return items


def _parse_services(data: dict) -> dict[str, ImageryService]:
    out: dict[str, ImageryService] = {}
    for entry in data.get("services", []):
        svc = ImageryService.from_dict(entry)
        out[svc.key] = svc
    return out


def load_services() -> dict[str, ImageryService]:
    """Load the cached service registry (written by ``refresh-services``).

    The registry is generated at runtime from the shipped endpoint scan list
    (``services_to_scan.json``); it is not shipped pre-resolved. If no cache
    exists yet, an empty dict is returned and :func:`select_service` directs
    the user to run ``refresh-services``.
    """
    cache = _cache_path()
    if cache is not None and cache.is_file():
        try:
            data = json.loads(cache.read_bytes())
        except (json.JSONDecodeError, OSError):
            data = None
        if data is not None:
            return _parse_services(data)
    return {}


def save_services(data: dict, path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


# --- Live harvesting from the ArcGIS REST directory -------------------------


def _coverage_from_extent(extent) -> tuple[float, float, float, float]:
    from pyproj import Transformer

    if not extent:
        raise ValueError("service metadata has no fullExtent/extent")
    sr = extent.get("spatialReference", {}) or {}
    wkid = sr.get("latestWkid") or sr.get("wkid")
    if wkid == 102100:
        wkid = 3857
    transformer = Transformer.from_crs(f"EPSG:{wkid}", "EPSG:4326", always_xy=True)
    lats, lons = [], []
    for x in (extent["xmin"], extent["xmax"]):
        for y in (extent["ymin"], extent["ymax"]):
            lo, la = transformer.transform(x, y)
            lons.append(lo)
            lats.append(la)
    return (min(lats), min(lons), max(lats), max(lons))


def _native_pixel_size(meta: dict) -> float:
    """Best-effort native resolution (m/px) from service metadata.

    Tries, in order: the finest cached LOD resolution, ``minPixelSize``,
    then the mosaic cell size (``pixelSizeX`` / ``meanPixelSize``). A
    ``minPixelSize`` of 0 (common on mosaic datasets, meaning "no minimum
    constraint") is treated as absent. Raises ``ValueError`` only when none
    are present -- in which case the scan-list entry should declare
    ``pixel_size`` explicitly.
    """
    tile_info = meta.get("tileInfo")
    if tile_info and tile_info.get("lods"):
        return float(min(lod["resolution"] for lod in tile_info["lods"]))
    if meta.get("minPixelSize"):
        return float(meta["minPixelSize"])
    for key in ("pixelSizeX", "meanPixelSize"):
        if meta.get(key):
            return float(meta[key])
    raise ValueError(
        "service has no tileInfo.lods, minPixelSize, pixelSizeX, or "
        "meanPixelSize; native resolution could not be determined. "
        "Declare 'pixel_size' on the scan-list entry to register it."
    )


def _endpoint_url(item) -> str:
    """Normalize a scan-list entry to an ImageServer URL (strip a trailing
    ``/exportImage`` if present). Accepts a plain URL string or an object
    with a ``url`` key."""
    if isinstance(item, str):
        url = item
    elif isinstance(item, dict):
        url = item.get("url")
        if not url:
            raise ValueError(f"endpoint entry missing 'url': {item!r}")
    else:
        raise ValueError(
            f"unsupported endpoint entry type {type(item).__name__}: {item!r}"
        )
    url = url.rstrip("/")
    if url.endswith("/exportImage"):
        url = url[: -len("/exportImage")]
    return url


def _item_pixel_size(item) -> float | None:
    """Return the native pixel size (m/px) declared on a scan-list entry, if
    any. Only object entries may carry a ``pixel_size`` field; plain URL
    strings cannot, in which case ``None`` is returned and the size is
    derived from the service metadata instead."""
    if isinstance(item, dict):
        ps = item.get("pixel_size")
        if ps is not None:
            return float(ps)
    return None


def _item_kinds(item) -> list[str]:
    """Return the capability ``kinds`` declared on a scan-list entry, if any.

    Only object entries may carry a ``kinds`` list (e.g. ``["elevation"]``);
    plain URL strings (and objects that omit it) default to ``["imagery"]``.
    Unknown values are filtered out so a typo degrades to the default rather
    than registering a capability the downloaders don't understand.
    """
    valid = {KIND_IMAGERY, KIND_ELEVATION}
    if isinstance(item, dict):
        kinds = item.get("kinds")
        if kinds:
            return [k for k in kinds if k in valid] or list(DEFAULT_KINDS)
    return list(DEFAULT_KINDS)


def harvest_from_items(items, timeout: int = 30) -> dict:
    """Build a registry by fetching each ImageServer endpoint's metadata.

    ``items`` is an array of endpoint entries: URL strings (pointing at an
    ArcGIS ``.../ImageServer`` resource, with or without a trailing
    ``/exportImage``) or objects. An object must have a ``url`` key and may
    also declare ``pixel_size`` (m/px) and ``kinds`` (list of capability
    kinds, defaulting to ``["imagery"]``). When present, ``pixel_size`` is
    used as the service's native resolution instead of reading it from the
    metadata -- this lets uncached / mosaic services (which expose no
    ``tileInfo`` and a meaningless ``minPixelSize`` of 0, e.g. USGSNAIPPlus)
    be registered with their real cell size.

    Each entry's ``fullExtent`` (coverage) is always read from the metadata;
    only the resolution and capability kinds may be supplied statically. No
    folder crawling, so the scan list is exactly the set of services to
    register.
    """
    import requests

    services: list[dict] = []
    seen_keys: set[str] = set()
    endpoints: list[str] = []
    for item in items:
        base = _endpoint_url(item)
        endpoints.append(base)
        declared = _item_pixel_size(item)
        kinds = _item_kinds(item)
        meta = requests.get(base, params={"f": "json"}, timeout=timeout).json()
        extent = meta.get("fullExtent") or meta.get("extent")
        sr = (extent or {}).get("spatialReference", {}) or {}
        srs = int(sr.get("latestWkid") or sr.get("wkid") or 3857)
        # Derive a stable, server-unique key from the service path; the leaf
        # name is prettified for the human-readable label.
        stem = base
        if "://" in stem:
            stem = stem.split("://", 1)[1]
        key = stem.replace("/", "__")
        if key in seen_keys:
            print(f"Skipping duplicate service key {key} ({base})")
            continue
        seen_keys.add(key)
        leaf = stem.rsplit("/", 1)[-1]
        try:
            cov = _coverage_from_extent(extent)
            nps = declared if declared is not None else _native_pixel_size(meta)
        except ValueError as e:
            print(f"Skipping {base}: {e}")
            continue
        services.append(
            ImageryService(
                key=key,
                label=leaf.replace("_", " ").title(),
                base_url=f"{base}/exportImage",
                native_pixel_size_m=nps,
                srs=srs,
                coverage=cov,
                kinds=kinds,
            ).to_dict()
        )
    return {"services": services, "_endpoints": endpoints}


def harvest_from_file(path, timeout: int = 30) -> dict:
    """Build a registry from a JSON file containing an array of ImageServer
    endpoints to register. Each entry is a URL string or an object
    ``{"url": ...}`` (optionally with ``"pixel_size": <m/px>`` to declare
    the native resolution statically, and ``"kinds"`` to declare the
    capability kinds). A top-level object with a ``"folders"`` array is also
    accepted for compatibility.
    """
    data = json.loads(Path(path).read_bytes())
    if isinstance(data, dict):
        items = data.get("folders") or data.get("endpoints") or []
    else:
        items = data
    if not isinstance(items, list):
        raise ValueError(
            "services file must contain a JSON array of endpoints (or an "
            "object with a 'folders' array)"
        )
    return harvest_from_items(items, timeout=timeout)


def refresh_services(timeout: int = 30, from_file=None) -> Path:
    """Fetch each endpoint's metadata and write the resolved registry to the
    user cache dir so ``load_services`` picks it up on subsequent runs.

    With no arguments, the endpoints are read from the scan list shipped with
    the package (``services_to_scan.json``). Pass ``from_file`` to use a
    custom scan-list file instead.
    """
    if from_file:
        data = harvest_from_file(from_file, timeout=timeout)
    else:
        data = harvest_from_items(_shipped_scan_items(), timeout=timeout)
    cache = _cache_path()
    if cache is None:
        raise RuntimeError("platformdirs is not installed; cannot write a cache.")
    save_services(data, cache)
    print(
        f"Refreshed {len(data['services'])} service(s) -> {cache}\n"
        f"download-arcgis / download-elevation will use this cached registry."
    )
    return cache
