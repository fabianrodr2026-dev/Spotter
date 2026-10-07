import json
import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from django.db import connection
from django.db.models import QuerySet
from django.db.models.expressions import RawSQL

from .routing import DrivingRoute

METERS_PER_MILE = 1609.344
EARTH_RADIUS_METERS = 6_371_008.8


@dataclass(frozen=True)
class CandidateStation:
    source_id: str
    latitude: float
    longitude: float
    price_usd_per_gallon: Decimal
    route_distance_miles: float
    offset_miles: float
    estimated_detour_miles: float
    geocoding_confidence: Decimal | None
    geocoding_source: str
    location_quality: str


class StationLookup(Protocol):
    def candidates(self, route: DrivingRoute, corridor_miles: float) -> tuple[CandidateStation, ...]: ...


def usable_stations(stations: QuerySet | None = None) -> QuerySet:
    from routes.models import FuelStation
    from .state_boundaries import CONTIGUOUS_STATES

    if stations is None:
        stations = FuelStation.objects.all()
    return stations.filter(
        geocoding_status="resolved", location_verified_at__isnull=False,
        state__in=CONTIGUOUS_STATES,
        latitude__gte=-90, latitude__lte=90, longitude__gte=-180, longitude__lte=180,
    ).exclude(geocoding_key="")


def corridor_has_candidates(candidates: tuple[CandidateStation, ...]) -> bool:
    return bool(candidates)


def _haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_METERS * math.asin(math.sqrt(min(1.0, max(0.0, a))))


def _route_coordinates(route: DrivingRoute) -> list[tuple[float, float]]:
    geometry = route.geometry
    if not isinstance(geometry, dict) or geometry.get("type") != "LineString":
        raise ValueError("route geometry must be a GeoJSON LineString")
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list) or len(coordinates) < 2:
        raise ValueError("route geometry needs at least two coordinates")
    points: list[tuple[float, float]] = []
    for point in coordinates:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            raise ValueError("invalid route coordinate")
        lon, lat = float(point[0]), float(point[1])
        if not math.isfinite(lat) or not math.isfinite(lon) or abs(lat) > 90 or abs(lon) > 180:
            raise ValueError("invalid route coordinate")
        points.append((lat, lon))
    return points


def _leg_ranges(route: DrivingRoute, point_count: int) -> list[tuple[int, int, float]]:
    indices = route.waypoint_indices
    legs = route.leg_distances_miles
    if indices and legs and len(indices) == len(legs) + 1:
        if indices[0] != 0 or indices[-1] != point_count - 1:
            raise ValueError("waypoint indices do not cover the geometry")
        if any(type(i) is not int or i < 0 or i >= point_count for i in indices):
            raise ValueError("invalid waypoint indices")
        if list(indices) != sorted(indices):
            raise ValueError("waypoint indices must be nondecreasing")
        return [(indices[i], indices[i + 1], float(legs[i])) for i in range(len(legs))]
    return [(0, point_count - 1, float(route.distance_miles))]


def _segment_drive_miles(points: list[tuple[float, float]], route: DrivingRoute) -> list[float]:
    ranges = _leg_ranges(route, len(points))
    drive_miles = [0.0] * (len(points) - 1)
    for start_index, end_index, leg_miles in ranges:
        if end_index <= start_index:
            raise ValueError("empty route leg")
        if not math.isfinite(leg_miles) or leg_miles < 0:
            raise ValueError("invalid leg distance")
        geodesic = [
            _haversine_meters(
                points[i][0], points[i][1], points[i + 1][0], points[i + 1][1],
            ) / METERS_PER_MILE
            for i in range(start_index, end_index)
        ]
        total = sum(geodesic)
        if total <= 0:
            if leg_miles == 0:
                continue
            share = leg_miles / (end_index - start_index)
            for i in range(start_index, end_index):
                drive_miles[i] = share
            continue
        scale = leg_miles / total
        for offset, length in enumerate(geodesic):
            drive_miles[start_index + offset] = length * scale
    return drive_miles


def _project_to_segment(
    latitude: float, longitude: float,
    start: tuple[float, float], end: tuple[float, float],
) -> tuple[float, float]:
    start_lat, start_lon = start
    end_lat, end_lon = end
    lat0 = math.radians(start_lat)
    cos_lat = math.cos(lat0)
    ax = ay = 0.0
    bx = math.radians(end_lon - start_lon) * cos_lat * EARTH_RADIUS_METERS
    by = math.radians(end_lat - start_lat) * EARTH_RADIUS_METERS
    px = math.radians(longitude - start_lon) * cos_lat * EARTH_RADIUS_METERS
    py = math.radians(latitude - start_lat) * EARTH_RADIUS_METERS
    ab2 = (bx - ax) ** 2 + (by - ay) ** 2
    if ab2 <= 0:
        return 0.0, _haversine_meters(latitude, longitude, start_lat, start_lon) / METERS_PER_MILE
    t = ((px - ax) * (bx - ax) + (py - ay) * (by - ay)) / ab2
    t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
    proj_lat = start_lat + t * (end_lat - start_lat)
    proj_lon = start_lon + t * (end_lon - start_lon)
    offset = _haversine_meters(latitude, longitude, proj_lat, proj_lon) / METERS_PER_MILE
    return t, offset


def _project_station(
    latitude: float, longitude: float,
    points: list[tuple[float, float]],
    segment_drive_miles: list[float],
) -> tuple[float, float]:
    cumulative = [0.0]
    for length in segment_drive_miles:
        cumulative.append(cumulative[-1] + length)
    best_offset = math.inf
    best_route_distance = math.inf
    for index, length in enumerate(segment_drive_miles):
        t, offset = _project_to_segment(latitude, longitude, points[index], points[index + 1])
        route_distance = cumulative[index] + t * length
        if offset < best_offset - 1e-9 or (
            abs(offset - best_offset) <= 1e-9 and route_distance < best_route_distance
        ):
            best_offset = offset
            best_route_distance = route_distance
    if not math.isfinite(best_offset):
        raise ValueError("unable to project station onto route")
    return best_route_distance, best_offset


def _location_quality(station) -> str:
    details = station.geocoding_details if isinstance(station.geocoding_details, dict) else {}
    reason = details.get("reason")
    if isinstance(reason, str) and reason:
        return reason
    if station.geocoding_status == "resolved" and station.location_verified_at is not None:
        return "resolved_verified"
    return station.geocoding_status or "unknown"


def _stations_in_corridor_postgis(route: DrivingRoute, corridor_meters: float, qs: QuerySet) -> QuerySet:
    geojson = json.dumps(route.geometry, separators=(",", ":"))
    return qs.annotate(
        _in_route_corridor=RawSQL(
            """
            ST_DWithin(
                ST_SetSRID(ST_MakePoint(longitude::double precision,
                                        latitude::double precision), 4326)::geography,
                ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)::geography,
                %s
            )
            """,
            (geojson, corridor_meters),
        )
    ).filter(_in_route_corridor=True)


def find_candidate_stations(
    route: DrivingRoute,
    corridor_miles: float | None = None,
    stations: QuerySet | None = None,
) -> tuple[CandidateStation, ...]:
    if corridor_miles is None:
        from django.conf import settings
        corridor_miles = float(settings.STATION_CORRIDOR_MILES)
    if not math.isfinite(corridor_miles) or corridor_miles < 0:
        raise ValueError("corridor_miles must be finite and non-negative")
    if not math.isfinite(route.distance_miles) or route.distance_miles < 0:
        raise ValueError("route distance must be finite and non-negative")

    points = _route_coordinates(route)
    segment_drive_miles = _segment_drive_miles(points, route)
    qs = usable_stations(stations)
    if connection.vendor == "postgresql":
        qs = _stations_in_corridor_postgis(route, corridor_miles * METERS_PER_MILE, qs)

    candidates: list[CandidateStation] = []
    for station in qs.iterator():
        latitude = float(station.latitude)
        longitude = float(station.longitude)
        route_distance_miles, offset_miles = _project_station(
            latitude, longitude, points, segment_drive_miles,
        )
        if offset_miles > corridor_miles + 1e-9:
            continue
        estimated_detour_miles = 2.0 * offset_miles
        candidates.append(CandidateStation(
            source_id=station.source_station_id,
            latitude=latitude,
            longitude=longitude,
            price_usd_per_gallon=station.price_usd_per_gallon,
            route_distance_miles=route_distance_miles,
            offset_miles=offset_miles,
            estimated_detour_miles=estimated_detour_miles,
            geocoding_confidence=station.geocoding_confidence,
            geocoding_source=station.geocoding_source or "",
            location_quality=_location_quality(station),
        ))

    candidates.sort(key=lambda item: (
        item.route_distance_miles, item.price_usd_per_gallon, item.source_id,
    ))
    return tuple(candidates)


class CorridorStationLookup:
    def candidates(self, route: DrivingRoute, corridor_miles: float) -> tuple[CandidateStation, ...]:
        return find_candidate_stations(route, corridor_miles)
