"""Setup-only, cached source downloads. Never imported by request handlers."""

import hashlib
import gzip
import json
import time
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


OVERPASS_URL = "https://overpass.openstreetmap.fr/api/interpreter"
OSM_QUERY = '[out:json][timeout:60];node["amenity"="fuel"](24,-125,50,-66);out;'
CENSUS_URL = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/"
    "Generalized_ACS2024/State_County/MapServer/7/query?"
    + urlencode({"where": "1=1", "outFields": "STUSAB,NAME", "outSR": 4326,
                 "returnGeometry": "true", "f": "geojson"})
)
MAX_BYTES = 100 * 1024 * 1024


def build_osm_tiles(south: float = 24.0, west: float = -125.0,
                    north: float = 50.0, east: float = -66.0,
                    step: float = 4.0) -> tuple[tuple[float, float, float, float], ...]:
    tiles = []
    lat = south
    while lat < north - 1e-9:
        next_lat = min(lat + step, north)
        lon = west
        while lon < east - 1e-9:
            next_lon = min(lon + step, east)
            tiles.append((lat, lon, next_lat, next_lon))
            lon = next_lon
        lat = next_lat
    return tuple(tiles)


OSM_TILES = build_osm_tiles()


class SourceError(ValueError):
    pass


def read_json(path: Path) -> tuple[dict, str]:
    raw = path.read_bytes()
    if path.suffix == ".gz":
        raw = gzip.decompress(raw)
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)


@contextmanager
def source_lock(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "download.lock"
    try:
        handle = path.open("x")
    except FileExistsError as exc:
        raise SourceError("Another source download is active; remove download.lock only after confirming it stopped.") from exc
    try:
        with handle:
            yield
    finally:
        path.unlink(missing_ok=True)


def validate_source(document: dict, kind: str, *, allow_empty: bool = False) -> None:
    if not isinstance(document, dict) or document.get("error") or document.get("remark"):
        raise SourceError(f"{kind} returned an error or an incomplete result")
    field = "elements" if kind == "osm" else "features"
    if not isinstance(document.get(field), list):
        raise SourceError(f"{kind} contains no {field}")
    if not document[field] and not allow_empty:
        raise SourceError(f"{kind} contains no {field}")
    if document.get("exceededTransferLimit") or document.get("properties", {}).get("exceededTransferLimit"):
        raise SourceError("Census response exceeded its transfer limit")


def download_source(directory: Path, kind: str, endpoint: str = OVERPASS_URL,
                    query: str = OSM_QUERY, target_name: str | None = None,
                    allow_empty: bool = False) -> Path:
    """One attempt per invocation; keep a persistent cooldown even after failure."""
    target = directory / (target_name or ("osm-fuel.json" if kind == "osm" else "us-states.geojson"))
    if target.exists():
        validate_source(read_json(target)[0], kind, allow_empty=allow_empty)
        return target
    with source_lock(directory):
        if target.exists():
            return target
        throttle = directory / f"{kind}-request.json"
        last = read_json(throttle)[0] if throttle.exists() else {}
        remaining = last.get("next_request_at", 0) - time.time()
        if remaining > 0:
            raise SourceError(f"{kind} cooldown: retry after {int(remaining) + 1} seconds")
        write_json(throttle, {"next_request_at": time.time() + 60})
        request = Request(
            endpoint if kind == "osm" else CENSUS_URL,
            data=urlencode({"data": query}).encode() if kind == "osm" else None,
            headers={"User-Agent": "Spotter-StationSetup/1.0 (offline fuel-station assessment)",
                     "Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urlopen(request, timeout=240) as response:
                raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise SourceError("Source download exceeds the 100 MiB limit")
            document = json.loads(raw)
            validate_source(document, kind, allow_empty=allow_empty)
        except HTTPError as exc:
            retry_after = exc.headers.get("Retry-After", "60") if exc.headers else "60"
            try:
                delay = max(60, int(retry_after))
            except ValueError:
                try:
                    delay = max(60, int(parsedate_to_datetime(retry_after).timestamp() - time.time()) + 1)
                except (ValueError, TypeError, OverflowError):
                    delay = 3600
            write_json(throttle, {"next_request_at": time.time() + delay})
            raise SourceError(f"{kind} HTTP {exc.code}; retry after {delay} seconds") from exc
        except SourceError:
            write_json(throttle, {"next_request_at": time.time() + 60})
            raise
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            write_json(throttle, {"next_request_at": time.time() + 60})
            raise SourceError(f"{kind} download failed; no partial cache saved ({type(exc).__name__})") from exc
        write_json(target, document)
        manifest_name = f"{target.stem}-manifest.json" if target_name else f"{kind}-manifest.json"
        write_json(directory / manifest_name, {
            "url": request.full_url, "query": query if kind == "osm" else None,
            "downloaded_at_unix": time.time(), "sha256": read_json(target)[1],
            "attribution": "© OpenStreetMap contributors, ODbL 1.0" if kind == "osm" else "Source: U.S. Census Bureau, 2024",
        })
        write_json(throttle, {"next_request_at": time.time() + 5})
    return target


def download_osm_snapshot(directory: Path, endpoint: str = OVERPASS_URL) -> Path:
    """Sequential geographic extracts; publish only a complete snapshot."""
    target = directory / "osm-fuel.json"
    if target.exists():
        validate_source(read_json(target)[0], "osm")
        return target
    elements, parts = {}, []
    for index, bounds in enumerate(OSM_TILES):
        filename = f"osm-tile-{index}.json"
        tile = directory / filename
        if not tile.exists():
            throttle = directory / "osm-request.json"
            remaining = read_json(throttle)[0].get("next_request_at", 0) - time.time() if throttle.exists() else 0
            if 0 < remaining <= 5:
                time.sleep(remaining)
        query = (
            f'[out:json][timeout:60];'
            f'node["amenity"="fuel"]({",".join(map(str, bounds))});out;'
        )
        path = download_source(directory, "osm", endpoint, query, filename, allow_empty=True)
        document, sha256 = read_json(path)
        parts.append({"bounds": bounds, "sha256": sha256, "osm3s": document.get("osm3s", {}), "query": query})
        for element in document["elements"]:
            elements[(element["type"], element["id"])] = element
    merged = {
        "version": 0.6, "source_parts": parts,
        "scope": {
            "type": "contiguous_us_fuel_nodes",
            "bounds": [24, -125, 50, -66],
            "tile_count": len(OSM_TILES),
            "tile_step_degrees": 4.0,
        },
        "attribution": "© OpenStreetMap contributors; ODbL 1.0; https://www.openstreetmap.org/copyright",
        "elements": [elements[key] for key in sorted(elements)],
    }
    validate_source(merged, "osm")
    write_json(target, merged)
    write_json(directory / "osm-manifest.json", {
        "url": endpoint, "source_parts": parts, "sha256": read_json(target)[1],
        "attribution": "© OpenStreetMap contributors; ODbL 1.0",
    })
    return target
