from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from routes.models import FuelPriceDataset, FuelStation
from routes.services.plan_api import PlanRequestError, plan_route_payload, status_for_error
from routes.services.planner import RoutePlanner
from routes.services.routing import DrivingRoute, OpenRouteService, RoutingBudget, RoutingError, RoutingSession
from routes.services.station_search import CandidateStation
from routes.services.usa import in_contiguous_usa, load_contiguous_boundaries
from routes.validation import Coordinate


NYC = {"latitude": 40.7128, "longitude": -74.0060}
PHILLY = {"latitude": 39.9526, "longitude": -75.1652}
TORONTO = {"latitude": 43.6532, "longitude": -79.3832}


def route_for(waypoints: tuple[Coordinate, ...], leg_miles: tuple[float, ...]) -> DrivingRoute:
    coordinates = [[point.longitude, point.latitude] for point in waypoints]
    return DrivingRoute(
        geometry={"type": "LineString", "coordinates": coordinates},
        distance_miles=sum(leg_miles),
        duration_seconds=120.0 * len(leg_miles),
        leg_distances_miles=leg_miles,
        waypoint_indices=tuple(range(len(waypoints))),
        provider_version="test",
    )


class FakeLookup:
    def __init__(self, candidates: tuple[CandidateStation, ...] = ()):
        self._candidates = candidates
        self.calls = 0

    def candidates(self, route: DrivingRoute, corridor_miles: float) -> tuple[CandidateStation, ...]:
        self.calls += 1
        return self._candidates


class ApiPlanTests(TestCase):
    def setUp(self):
        cache.clear()
        load_contiguous_boundaries.cache_clear()
        self.dataset = FuelPriceDataset.objects.create(
            sha256="a" * 64,
            source_filename="fixture.csv",
            source_row_count=1,
        )
        FuelStation.objects.create(
            dataset=self.dataset,
            source_station_id="S1",
            name="Alpha <Station> & Co",
            address="1 Main",
            city="Town",
            state="NY",
            rack_id="1",
            price_usd_per_gallon=Decimal("3.50"),
            price_source_text="3.50",
            source_row_numbers=[1],
            source_names=["Alpha <Station> & Co"],
            latitude=Decimal("40.5"),
            longitude=Decimal("-74.5"),
            geocoding_status="resolved",
            geocoding_source="test",
            geocoding_key="key",
            location_verified_at=timezone.now(),
        )
        self.routes: dict[tuple[tuple[float, float], ...], DrivingRoute] = {}
        self.failures: dict[tuple[tuple[float, float], ...], str] = {}
        self.transport_calls = 0

        def transport(request, deadline):
            self.transport_calls += 1
            coords = tuple((latlon[1], latlon[0]) for latlon in request["body"]["coordinates"])
            if coords in self.failures:
                raise RoutingError(self.failures[coords])
            if coords not in self.routes:
                raise AssertionError(f"unexpected waypoints: {coords}")
            route = self.routes[coords]
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
            return {"status": 200, "body": json.dumps(body)}

        self.transport = transport
        self.lookup = FakeLookup()

    def session_factory(self, start: Coordinate, finish: Coordinate) -> RoutingSession:
        provider = OpenRouteService(
            api_key="test-key",
            cache=cache,
            budget=RoutingBudget(30),
            transport=self.transport,
        )
        return RoutingSession(provider, start, finish)

    def planner_factory(self) -> RoutePlanner:
        return RoutePlanner(
            self.session_factory,
            self.lookup,
            corridor_miles=5,
            snap_tolerance_miles=0.5,
        )

    def plan_with_mocks(self, payload: dict):
        return plan_route_payload(
            json.dumps(payload).encode("utf-8"),
            planner_factory=self.planner_factory,
        )

    def test_invalid_types_rejected(self):
        response = self.client.post(
            reverse("plan-route"),
            data=json.dumps({
                "start": {"latitude": "40.7", "longitude": -74.0},
                "finish": PHILLY,
            }),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_input")

    def test_fuel_bounds_rejected(self):
        response = self.client.post(
            reverse("plan-route"),
            data=json.dumps({"start": NYC, "finish": PHILLY, "initial_fuel_gallons": 51}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_input")

    def test_non_usa_endpoint_rejected(self):
        response = self.client.post(
            reverse("plan-route"),
            data=json.dumps({"start": TORONTO, "finish": NYC}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "unsupported_location")

    def test_successful_plan_returns_map_and_totals(self):
        start = Coordinate(NYC["latitude"], NYC["longitude"])
        finish = Coordinate(PHILLY["latitude"], PHILLY["longitude"])
        self.routes[((start.latitude, start.longitude), (finish.latitude, finish.longitude))] = route_for(
            (start, finish), (90.0,),
        )
        payload, status = self.plan_with_mocks({
            "start": NYC,
            "finish": PHILLY,
            "initial_fuel_gallons": 50,
        })
        self.assertEqual(status, 200)
        self.assertEqual(payload["cache_status"], "miss")
        self.assertAlmostEqual(payload["route"]["distance_miles"], 90.0, places=5)
        self.assertEqual(payload["totals"]["fuel_purchased_gallons"], "0")
        self.assertEqual(payload["totals"]["total_fuel_cost_usd"], "0.00")
        self.assertTrue(payload["map_url"].endswith(f"/map/{payload['plan_id']}/"))
        self.assertEqual(payload["routing_attempts"], 1)
        self.assertEqual(payload["dataset_version"], self.dataset.sha256)
        self.assertLessEqual(payload["routing_attempts"], 3)

        map_response = self.client.get(payload["map_url"])
        self.assertEqual(map_response.status_code, 200)
        self.assertContains(map_response, "OpenStreetMap")
        self.assertContains(map_response, 'data-state="ready"')
        body = map_response.content.decode("utf-8")
        self.assertIn("plan-data", body)
        self.assertIn("LineString", body)
        self.assertIn(str(payload["route"]["distance_miles"]), body)

        calls_before_refresh = self.transport_calls
        refresh = self.client.get(payload["map_url"])
        self.assertEqual(refresh.status_code, 200)
        self.assertEqual(self.transport_calls, calls_before_refresh)

    def test_warm_cache_avoids_routing(self):
        start = Coordinate(NYC["latitude"], NYC["longitude"])
        finish = Coordinate(PHILLY["latitude"], PHILLY["longitude"])
        self.routes[((start.latitude, start.longitude), (finish.latitude, finish.longitude))] = route_for(
            (start, finish), (90.0,),
        )
        body = {"start": NYC, "finish": PHILLY, "initial_fuel_gallons": 40}
        first, _ = self.plan_with_mocks(body)
        calls = self.transport_calls
        second, _ = self.plan_with_mocks(body)
        self.assertEqual(second["cache_status"], "hit")
        self.assertEqual(second["plan_id"], first["plan_id"])
        self.assertEqual(second["routing_attempts"], 0)
        self.assertEqual(self.transport_calls, calls)

    def test_new_usable_station_invalidates_cached_plan(self):
        start = Coordinate(NYC["latitude"], NYC["longitude"])
        finish = Coordinate(PHILLY["latitude"], PHILLY["longitude"])
        self.routes[((start.latitude, start.longitude), (finish.latitude, finish.longitude))] = route_for(
            (start, finish), (90.0,),
        )
        body = {"start": NYC, "finish": PHILLY}
        first, _ = self.plan_with_mocks(body)
        station = FuelStation.objects.get(dataset=self.dataset, source_station_id="S1")
        station.geocoding_status = "review"
        station.save(update_fields=["geocoding_status"])
        second, _ = self.plan_with_mocks(body)
        self.assertEqual(second["cache_status"], "miss")
        self.assertNotEqual(first["plan_id"], second["plan_id"])

    def test_usa_endpoints_allow_route_through_canada(self):
        start = Coordinate(NYC["latitude"], NYC["longitude"])
        finish = Coordinate(PHILLY["latitude"], PHILLY["longitude"])
        route = route_for((start, finish), (90.0,))
        route.geometry["coordinates"].insert(1, [-79.3832, 43.6532])
        from dataclasses import replace
        route = replace(route, waypoint_indices=(0, 2))
        self.routes[((start.latitude, start.longitude), (finish.latitude, finish.longitude))] = route
        payload, status = self.plan_with_mocks({"start": NYC, "finish": PHILLY})
        self.assertEqual(status, 200)
        self.assertEqual(payload["route"]["geometry"], route.geometry)

    def test_station_metadata_is_scoped_to_plan_dataset(self):
        from routes.services.plan_api import _station_details
        other = FuelPriceDataset.objects.create(
            sha256="b" * 64, source_filename="other.csv", source_row_count=1,
        )
        station = FuelStation.objects.get(dataset=self.dataset, source_station_id="S1")
        station.pk = None
        station.dataset = other
        station.name = "Wrong version"
        station.save()
        details = _station_details(["S1"], self.dataset.sha256)
        self.assertEqual(details["S1"]["name"], "Alpha <Station> & Co")

    def test_failed_plan_does_not_create_success_map(self):
        start = Coordinate(NYC["latitude"], NYC["longitude"])
        finish = Coordinate(PHILLY["latitude"], PHILLY["longitude"])
        self.failures[((start.latitude, start.longitude), (finish.latitude, finish.longitude))] = "no_route"
        with self.assertRaises(PlanRequestError) as ctx:
            self.plan_with_mocks({"start": NYC, "finish": PHILLY})
        self.assertEqual(ctx.exception.code, "no_route")
        self.assertEqual(status_for_error(ctx.exception.code), 422)
        unknown = self.client.get(reverse("map-plan", kwargs={"plan_id": "missingplanid"}))
        self.assertEqual(unknown.status_code, 404)
        self.assertContains(unknown, "unknown or has expired", status_code=404)

    def test_provider_unavailable_status(self):
        start = Coordinate(NYC["latitude"], NYC["longitude"])
        finish = Coordinate(PHILLY["latitude"], PHILLY["longitude"])
        self.failures[((start.latitude, start.longitude), (finish.latitude, finish.longitude))] = (
            "provider_unavailable"
        )
        with self.assertRaises(PlanRequestError) as ctx:
            self.plan_with_mocks({"start": NYC, "finish": PHILLY})
        self.assertEqual(ctx.exception.code, "provider_unavailable")
        self.assertEqual(status_for_error(ctx.exception.code), 503)

    def test_http_endpoint_returns_structured_error(self):
        response = self.client.post(
            reverse("plan-route"),
            data=json.dumps({"start": TORONTO, "finish": NYC}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "unsupported_location")

    def test_http_success_through_view(self):
        start = Coordinate(NYC["latitude"], NYC["longitude"])
        finish = Coordinate(PHILLY["latitude"], PHILLY["longitude"])
        self.routes[((start.latitude, start.longitude), (finish.latitude, finish.longitude))] = route_for(
            (start, finish), (80.0,),
        )

        def fake_payload(body, planner_factory=None):
            return plan_route_payload(body, planner_factory=self.planner_factory)

        with patch("routes.views.plan_route_payload", side_effect=fake_payload):
            response = self.client.post(
                reverse("plan-route"),
                data=json.dumps({"start": NYC, "finish": PHILLY}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("plan_id", data)
        self.assertAlmostEqual(data["route"]["distance_miles"], 80.0, places=5)

    def test_map_escapes_station_names(self):
        from routes.services.plan_store import store_successful_plan

        plan = {
            "plan_id": "escape1",
            "map_url": "/map/escape1/",
            "route": {
                "geometry": {"type": "LineString", "coordinates": [[-74.0, 40.7], [-75.1, 39.9]]},
                "distance_miles": 90,
                "duration_seconds": 100,
            },
            "stops": [{
                "source_id": "S1",
                "name": "Alpha <Station> & Co",
                "city": "Town",
                "state": "NY",
                "latitude": 40.5,
                "longitude": -74.5,
                "price_usd_per_gallon": "3.50",
                "gallons_purchased": "5",
                "cost_usd": "17.50",
            }],
            "totals": {
                "initial_fuel_gallons": "50",
                "fuel_consumed_gallons": "9",
                "fuel_purchased_gallons": "5",
                "fuel_remaining_gallons": "46",
                "total_fuel_cost_usd": "17.50",
            },
            "assumptions": [],
            "routing_attempts": 2,
        }
        store_successful_plan("escape1", {"k": 1}, plan)
        response = self.client.get(reverse("map-plan", kwargs={"plan_id": "escape1"}))
        content = response.content.decode("utf-8")
        self.assertEqual(response.status_code, 200)
        self.assertIn("\\u003CStation\\u003E", content)
        self.assertNotIn("<Station>", content)

    def test_missing_map_id_page(self):
        response = self.client.get(reverse("map"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "successful plan response")

    def test_long_route_with_multiple_fuel_stops(self):
        start = Coordinate(NYC["latitude"], NYC["longitude"])
        finish = Coordinate(34.0522, -118.2437)
        stops = (
            CandidateStation(
                source_id="S1",
                latitude=40.0,
                longitude=-80.0,
                price_usd_per_gallon=Decimal("3.50"),
                route_distance_miles=400.0,
                offset_miles=0.1,
                estimated_detour_miles=0.2,
                geocoding_confidence=Decimal("1.0"),
                geocoding_source="test",
                location_quality="resolved_verified",
            ),
            CandidateStation(
                source_id="S2",
                latitude=39.0,
                longitude=-90.0,
                price_usd_per_gallon=Decimal("3.20"),
                route_distance_miles=800.0,
                offset_miles=0.1,
                estimated_detour_miles=0.2,
                geocoding_confidence=Decimal("1.0"),
                geocoding_source="test",
                location_quality="resolved_verified",
            ),
            CandidateStation(
                source_id="S3",
                latitude=37.0,
                longitude=-100.0,
                price_usd_per_gallon=Decimal("3.40"),
                route_distance_miles=1200.0,
                offset_miles=0.1,
                estimated_detour_miles=0.2,
                geocoding_confidence=Decimal("1.0"),
                geocoding_source="test",
                location_quality="resolved_verified",
            ),
            CandidateStation(
                source_id="S4",
                latitude=35.5,
                longitude=-110.0,
                price_usd_per_gallon=Decimal("3.10"),
                route_distance_miles=1600.0,
                offset_miles=0.1,
                estimated_detour_miles=0.2,
                geocoding_confidence=Decimal("1.0"),
                geocoding_source="test",
                location_quality="resolved_verified",
            ),
            CandidateStation(
                source_id="S5",
                latitude=34.5,
                longitude=-115.0,
                price_usd_per_gallon=Decimal("3.60"),
                route_distance_miles=2000.0,
                offset_miles=0.1,
                estimated_detour_miles=0.2,
                geocoding_confidence=Decimal("1.0"),
                geocoding_source="test",
                location_quality="resolved_verified",
            ),
        )
        self.lookup = FakeLookup(stops)
        waypoints = (start, *(Coordinate(s.latitude, s.longitude) for s in stops), finish)
        legs = (400.0, 400.0, 400.0, 400.0, 400.0, 400.0)
        self.routes[((start.latitude, start.longitude), (finish.latitude, finish.longitude))] = route_for(
            (start, finish), (2400.0,),
        )
        self.routes[tuple((p.latitude, p.longitude) for p in waypoints)] = route_for(waypoints, legs)
        for stop in stops[1:]:
            FuelStation.objects.create(
                dataset=self.dataset,
                source_station_id=stop.source_id,
                name=f"Station {stop.source_id}",
                address="1 Main",
                city="Town",
                state="OH",
                rack_id="1",
                price_usd_per_gallon=stop.price_usd_per_gallon,
                price_source_text=str(stop.price_usd_per_gallon),
                source_row_numbers=[1],
                source_names=[f"Station {stop.source_id}"],
                latitude=Decimal(str(stop.latitude)),
                longitude=Decimal(str(stop.longitude)),
                geocoding_status="resolved",
                geocoding_source="test",
                geocoding_key=stop.source_id,
                location_verified_at=timezone.now(),
            )
        payload, status = self.plan_with_mocks({
            "start": NYC,
            "finish": {"latitude": finish.latitude, "longitude": finish.longitude},
            "initial_fuel_gallons": 50,
        })
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(payload["stops"]), 2)
        self.assertLessEqual(payload["routing_attempts"], 3)
        purchased = Decimal(payload["totals"]["fuel_purchased_gallons"])
        consumed = Decimal(payload["totals"]["fuel_consumed_gallons"])
        remaining = Decimal(payload["totals"]["fuel_remaining_gallons"])
        initial = Decimal(payload["totals"]["initial_fuel_gallons"])
        self.assertAlmostEqual(float(initial + purchased - consumed), float(remaining), places=6)
        map_response = self.client.get(payload["map_url"])
        self.assertEqual(map_response.status_code, 200)
        self.assertIn(payload["plan_id"], map_response.content.decode("utf-8"))


@override_settings(USA_BOUNDARY_PATH="artifacts/us-states-2024.geojson.gz")
class UsaValidationUnitTests(TestCase):
    def setUp(self):
        load_contiguous_boundaries.cache_clear()

    def test_nyc_inside_philly_inside_toronto_outside(self):
        self.assertTrue(in_contiguous_usa(Coordinate(40.7128, -74.0060)))
        self.assertTrue(in_contiguous_usa(Coordinate(39.9526, -75.1652)))
        self.assertFalse(in_contiguous_usa(Coordinate(43.6532, -79.3832)))
