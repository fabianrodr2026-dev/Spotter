from decimal import Decimal
from unittest import TestCase

from django.core.cache.backends.locmem import LocMemCache

from routes.services.planner import PlanError, RoutePlanner
from routes.services.routing import DrivingRoute, OpenRouteService, RoutingBudget, RoutingError, RoutingSession
from routes.services.station_search import CandidateStation
from routes.validation import Coordinate, PlanInput


START = Coordinate(40.0, -90.0)
FINISH = Coordinate(41.0, -89.0)


def candidate(
    source_id: str,
    *,
    lat: float,
    lon: float,
    price: str,
    route_miles: float,
    offset: float = 0.1,
) -> CandidateStation:
    return CandidateStation(
        source_id=source_id,
        latitude=lat,
        longitude=lon,
        price_usd_per_gallon=Decimal(price),
        route_distance_miles=route_miles,
        offset_miles=offset,
        estimated_detour_miles=2 * offset,
        geocoding_confidence=Decimal("1.0"),
        geocoding_source="test",
        location_quality="resolved_verified",
    )


def route_for(waypoints: tuple[Coordinate, ...], leg_miles: tuple[float, ...], *, snap: tuple[Coordinate, ...] | None = None) -> DrivingRoute:
    points = snap if snap is not None else waypoints
    coordinates = [[point.longitude, point.latitude] for point in points]
    return DrivingRoute(
        geometry={"type": "LineString", "coordinates": coordinates},
        distance_miles=sum(leg_miles),
        duration_seconds=120.0 * len(leg_miles),
        leg_distances_miles=leg_miles,
        waypoint_indices=tuple(range(len(points))),
        provider_version="test",
    )


class FakeLookup:
    def __init__(self, candidates: tuple[CandidateStation, ...]):
        self._candidates = candidates
        self.calls = 0

    def candidates(self, route: DrivingRoute, corridor_miles: float) -> tuple[CandidateStation, ...]:
        self.calls += 1
        return self._candidates


class PlannerTests(TestCase):
    def setUp(self):
        self.cache = LocMemCache(self.id(), {})
        self.cache.clear()
        self.routes: dict[tuple[tuple[float, float], ...], DrivingRoute] = {}
        self.failures: dict[tuple[tuple[float, float], ...], str] = {}
        self.transport_calls = 0

        def transport(request, deadline):
            self.transport_calls += 1
            coords = tuple((latlon[1], latlon[0]) for latlon in request["body"]["coordinates"])
            key = coords
            if key in self.failures:
                raise RoutingError(self.failures[key])
            if key not in self.routes:
                raise AssertionError(f"unexpected waypoints: {key}")
            route = self.routes[key]
            body = {
                "type": "FeatureCollection",
                "features": [{
                    "geometry": route.geometry,
                    "properties": {
                        "summary": {
                            "distance": route.distance_miles * 1609.344,
                            "duration": route.duration_seconds,
                        },
                        "segments": [{"distance": leg * 1609.344} for leg in route.leg_distances_miles],
                        "way_points": list(route.waypoint_indices),
                    },
                }],
                "metadata": {"engine": {"version": route.provider_version}},
            }
            import json
            return {"status": 200, "body": json.dumps(body)}

        self.transport = transport

    def session_factory(self, start: Coordinate, finish: Coordinate) -> RoutingSession:
        provider = OpenRouteService(
            api_key="test-key",
            cache=self.cache,
            budget=RoutingBudget(30),
            transport=self.transport,
        )
        return RoutingSession(provider, start, finish)

    def planner(self, candidates: tuple[CandidateStation, ...], **kwargs) -> RoutePlanner:
        return RoutePlanner(
            self.session_factory,
            FakeLookup(candidates),
            corridor_miles=5,
            **kwargs,
        )

    def test_short_trip_without_stations_uses_final_geometry(self):
        initial = route_for((START, FINISH), (100.0,))
        self.routes[((START.latitude, START.longitude), (FINISH.latitude, FINISH.longitude))] = initial
        result = self.planner(()).plan(PlanInput(START, FINISH, 50))
        self.assertEqual(result.fuel.fuel_purchased_gallons, Decimal("0"))
        self.assertFalse(result.fuel.is_estimate)
        self.assertEqual(result.route.distance_miles, 100.0)
        self.assertEqual(result.routing_attempts, 1)
        self.assertIn("corridor-based heuristic", result.assumptions[1])

    def test_final_cost_uses_verified_leg_distances_not_estimate(self):
        stop = candidate("S1", lat=40.5, lon=-89.5, price="4.00", route_miles=450.0)
        initial = route_for((START, FINISH), (600.0,))
        verified = route_for((START, Coordinate(stop.latitude, stop.longitude), FINISH), (460.0, 160.0))
        self.routes[((START.latitude, START.longitude), (FINISH.latitude, FINISH.longitude))] = initial
        self.routes[(
            (START.latitude, START.longitude),
            (stop.latitude, stop.longitude),
            (FINISH.latitude, FINISH.longitude),
        )] = verified
        result = self.planner((stop,)).plan(PlanInput(START, FINISH, 50))
        self.assertFalse(result.fuel.is_estimate)
        self.assertEqual(result.route.leg_distances_miles, (460.0, 160.0))
        self.assertEqual(result.fuel.fuel_consumed_gallons, Decimal("62"))
        self.assertEqual(result.fuel.fuel_purchased_gallons, Decimal("12"))
        self.assertEqual(result.fuel.total_fuel_cost_usd, Decimal("48.00"))
        self.assertEqual(result.routing_attempts, 2)

    def test_divided_highway_snap_failure_repairs_with_third_call(self):
        bad = candidate("BAD", lat=40.5, lon=-89.5, price="4.00", route_miles=450.0)
        good = candidate("GOOD", lat=40.51, lon=-89.49, price="4.10", route_miles=452.0)
        initial = route_for((START, FINISH), (600.0,))
        snapped_away = Coordinate(40.6, -89.7)
        bad_verified = route_for(
            (START, Coordinate(bad.latitude, bad.longitude), FINISH),
            (450.0, 150.0),
            snap=(START, snapped_away, FINISH),
        )
        good_verified = route_for(
            (START, Coordinate(good.latitude, good.longitude), FINISH),
            (452.0, 148.0),
        )
        self.routes[((START.latitude, START.longitude), (FINISH.latitude, FINISH.longitude))] = initial
        self.routes[(
            (START.latitude, START.longitude),
            (bad.latitude, bad.longitude),
            (FINISH.latitude, FINISH.longitude),
        )] = bad_verified
        self.routes[(
            (START.latitude, START.longitude),
            (good.latitude, good.longitude),
            (FINISH.latitude, FINISH.longitude),
        )] = good_verified
        result = self.planner((bad, good), snap_tolerance_miles=0.5).plan(PlanInput(START, FINISH, 50))
        self.assertTrue(result.repaired)
        self.assertEqual(result.selected_stops[0].source_id, "GOOD")
        self.assertEqual(result.routing_attempts, 3)
        self.assertFalse(result.fuel.is_estimate)

    def test_inaccessible_station_repair_then_inability(self):
        bad = candidate("BAD", lat=40.5, lon=-89.5, price="4.00", route_miles=450.0)
        initial = route_for((START, FINISH), (600.0,))
        self.routes[((START.latitude, START.longitude), (FINISH.latitude, FINISH.longitude))] = initial
        self.failures[(
            (START.latitude, START.longitude),
            (bad.latitude, bad.longitude),
            (FINISH.latitude, FINISH.longitude),
        )] = "no_route"
        with self.assertRaisesRegex(PlanError, "inability_to_plan"):
            self.planner((bad,)).plan(PlanInput(START, FINISH, 50))
        self.assertEqual(self.transport_calls, 2)

    def test_detour_beyond_range_repairs_or_fails_within_budget(self):
        stop = candidate("FAR", lat=40.5, lon=-89.5, price="3.00", route_miles=450.0)
        alt = candidate("ALT", lat=40.52, lon=-89.48, price="5.00", route_miles=455.0)
        initial = route_for((START, FINISH), (600.0,))
        too_long = route_for(
            (START, Coordinate(stop.latitude, stop.longitude), FINISH),
            (520.0, 100.0),
        )
        repaired = route_for(
            (START, Coordinate(alt.latitude, alt.longitude), FINISH),
            (455.0, 145.0),
        )
        self.routes[((START.latitude, START.longitude), (FINISH.latitude, FINISH.longitude))] = initial
        self.routes[(
            (START.latitude, START.longitude),
            (stop.latitude, stop.longitude),
            (FINISH.latitude, FINISH.longitude),
        )] = too_long
        self.routes[(
            (START.latitude, START.longitude),
            (alt.latitude, alt.longitude),
            (FINISH.latitude, FINISH.longitude),
        )] = repaired
        result = self.planner((stop, alt)).plan(PlanInput(START, FINISH, 50))
        self.assertTrue(result.repaired)
        self.assertEqual(result.selected_stops[0].source_id, "ALT")
        self.assertLessEqual(result.routing_attempts, 3)
        self.assertEqual(result.route.leg_distances_miles, (455.0, 145.0))

    def test_repair_never_exceeds_three_attempts(self):
        stop = candidate("S1", lat=40.5, lon=-89.5, price="4.00", route_miles=450.0)
        alt = candidate("S2", lat=40.51, lon=-89.49, price="4.05", route_miles=451.0)
        initial = route_for((START, FINISH), (600.0,))
        self.routes[((START.latitude, START.longitude), (FINISH.latitude, FINISH.longitude))] = initial
        self.failures[(
            (START.latitude, START.longitude),
            (stop.latitude, stop.longitude),
            (FINISH.latitude, FINISH.longitude),
        )] = "no_route"
        self.failures[(
            (START.latitude, START.longitude),
            (alt.latitude, alt.longitude),
            (FINISH.latitude, FINISH.longitude),
        )] = "no_route"
        with self.assertRaisesRegex(PlanError, "inability_to_plan"):
            self.planner((stop, alt)).plan(PlanInput(START, FINISH, 50))
        self.assertLessEqual(self.transport_calls, 3)

    def test_long_access_detour_recalculated_against_final_legs(self):
        stop = candidate("EXIT", lat=40.5, lon=-89.5, price="3.50", route_miles=200.0, offset=0.2)
        initial = route_for((START, FINISH), (300.0,))
        detour = route_for(
            (START, Coordinate(stop.latitude, stop.longitude), FINISH),
            (230.0, 120.0),
        )
        self.routes[((START.latitude, START.longitude), (FINISH.latitude, FINISH.longitude))] = initial
        self.routes[(
            (START.latitude, START.longitude),
            (stop.latitude, stop.longitude),
            (FINISH.latitude, FINISH.longitude),
        )] = detour
        result = self.planner((stop,)).plan(PlanInput(START, FINISH, 25))
        self.assertEqual(result.fuel.fuel_consumed_gallons, Decimal("35"))
        self.assertEqual(sum(result.route.leg_distances_miles), 350.0)
        self.assertNotEqual(result.route.distance_miles, initial.distance_miles)
