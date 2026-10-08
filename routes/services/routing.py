import copy
import hashlib
import json
import math
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from routes.validation import Coordinate

PROVIDER_ATTRIBUTION = "© openrouteservice by HeiGIT | Data from OpenStreetMap"


@dataclass(frozen=True)
class DrivingRoute:
    geometry: dict
    distance_miles: float
    duration_seconds: float
    leg_distances_miles: tuple[float, ...]
    waypoint_indices: tuple[int, ...] = ()
    provider_version: str = ""
    attribution: str = PROVIDER_ATTRIBUTION


class RoutingProvider(Protocol):
    def route(self, waypoints: tuple[Coordinate, ...]) -> DrivingRoute: ...


class RoutingError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class RoutingBudget:
    """One sequential planning operation, including all stages and retries."""

    def __init__(self, deadline_seconds: float, clock: Callable = time.monotonic):
        if not math.isfinite(deadline_seconds) or deadline_seconds <= 0:
            raise ValueError("deadline_seconds must be positive and finite")
        self.clock = clock
        self.expires_at = clock() + deadline_seconds
        self.attempts = 0

    def remaining(self) -> float:
        remaining = self.expires_at - self.clock()
        if remaining <= 0:
            raise RoutingError("planning_deadline_exceeded")
        return remaining

    def consume(self) -> None:
        self.remaining()
        if self.attempts >= 3:
            raise RoutingError("routing_budget_exhausted")
        self.attempts += 1


def post_once(request: dict, deadline_seconds: float) -> dict:
    # A separate process bounds DNS, TLS, headers and slow-drip response bodies.
    # Credentials go through stdin, never command-line arguments or error output.
    try:
        completed = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("routing_http.py"))],
            input=json.dumps(request), capture_output=True, text=True,
            timeout=deadline_seconds, check=False,
        )
    except subprocess.TimeoutExpired:
        raise RoutingError("planning_deadline_exceeded") from None
    except OSError:
        raise RoutingError("provider_unavailable") from None
    if completed.returncode:
        raise RoutingError("provider_unavailable")
    try:
        result = json.loads(completed.stdout)
        if "error" in result:
            raise RoutingError(result["error"])
        return result
    except (ValueError, TypeError):
        raise RoutingError("invalid_response") from None


def _number(value: object, maximum: float = math.inf) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 <= value <= maximum):
        raise ValueError("Invalid numeric value")
    return float(value)


def _validate_coordinate(point: Coordinate) -> None:
    for value, limit in ((point.latitude, 90), (point.longitude, 180)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Invalid coordinate")
        if not math.isfinite(value) or abs(value) > limit:
            raise ValueError("Invalid coordinate")


def _lower_bound_meters(points: tuple[Coordinate, ...]) -> float:
    total = 0.0
    for start, finish in zip(points, points[1:]):
        lat1, lat2 = math.radians(start.latitude), math.radians(finish.latitude)
        dlon = math.radians(finish.longitude - start.longitude)
        a = (math.sin((lat2 - lat1) / 2) ** 2
             + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2)
        total += 2 * 6371008.8 * math.asin(math.sqrt(min(1, max(0, a))))
    return total


def _parse_route(payload: dict, waypoint_count: int) -> DrivingRoute:
    try:
        if payload["type"] != "FeatureCollection" or len(payload["features"]) != 1:
            raise ValueError
        feature = payload["features"][0]
        geometry = feature["geometry"]
        coordinates = geometry["coordinates"]
        if geometry["type"] != "LineString" or len(coordinates) < 2:
            raise ValueError
        for point in coordinates:
            if len(point) != 2:
                raise ValueError
            _validate_coordinate(Coordinate(point[1], point[0]))
        properties = feature["properties"]
        distance = _number(properties["summary"]["distance"], 6_000_000)
        duration = _number(properties["summary"]["duration"])
        legs = tuple(_number(leg["distance"]) for leg in properties["segments"])
        if len(legs) != waypoint_count - 1:
            raise ValueError
        if not math.isclose(sum(legs), distance, abs_tol=0.1 * len(legs), rel_tol=1e-6):
            raise ValueError
        indices = tuple(properties["way_points"])
        if (len(indices) != waypoint_count or indices[0] != 0
                or indices[-1] != len(coordinates) - 1
                or any(type(i) is not int or not 0 <= i < len(coordinates) for i in indices)
                or list(indices) != sorted(indices)):
            raise ValueError
        metadata = payload.get("metadata", {})
        return DrivingRoute(
            copy.deepcopy(geometry), distance / 1609.344, duration,
            tuple(leg / 1609.344 for leg in legs), indices,
            str(metadata.get("engine", {}).get("version", "unknown")),
            PROVIDER_ATTRIBUTION,
        )
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        raise RoutingError("invalid_response") from None


class OpenRouteService:
    """Cached, single-attempt adapter; reuse one RoutingBudget for the whole plan."""

    def __init__(self, *, api_key: str, cache, budget: RoutingBudget,
                 profile: str = "driving-car", options: dict | None = None,
                 connect_timeout: float = 3, read_timeout: float = 15,
                 cache_timeout: int = 3600, version: str = "v2-adapter-1",
                 transport: Callable = post_once):
        if not api_key or api_key.startswith("replace-"):
            raise ValueError("ROUTING_PROVIDER_API_KEY is required")
        if profile not in ("driving-car", "driving-hgv"):
            raise ValueError("Unsupported driving profile")
        self.options = copy.deepcopy(options if options is not None else {})
        # Other ORS options have different distance limits; do not silently accept them.
        if set(self.options) - {"avoid_borders"}:
            raise ValueError("Unsupported routing options")
        if self.options.get("avoid_borders", "none") not in ("all", "controlled", "none"):
            raise ValueError("Invalid avoid_borders")
        for value in (connect_timeout, read_timeout, cache_timeout):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Timeouts must be positive and finite")
        self.api_key, self.cache, self.budget = api_key, cache, budget
        self.profile, self.version = profile, version
        self.connect_timeout, self.read_timeout = connect_timeout, read_timeout
        self.cache_timeout, self.transport = cache_timeout, transport

    def route(self, waypoints: tuple[Coordinate, ...]) -> DrivingRoute:
        self.budget.remaining()
        try:
            if not 2 <= len(waypoints) <= 50:
                raise ValueError
            for point in waypoints:
                _validate_coordinate(point)
        except (ValueError, TypeError, AttributeError):
            raise RoutingError("invalid_waypoints") from None
        # This is a lower bound, not a prediction of road distance. ORS enforces
        # the actual road limit; an over-limit response is never cached.
        if _lower_bound_meters(waypoints) > 6_000_000:
            raise RoutingError("route_length_limit")
        body = {
            "coordinates": [[p.longitude, p.latitude] for p in waypoints],
            "instructions": True, "geometry_simplify": False,
            "units": "m", "options": self.options,
        }
        path = f"/openrouteservice/v2/directions/{self.profile}/geojson"
        material = ["api.heigit.org", path, self.version, body]
        key = "ors:" + hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()
        cached = self.cache.get(key)
        self.budget.remaining()
        if cached is not None:
            return copy.deepcopy(cached)
        self.budget.consume()
        response = self.transport({
            "path": path, "body": body, "key": self.api_key,
            "connect_timeout": min(self.connect_timeout, self.budget.remaining()),
            "read_timeout": min(self.read_timeout, self.budget.remaining()),
        }, self.budget.remaining())
        self.budget.remaining()
        status = response["status"]
        if status == 429:
            raise RoutingError("rate_limited")
        if status in (401, 403):
            raise RoutingError("provider_authentication")
        if status >= 500:
            raise RoutingError("provider_unavailable")
        try:
            payload = json.loads(response["body"])
        except (ValueError, TypeError):
            raise RoutingError("invalid_response") from None
        if status != 200:
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            code = error.get("code") if isinstance(error, dict) else None
            if code in (2009, 2010):
                raise RoutingError("no_route")
            if code == 2004:
                raise RoutingError("route_length_limit")
            raise RoutingError("provider_rejected_request")
        route = _parse_route(payload, len(waypoints))
        self.budget.remaining()
        self.cache.set(key, route, timeout=self.cache_timeout)
        self.budget.remaining()
        return route


class RoutingSession:
    """Initial route, selected stops, then at most one explicit repair/retry.

    No automatic retries: a failed stage can be explicitly repeated, but every
    cache miss uses the same three-attempt budget. Instances are not thread-safe.
    """

    def __init__(self, provider: OpenRouteService, start: Coordinate, finish: Coordinate):
        self.provider, self.start, self.finish = provider, start, finish
        self.initial_route: DrivingRoute | None = None
        self.selected_requested = False
        self.repair_requested = False

    def initial(self) -> DrivingRoute:
        self.provider.budget.remaining()
        if self.initial_route is None:
            self.initial_route = self.provider.route((self.start, self.finish))
        return copy.deepcopy(self.initial_route)

    def through_stops(self, stops: tuple[Coordinate, ...], *, repair: bool = False) -> DrivingRoute:
        if self.initial_route is None:
            raise RoutingError("initial_route_required")
        if repair:
            if not self.selected_requested or self.repair_requested:
                raise RoutingError("repair_not_available")
            self.repair_requested = True
        elif self.selected_requested:
            raise RoutingError("use_bounded_repair")
        self.selected_requested = True
        return self.provider.route((self.start, *stops, self.finish))


def create_routing_session(start: Coordinate, finish: Coordinate) -> RoutingSession:
    from django.conf import settings
    from django.core.cache import cache

    provider = OpenRouteService(
        api_key=settings.ROUTING_PROVIDER_API_KEY, cache=cache,
        budget=RoutingBudget(settings.ROUTING_TOTAL_DEADLINE_SECONDS),
        connect_timeout=settings.ROUTING_CONNECT_TIMEOUT_SECONDS,
        read_timeout=settings.ROUTING_READ_TIMEOUT_SECONDS,
        cache_timeout=settings.ROUTE_CACHE_TIMEOUT_SECONDS,
    )
    return RoutingSession(provider, start, finish)
