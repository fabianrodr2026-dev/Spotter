from __future__ import annotations

import logging
import time
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable

from django.conf import settings
from django.urls import reverse

from routes.models import FuelPriceDataset, FuelStation
from routes.services.optimizer import MILES_PER_GALLON, TANK_CAPACITY_GALLONS
from routes.services.planner import PlanError, PlannedRoute, RoutePlanner
from routes.services.plan_store import (
    get_cached_plan_by_material,
    new_plan_id,
    store_successful_plan,
)
from routes.services.routing import RoutingError, create_routing_session
from routes.services.station_search import CorridorStationLookup
from routes.services.usa import (
    BOUNDARY_SOURCE,
    geometry_stays_in_contiguous_usa,
    in_contiguous_usa,
    load_contiguous_boundaries,
)
from routes.validation import InvalidPlanInput, PlanInput, parse_plan_input

logger = logging.getLogger("routes.plan")

CENT = Decimal("0.01")

ERROR_STATUS = {
    "invalid_input": 400,
    "unsupported_location": 400,
    "unsupported_route": 422,
    "no_route": 422,
    "insufficient_station_coverage": 422,
    "infeasible_fuel": 422,
    "inability_to_plan": 422,
    "station_inaccessible": 422,
    "invalid_route": 422,
    "route_length_limit": 422,
    "invalid_waypoints": 422,
    "provider_rejected_request": 422,
    "provider_unavailable": 503,
    "rate_limited": 503,
    "provider_authentication": 503,
    "planning_deadline_exceeded": 503,
    "routing_budget_exhausted": 503,
    "invalid_response": 503,
    "dataset_unavailable": 503,
}


class PlanRequestError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(code)


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _money_cents(value: Decimal) -> str:
    return format(value.quantize(CENT, rounding=ROUND_HALF_UP), "f")


def _latest_dataset_sha() -> str:
    dataset = FuelPriceDataset.objects.order_by("-imported_at").first()
    if dataset is None:
        raise PlanRequestError(
            "dataset_unavailable",
            "No imported fuel-price dataset is available; run import_fuel_prices first",
        )
    return dataset.sha256


def build_material(request: PlanInput, dataset_sha: str, boundary_hash: str) -> dict[str, Any]:
    return {
        "start": {
            "latitude": request.start.latitude,
            "longitude": request.start.longitude,
        },
        "finish": {
            "latitude": request.finish.latitude,
            "longitude": request.finish.longitude,
        },
        "initial_fuel_gallons": request.initial_fuel_gallons,
        "tank_capacity_gallons": _decimal_text(TANK_CAPACITY_GALLONS),
        "miles_per_gallon": _decimal_text(MILES_PER_GALLON),
        "station_corridor_miles": float(settings.STATION_CORRIDOR_MILES),
        "station_snap_tolerance_miles": float(settings.STATION_SNAP_TOLERANCE_MILES),
        "dataset_sha256": dataset_sha,
        "boundary_hash": boundary_hash,
        "routing_profile": "driving-car",
        "routing_options": {"avoid_borders": "all"},
        "routing_adapter_version": "v2-adapter-1",
    }


def _station_details(source_ids: list[str]) -> dict[str, dict[str, str]]:
    if not source_ids:
        return {}
    rows = FuelStation.objects.filter(source_station_id__in=source_ids).only(
        "source_station_id", "name", "city", "state",
    )
    return {
        row.source_station_id: {
            "name": row.name,
            "city": row.city,
            "state": row.state,
        }
        for row in rows
    }


def serialize_planned_route(
    planned: PlannedRoute,
    *,
    plan_id: str,
    request: PlanInput,
    dataset_sha: str,
    boundary_hash: str,
    cache_status: str,
    candidate_count: int,
) -> dict[str, Any]:
    details = _station_details([stop.source_id for stop in planned.selected_stops])
    stops = []
    for stop in planned.selected_stops:
        meta = details.get(stop.source_id, {"name": "", "city": "", "state": ""})
        stops.append({
            "source_id": stop.source_id,
            "name": meta["name"],
            "city": meta["city"],
            "state": meta["state"],
            "latitude": stop.latitude,
            "longitude": stop.longitude,
            "price_usd_per_gallon": _decimal_text(stop.price_usd_per_gallon),
            "gallons_purchased": _decimal_text(stop.gallons_purchased),
            "cost_usd": _decimal_text(stop.cost_usd),
        })
    return {
        "plan_id": plan_id,
        "map_url": reverse("map-plan", kwargs={"plan_id": plan_id}),
        "route": {
            "geometry": planned.route.geometry,
            "distance_miles": planned.route.distance_miles,
            "duration_seconds": planned.route.duration_seconds,
            "leg_distances_miles": list(planned.route.leg_distances_miles),
        },
        "stops": stops,
        "totals": {
            "initial_fuel_gallons": _decimal_text(planned.fuel.initial_fuel_gallons),
            "fuel_consumed_gallons": _decimal_text(planned.fuel.fuel_consumed_gallons),
            "fuel_purchased_gallons": _decimal_text(planned.fuel.fuel_purchased_gallons),
            "fuel_remaining_gallons": _decimal_text(planned.fuel.fuel_remaining_gallons),
            "total_fuel_cost_usd": _money_cents(planned.fuel.total_fuel_cost_usd),
        },
        "assumptions": list(planned.assumptions),
        "routing_attempts": planned.routing_attempts,
        "repaired": planned.repaired,
        "cache_status": cache_status,
        "dataset_version": dataset_sha,
        "boundary": {
            "source": BOUNDARY_SOURCE,
            "hash": boundary_hash,
        },
        "request": {
            "start": {
                "latitude": request.start.latitude,
                "longitude": request.start.longitude,
            },
            "finish": {
                "latitude": request.finish.latitude,
                "longitude": request.finish.longitude,
            },
            "initial_fuel_gallons": request.initial_fuel_gallons,
        },
        "diagnostics": {
            "candidate_count": candidate_count,
        },
        "attribution": {
            "routing": planned.route.attribution,
            "map_tiles": "© OpenStreetMap contributors",
        },
    }


def _map_plan_error(exc: PlanError | RoutingError | PlanRequestError) -> PlanRequestError:
    if isinstance(exc, PlanRequestError):
        return exc
    code = exc.code
    if code == "no_route":
        message = "No supported driving route connects the requested endpoints"
    elif code in ERROR_STATUS:
        message = getattr(exc, "message", None) or f"Planning failed: {code}"
    else:
        code = "inability_to_plan"
        message = getattr(exc, "message", None) or "Unable to produce a verified fuel plan"
    return PlanRequestError(code, message)


def validate_request_geography(request: PlanInput) -> tuple[object, str]:
    boundaries, boundary_hash = load_contiguous_boundaries()
    if not in_contiguous_usa(request.start, boundaries):
        raise PlanRequestError(
            "unsupported_location",
            "start must be inside the contiguous United States or Washington, DC",
        )
    if not in_contiguous_usa(request.finish, boundaries):
        raise PlanRequestError(
            "unsupported_location",
            "finish must be inside the contiguous United States or Washington, DC",
        )
    return boundaries, boundary_hash


def ensure_route_in_usa(planned: PlannedRoute, boundaries) -> None:
    if not geometry_stays_in_contiguous_usa(planned.route.geometry, boundaries=boundaries):
        raise PlanRequestError(
            "unsupported_route",
            "Provider route leaves the contiguous United States",
        )


def plan_route_payload(
    body: bytes,
    *,
    planner_factory: Callable[[], RoutePlanner] | None = None,
) -> tuple[dict[str, Any], int]:
    started = time.perf_counter()
    stages: dict[str, float] = {}

    def mark(name: str, origin: float) -> None:
        stages[name] = round((time.perf_counter() - origin) * 1000, 3)

    parse_started = time.perf_counter()
    try:
        request = parse_plan_input(body)
    except InvalidPlanInput as exc:
        raise PlanRequestError("invalid_input", str(exc)) from exc
    mark("parse_ms", parse_started)

    geo_started = time.perf_counter()
    boundaries, boundary_hash = validate_request_geography(request)
    mark("geography_ms", geo_started)

    dataset_started = time.perf_counter()
    dataset_sha = _latest_dataset_sha()
    mark("dataset_ms", dataset_started)

    material = build_material(request, dataset_sha, boundary_hash)
    cached = get_cached_plan_by_material(material)
    if cached is not None:
        logger.info(
            "plan cache_hit stages=%s routing_attempts=%s candidate_count=%s total_ms=%.3f",
            stages,
            cached.get("routing_attempts"),
            cached.get("diagnostics", {}).get("candidate_count"),
            (time.perf_counter() - started) * 1000,
        )
        payload = dict(cached)
        payload["cache_status"] = "hit"
        return payload, 200

    plan_started = time.perf_counter()
    if planner_factory is None:
        base_lookup = CorridorStationLookup()
    else:
        planner = planner_factory()
        base_lookup = planner.station_lookup

    class CountingLookup:
        def __init__(self, inner):
            self.inner = inner
            self.count = 0

        def candidates(self, route, corridor_miles):
            result = self.inner.candidates(route, corridor_miles)
            self.count = len(result)
            return result

    lookup = CountingLookup(base_lookup)
    if planner_factory is None:
        planner = RoutePlanner(create_routing_session, lookup)
    else:
        planner = RoutePlanner(
            planner.session_factory,
            lookup,
            corridor_miles=planner.corridor_miles,
            snap_tolerance_miles=planner.snap_tolerance_miles,
        )

    try:
        planned = planner.plan(request)
        ensure_route_in_usa(planned, boundaries)
    except (PlanError, RoutingError, PlanRequestError) as exc:
        mapped = _map_plan_error(exc)
        logger.info(
            "plan failed code=%s stages=%s candidate_count=%s total_ms=%.3f",
            mapped.code,
            stages,
            lookup.count,
            (time.perf_counter() - started) * 1000,
        )
        raise mapped from exc
    mark("plan_ms", plan_started)
    candidate_count = lookup.count

    plan_id = new_plan_id()
    payload = serialize_planned_route(
        planned,
        plan_id=plan_id,
        request=request,
        dataset_sha=dataset_sha,
        boundary_hash=boundary_hash,
        cache_status="miss",
        candidate_count=candidate_count,
    )
    store_successful_plan(plan_id, material, payload)
    logger.info(
        "plan cache_miss stages=%s routing_attempts=%s candidate_count=%s total_ms=%.3f",
        stages,
        payload["routing_attempts"],
        candidate_count,
        (time.perf_counter() - started) * 1000,
    )
    return payload, 200


def status_for_error(code: str) -> int:
    return ERROR_STATUS.get(code, 500)
