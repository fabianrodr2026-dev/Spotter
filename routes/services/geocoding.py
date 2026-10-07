"""Conservative station resolution against an offline, versioned OSM snapshot."""

import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from decimal import Decimal
from itertools import islice

from django.db import transaction
from django.db.models import Count, QuerySet
from django.utils import timezone

from routes.models import FuelStation, StationGeocodeCache
from .state_boundaries import CONTIGUOUS_STATES, StateBoundaries, valid_coordinate


MATCHER_VERSION = "osm-address-name-v6"
SOURCE = "OpenStreetMap / Overpass"
ATTRIBUTION = "© OpenStreetMap contributors; ODbL 1.0; https://www.openstreetmap.org/copyright"
ROAD_WORDS = {
    "street": "st", "road": "rd", "avenue": "ave", "boulevard": "blvd",
    "drive": "dr", "lane": "ln", "highway": "hwy", "parkway": "pkwy",
    "court": "ct", "north": "n", "south": "s", "east": "e", "west": "w",
}
STORE_NUMBER = re.compile(r"#\s*(\d+)\s*$")


def normalized(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().casefold()
    return " ".join(re.findall(r"[a-z0-9]+", value.replace("'", "").replace("’", "")))


def normalized_address(value: str) -> str:
    return " ".join(ROAD_WORDS.get(token, token) for token in normalized(value).split())


def normalized_name(value: str) -> str:
    return normalized(re.sub(r"\s*#\s*\d+\s*$", "", value))


def brand_and_store_number(value: str) -> tuple[str, str | None]:
    match = STORE_NUMBER.search(value.strip())
    if not match:
        return normalized_name(value), None
    return normalized_name(value[: match.start()]), match.group(1)


def ambiguous_address(address: str) -> bool:
    return bool(re.search(r"\b(exit|interchange|mile\s*(marker|post))\b|[&@]|\bi\s*-?\s*\d+\b", address, re.I))


def brand_city_state_keys(station: FuelStation) -> set[tuple[str, str, str]]:
    keys = set()
    for name in [station.name, *station.source_names]:
        brand, number = brand_and_store_number(name)
        if brand and number:
            keys.add((brand, normalized(station.city), station.state))
    return keys


def contested_brand_cities(stations: QuerySet) -> set[tuple[str, str, str]]:
    counts: Counter = Counter()
    for station in stations.iterator(chunk_size=500):
        for key in brand_city_state_keys(station):
            counts[key] += 1
    return {key for key, count in counts.items() if count > 1}


class OsmStationResolver:
    def __init__(self, document: dict, boundaries: StateBoundaries, source_hash: str, boundary_hash: str,
                 contested_brands: set[tuple[str, str, str]] | None = None):
        self.boundaries = boundaries
        self.source_hash = source_hash
        self.boundary_hash = boundary_hash
        self.contested_brands = contested_brands or set()
        self.by_address = defaultdict(list)
        self.by_name = defaultdict(list)
        self.elements = {}
        self.ignored_elements = 0
        for element in document["elements"]:
            tags = element.get("tags", {})
            if tags.get("amenity") != "fuel" or element.get("type") not in ("node", "way", "relation"):
                self.ignored_elements += 1
                continue
            position = element if element["type"] == "node" else element.get("center", {})
            try:
                lon, lat = float(position["lon"]), float(position["lat"])
                if not valid_coordinate(lon, lat):
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                self.ignored_elements += 1
                continue
            osm_id = f'{element["type"]}/{element["id"]}'
            if osm_id in self.elements:
                raise ValueError("Duplicate OSM element identity")
            self.elements[osm_id] = element
            city = normalized(tags.get("addr:city", ""))
            number, street = tags.get("addr:housenumber", ""), tags.get("addr:street", "")
            if city and number and street:
                self.by_address[(city, normalized_address(f"{number} {street}"))].append(osm_id)
            names = {normalized_name(tags.get(field, "")) for field in ("name", "brand", "official_name", "alt_name")}
            for name in names - {""}:
                if city:
                    self.by_name[(city, name)].append(osm_id)

    def key(self, station: FuelStation) -> str:
        value = [MATCHER_VERSION, self.source_hash, self.boundary_hash, station.name,
                 station.source_names, station.address, station.city, station.state,
                 sorted(brand_city_state_keys(station) & self.contested_brands)]
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def resolve(self, station: FuelStation) -> dict:
        result = {
            "status": "not_found", "reason": "no_matching_fuel_poi", "latitude": None,
            "longitude": None, "confidence": None, "candidates": [],
            "source_sha256": self.source_hash, "boundary_sha256": self.boundary_hash,
            "matcher": MATCHER_VERSION, "attribution": ATTRIBUTION,
            "verification_method": "automated_name_address_and_state_polygon",
        }
        if station.state not in CONTIGUOUS_STATES:
            return {**result, "status": "unsupported", "reason": "outside_contiguous_us_scope"}
        if station.state not in self.boundaries.states:
            raise ValueError(f"Missing required boundary for {station.state}")
        station_names = [station.name, *station.source_names]
        brands = {brand_and_store_number(name)[0] for name in station_names} - {""}
        store_numbers = {brand_and_store_number(name)[1] for name in station_names} - {None}
        city, address = normalized(station.city), normalized_address(station.address)
        candidates = set(self.by_address.get((city, address), []))
        for brand in brands:
            candidates.update(self.by_name.get((city, brand), []))
        exact_address = []
        brand_in_city = []
        brand_with_store = []
        for osm_id in sorted(candidates):
            element = self.elements[osm_id]
            tags = element["tags"]
            position = element if element["type"] == "node" else element["center"]
            lon, lat = float(position["lon"]), float(position["lat"])
            in_state = self.boundaries.contains(station.state, lon, lat)
            osm_names = {normalized_name(tags.get(field, "")) for field in ("name", "brand", "official_name", "alt_name")}
            raw_osm_names = " ".join(tags.get(field, "") for field in ("name", "brand", "official_name", "alt_name"))
            osm_address = normalized_address(f'{tags.get("addr:housenumber", "")} {tags.get("addr:street", "")}')
            name_matches = bool(brands & osm_names)
            address_matches = bool(re.match(r"^\d+[a-z]?\s", address)) and address == osm_address
            country_matches = tags.get("addr:country", "US").upper() in ("US", "USA")
            tagged_state = tags.get("addr:state", "").upper()
            tag_matches = not tagged_state or tagged_state in (
                station.state, self.boundaries.state_names.get(station.state),
            )
            evidence = {
                "osm_id": osm_id, "longitude": lon, "latitude": lat, "tags": tags,
                "in_expected_state": in_state, "name_matches": name_matches,
                "address_matches": address_matches, "country_matches": country_matches,
                "state_tag_matches": tag_matches,
                "precision": "osm_poi_node" if element["type"] == "node" else "osm_feature_bbox_center",
            }
            result["candidates"].append(evidence)
            if not (in_state and name_matches and country_matches and tag_matches and element["type"] == "node"):
                continue
            if address_matches:
                exact_address.append(evidence)
            if store_numbers:
                brand_in_city.append(evidence)
                if any(re.search(rf"#\s*{re.escape(number)}\b", raw_osm_names, re.I)
                       or tags.get("ref", "").strip() == number for number in store_numbers):
                    brand_with_store.append(evidence)
        if len(exact_address) == 1:
            match = exact_address[0]
            return {
                **result, "status": "resolved", "reason": "unique_name_address_city_state_match",
                "latitude": str(match["latitude"]), "longitude": str(match["longitude"]),
                "osm_id": match["osm_id"], "precision": match["precision"],
                "verification_method": "automated_name_address_and_state_polygon",
            }
        if len(brand_with_store) == 1:
            match = brand_with_store[0]
            return {
                **result, "status": "resolved", "reason": "unique_brand_store_number_city_state_match",
                "latitude": str(match["latitude"]), "longitude": str(match["longitude"]),
                "osm_id": match["osm_id"], "precision": match["precision"],
                "verification_method": "automated_brand_store_number_city_and_state_polygon",
            }
        station_brand_keys = brand_city_state_keys(station)
        contested_here = bool(station_brand_keys & self.contested_brands)
        if len(brand_in_city) >= 1 and not brand_with_store and contested_here:
            return {**result, "status": "review", "reason": "multiple_same_brand_stations_in_city"}
        if ambiguous_address(station.address):
            return {**result, "status": "review", "reason": "ambiguous_highway_exit_or_intersection"}
        if candidates:
            return {**result, "status": "review",
                    "reason": "multiple_matches" if len(exact_address) > 1 or len(brand_in_city) > 1
                    else "insufficient_or_conflicting_location_evidence"}
        return result


def enrich_stations(stations: QuerySet, resolver: OsmStationResolver, batch_size: int = 100, limit: int | None = None) -> dict:
    """Commit each batch with its cache; an interrupted batch can safely be replayed."""
    stats = Counter(processed=0, skipped=0, cache_hits=0, matched_locally=0)
    if not stations.query.is_sliced:
        stations = stations.order_by("pk")
    iterator = stations.iterator(chunk_size=batch_size)
    while batch := list(islice(iterator, batch_size)):
        keys = {station.pk: resolver.key(station) for station in batch}
        cached = StationGeocodeCache.objects.in_bulk(keys.values())
        changed, new_cache = [], {}
        for station in batch:
            key = keys[station.pk]
            if station.geocoding_key == key and station.geocoding_status != "pending":
                stats["skipped"] += 1
                continue
            if limit is not None and stats["processed"] >= limit:
                break
            entry = cached.get(key) or new_cache.get(key)
            if entry:
                result = entry.result
                stats["cache_hits"] += 1
            else:
                result = resolver.resolve(station)
                new_cache[key] = StationGeocodeCache(key=key, result=result)
                stats["matched_locally"] += 1
            station.latitude = Decimal(result["latitude"]) if result["latitude"] is not None else None
            station.longitude = Decimal(result["longitude"]) if result["longitude"] is not None else None
            station.geocoding_confidence = Decimal(result["confidence"]) if result["confidence"] else None
            station.geocoding_status = result["status"]
            station.geocoding_source = SOURCE
            station.geocoding_key = key
            station.geocoding_details = result
            station.location_verified_at = timezone.now() if result["status"] == "resolved" else None
            changed.append(station)
            stats["processed"] += 1
        with transaction.atomic():
            StationGeocodeCache.objects.bulk_create(new_cache.values(), ignore_conflicts=True, batch_size=batch_size)
            FuelStation.objects.bulk_update(changed, [
                "latitude", "longitude", "geocoding_confidence", "geocoding_status", "geocoding_source",
                "geocoding_key", "geocoding_details", "location_verified_at",
            ], batch_size=batch_size)
        if limit is not None and stats["processed"] >= limit:
            break
    return dict(stats)


def coverage_report(stations: QuerySet) -> dict:
    from .station_search import usable_stations

    statuses = dict(stations.values("geocoding_status").annotate(count=Count("pk")).values_list("geocoding_status", "count"))
    total = sum(statuses.values())
    usable = usable_stations(stations).count()
    states = list(stations.values("state", "geocoding_status").annotate(count=Count("pk")).order_by("state", "geocoding_status"))
    return {"total_stations": total, "usable_stations": usable, "excluded_stations": total - usable,
            "usable_percent": round(100 * usable / total, 2) if total else 0,
            "statuses": statuses, "by_state": states}
