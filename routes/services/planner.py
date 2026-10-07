from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Sequence

from routes.validation import Coordinate, PlanInput

from .optimizer import (
    DISTANCE_TOLERANCE_MILES,
    FUEL_TOLERANCE_GALLONS,
    FuelOptimizationError,
    FuelPlan,
    FuelStop,
    TANK_CAPACITY_GALLONS,
    legs_from_route_distances,
    optimize_fuel_purchases,
)
from .routing import DrivingRoute, RoutingError, RoutingSession
from .station_search import CandidateStation, StationLookup, corridor_has_candidates

METERS_PER_MILE = 1609.344
EARTH_RADIUS_METERS = 6_371_008.8
REPAIR_WINDOW_MILES = 75.0

ASSUMPTIONS = (
    "Purchase quantities are optimized for the final ordered stops.",
    "Stop selection is a corridor-based heuristic, not a globally optimal station set.",
    "Retail prices are treated as USD per US gallon.",
    "Vehicle tank capacity is 50 US gallons at 10 miles per gallon.",
)


@dataclass(frozen=True)
class SelectedStop:
    source_id: str
    latitude: float
    longitude: float
    price_usd_per_gallon: Decimal
    gallons_purchased: Decimal
    cost_usd: Decimal


@dataclass(frozen=True)
class PlannedRoute:
    route: DrivingRoute
    fuel: FuelPlan
    selected_stops: tuple[SelectedStop, ...]
    routing_attempts: int
    assumptions: tuple[str, ...]
    repaired: bool = False


class PlanError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(code)


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return (2 * EARTH_RADIUS_METERS * math.asin(math.sqrt(min(1.0, max(0.0, a))))) / METERS_PER_MILE


def _geometry_point(route: DrivingRoute, waypoint_index: int) -> Coordinate:
    coordinates = route.geometry.get("coordinates")
    if not isinstance(coordinates, list) or waypoint_index >= len(coordinates):
        raise PlanError("invalid_route", "Verified route geometry is missing a waypoint")
    point = coordinates[waypoint_index]
    if not isinstance(point, (list, tuple)) or len(point) < 2:
        raise PlanError("invalid_route", "Verified route geometry has an invalid waypoint")
    return Coordinate(float(point[1]), float(point[0]))


def _verify_stop_snapping(
    route: DrivingRoute,
    stops: Sequence[CandidateStation],
    snap_tolerance_miles: float,
) -> None:
    indices = route.waypoint_indices
    if len(indices) != len(stops) + 2:
        raise PlanError("invalid_route", "Verified route waypoint count does not match selected stops")
    for offset, stop in enumerate(stops):
        snapped = _geometry_point(route, indices[offset + 1])
        distance = _haversine_miles(stop.latitude, stop.longitude, snapped.latitude, snapped.longitude)
        if distance > snap_tolerance_miles + DISTANCE_TOLERANCE_MILES:
            raise PlanError(
                "station_inaccessible",
                f"Provider route snaps more than {snap_tolerance_miles} miles from station {stop.source_id}",
            )


def _candidate_map(candidates: Sequence[CandidateStation]) -> dict[str, CandidateStation]:
    return {candidate.source_id: candidate for candidate in candidates}


def _stops_for_purchases(
    plan: FuelPlan,
    by_id: dict[str, CandidateStation],
) -> tuple[CandidateStation, ...]:
    selected: list[CandidateStation] = []
    for purchase in plan.purchases:
        if purchase.gallons_purchased <= FUEL_TOLERANCE_GALLONS:
            continue
        station = by_id.get(purchase.station_id)
        if station is None:
            raise PlanError("insufficient_station_coverage", "Selected station is no longer available")
        selected.append(station)
    return tuple(selected)


def _selected_stop_results(
    plan: FuelPlan,
    stops: Sequence[CandidateStation],
) -> tuple[SelectedStop, ...]:
    by_id = _candidate_map(stops)
    results: list[SelectedStop] = []
    for purchase in plan.purchases:
        station = by_id[purchase.station_id]
        results.append(SelectedStop(
            source_id=station.source_id,
            latitude=station.latitude,
            longitude=station.longitude,
            price_usd_per_gallon=purchase.price_usd_per_gallon,
            gallons_purchased=purchase.gallons_purchased,
            cost_usd=purchase.cost_usd,
        ))
    return tuple(results)


def _estimate_from_candidates(
    candidates: Sequence[CandidateStation],
    total_distance_miles: float,
    initial_fuel_gallons: float,
) -> FuelPlan:
    ordered = tuple(sorted(
        candidates,
        key=lambda item: (item.route_distance_miles, item.price_usd_per_gallon, item.source_id),
    ))
    stops = tuple(
        FuelStop(station_id=c.source_id, price_usd_per_gallon=c.price_usd_per_gallon)
        for c in ordered
    )
    legs = legs_from_route_distances(
        tuple(c.route_distance_miles for c in ordered),
        total_distance_miles,
    )
    return optimize_fuel_purchases(stops, legs, initial_fuel_gallons, is_estimate=True)


def _plan_from_verified_legs(
    stops: Sequence[CandidateStation],
    leg_distances_miles: Sequence[float],
    initial_fuel_gallons: float,
) -> FuelPlan:
    fuel_stops = tuple(
        FuelStop(station_id=c.source_id, price_usd_per_gallon=c.price_usd_per_gallon)
        for c in stops
    )
    return optimize_fuel_purchases(
        fuel_stops, leg_distances_miles, initial_fuel_gallons, is_estimate=False,
    )


def _within_initial_range(distance_miles: float, initial_fuel_gallons: float) -> bool:
    range_miles = float(Decimal(str(initial_fuel_gallons)) * Decimal("10"))
    return distance_miles <= range_miles + DISTANCE_TOLERANCE_MILES


def _repair_candidates(
    original: Sequence[CandidateStation],
    failing: CandidateStation,
    pool: Sequence[CandidateStation],
    excluded: set[str],
) -> tuple[CandidateStation, ...] | None:
    alternatives = [
        candidate for candidate in pool
        if candidate.source_id not in excluded
        and abs(candidate.route_distance_miles - failing.route_distance_miles) <= REPAIR_WINDOW_MILES
    ]
    alternatives.sort(key=lambda item: (
        item.price_usd_per_gallon,
        abs(item.route_distance_miles - failing.route_distance_miles),
        item.source_id,
    ))
    if not alternatives:
        return None
    replacement = alternatives[0]
    repaired: list[CandidateStation] = []
    for station in original:
        if station.source_id == failing.source_id:
            repaired.append(replacement)
        elif station.source_id != replacement.source_id:
            repaired.append(station)
    repaired.sort(key=lambda item: (item.route_distance_miles, item.price_usd_per_gallon, item.source_id))
    return tuple(repaired)


class RoutePlanner:
    def __init__(
        self,
        session_factory: Callable[[Coordinate, Coordinate], RoutingSession],
        station_lookup: StationLookup,
        *,
        corridor_miles: float | None = None,
        snap_tolerance_miles: float | None = None,
    ) -> None:
        if snap_tolerance_miles is not None and (
            not math.isfinite(snap_tolerance_miles) or snap_tolerance_miles < 0
        ):
            raise ValueError("snap_tolerance_miles must be finite and non-negative")
        self.session_factory = session_factory
        self.station_lookup = station_lookup
        self.corridor_miles = corridor_miles
        self.snap_tolerance_miles = snap_tolerance_miles

    def plan(self, request: PlanInput) -> PlannedRoute:
        if request.initial_fuel_gallons < 0 or request.initial_fuel_gallons > float(TANK_CAPACITY_GALLONS):
            raise PlanError("invalid_input", "initial_fuel_gallons must be between 0 and 50")

        session = self.session_factory(request.start, request.finish)
        try:
            initial_route = session.initial()
        except RoutingError as exc:
            raise PlanError(exc.code, f"Initial routing failed: {exc.code}") from exc

        from django.conf import settings

        corridor = (
            float(settings.STATION_CORRIDOR_MILES)
            if self.corridor_miles is None
            else self.corridor_miles
        )
        snap_tolerance_miles = (
            float(settings.STATION_SNAP_TOLERANCE_MILES)
            if self.snap_tolerance_miles is None
            else self.snap_tolerance_miles
        )

        candidates = self.station_lookup.candidates(initial_route, corridor)
        by_id = _candidate_map(candidates)

        if not corridor_has_candidates(candidates):
            if not _within_initial_range(initial_route.distance_miles, request.initial_fuel_gallons):
                raise PlanError(
                    "insufficient_station_coverage",
                    "No usable stations in the route corridor and initial fuel cannot finish the trip",
                )
            verified = self._route_through(session, ())
            fuel = optimize_fuel_purchases(
                (), verified.leg_distances_miles, request.initial_fuel_gallons, is_estimate=False,
            )
            return PlannedRoute(
                route=verified,
                fuel=fuel,
                selected_stops=(),
                routing_attempts=session.provider.budget.attempts,
                assumptions=ASSUMPTIONS,
            )

        try:
            estimate = _estimate_from_candidates(
                candidates, initial_route.distance_miles, request.initial_fuel_gallons,
            )
        except FuelOptimizationError as exc:
            raise PlanError(
                "infeasible_fuel",
                "Corridor stations cannot support a feasible fuel plan for this trip",
            ) from exc

        selected = _stops_for_purchases(estimate, by_id)
        repaired = False
        try:
            verified_route, fuel = self._verify_sequence(
                session, selected, request.initial_fuel_gallons, snap_tolerance_miles, repair=False,
            )
        except PlanError as first_error:
            if not selected or session.repair_requested:
                raise PlanError(
                    "inability_to_plan",
                    "Unable to verify a feasible route through selected fuel stops",
                ) from first_error
            failing = self._identify_failure_stop(selected, first_error)
            excluded = {stop.source_id for stop in selected}
            repaired_stops = _repair_candidates(selected, failing, candidates, excluded)
            if repaired_stops is None:
                raise PlanError(
                    "inability_to_plan",
                    "Unable to verify a feasible route through selected fuel stops",
                ) from first_error
            try:
                verified_route, fuel = self._verify_sequence(
                    session, repaired_stops, request.initial_fuel_gallons, snap_tolerance_miles, repair=True,
                )
                selected = repaired_stops
                repaired = True
            except PlanError as exc:
                raise PlanError(
                    "inability_to_plan",
                    "Verified routing through selected fuel stops failed after bounded repair",
                ) from exc

        return PlannedRoute(
            route=verified_route,
            fuel=fuel,
            selected_stops=_selected_stop_results(fuel, selected),
            routing_attempts=session.provider.budget.attempts,
            assumptions=ASSUMPTIONS,
            repaired=repaired,
        )

    def _identify_failure_stop(
        self,
        selected: Sequence[CandidateStation],
        error: PlanError,
    ) -> CandidateStation:
        for stop in selected:
            if stop.source_id in error.message:
                return stop
        return selected[0]

    def _route_through(
        self,
        session: RoutingSession,
        stops: Sequence[CandidateStation],
        *,
        repair: bool = False,
    ) -> DrivingRoute:
        waypoints = tuple(Coordinate(stop.latitude, stop.longitude) for stop in stops)
        try:
            return session.through_stops(waypoints, repair=repair)
        except RoutingError as exc:
            raise PlanError(exc.code, f"Routing through stops failed: {exc.code}") from exc

    def _verify_sequence(
        self,
        session: RoutingSession,
        selected: Sequence[CandidateStation],
        initial_fuel_gallons: float,
        snap_tolerance_miles: float,
        *,
        repair: bool,
    ) -> tuple[DrivingRoute, FuelPlan]:
        verified_route = self._route_through(session, selected, repair=repair)
        _verify_stop_snapping(verified_route, selected, snap_tolerance_miles)
        try:
            fuel = _plan_from_verified_legs(
                selected, verified_route.leg_distances_miles, initial_fuel_gallons,
            )
        except FuelOptimizationError as exc:
            raise PlanError(
                "infeasible_fuel",
                "Verified leg distances make the selected fuel stops infeasible",
            ) from exc
        return verified_route, fuel
