import json
from datetime import datetime, timezone
from decimal import Decimal

from django.db import connection
from django.test import TestCase, override_settings

from routes.models import FuelPriceDataset, FuelStation
from routes.services.routing import DrivingRoute
from routes.services.station_search import (
    METERS_PER_MILE,
    CorridorStationLookup,
    corridor_has_candidates,
    find_candidate_stations,
    usable_stations,
)


def line_route(coordinates, distance_miles=None, legs=None, indices=None):
    if distance_miles is None:
        distance_miles = max(0.0, len(coordinates) - 1)
    if legs is None:
        legs = (float(distance_miles),)
    if indices is None:
        indices = (0, len(coordinates) - 1)
    return DrivingRoute(
        geometry={"type": "LineString", "coordinates": coordinates},
        distance_miles=float(distance_miles),
        duration_seconds=3600,
        leg_distances_miles=tuple(float(value) for value in legs),
        waypoint_indices=tuple(indices),
    )


class StationSearchTests(TestCase):
    def setUp(self):
        self.dataset = FuelPriceDataset.objects.create(
            sha256="b" * 64, source_filename="stations.csv", source_row_count=1,
        )

    def usable(
        self, source_id="1", latitude=40.0, longitude=-75.0, price="3.50",
        state="TX", **changes,
    ):
        values = dict(
            dataset=self.dataset,
            source_station_id=source_id,
            name=f"Station {source_id}",
            address="123 Main St",
            city="Town",
            state=state,
            rack_id="1",
            price_usd_per_gallon=Decimal(price),
            price_source_text=price,
            latitude=Decimal(str(latitude)),
            longitude=Decimal(str(longitude)),
            geocoding_status="resolved",
            geocoding_source="osm",
            geocoding_confidence=Decimal("0.9000"),
            location_verified_at=datetime(2026, 10, 7, tzinfo=timezone.utc),
            geocoding_key=f"key-{source_id}",
            geocoding_details={"reason": "exact_store_ref"},
        )
        values.update(changes)
        return FuelStation.objects.create(**values)

    def test_nearby_included_distant_excluded_and_units(self):
        route = line_route([
            [-75.0, 40.0],
            [-74.9, 40.0],
            [-74.8, 40.0],
        ], distance_miles=10)
        near = self.usable("near", latitude=40.01, longitude=-74.9, price="3.10")
        self.usable("far", latitude=41.0, longitude=-74.9, price="2.00")
        candidates = find_candidate_stations(route, corridor_miles=5)
        self.assertEqual([item.source_id for item in candidates], ["near"])
        self.assertEqual(candidates[0].price_usd_per_gallon, near.price_usd_per_gallon)
        self.assertAlmostEqual(candidates[0].route_distance_miles, 5.0, places=2)
        self.assertLess(candidates[0].offset_miles, 1.0)
        self.assertAlmostEqual(
            candidates[0].estimated_detour_miles,
            2 * candidates[0].offset_miles,
            places=9,
        )
        self.assertEqual(candidates[0].location_quality, "exact_store_ref")
        self.assertEqual(candidates[0].geocoding_source, "osm")
        self.assertNotEqual(candidates[0].route_distance_miles, candidates[0].offset_miles)

    def test_reversed_travel_direction_orders_by_progress(self):
        eastbound = line_route([[-75.0, 40.0], [-74.5, 40.0], [-74.0, 40.0]], distance_miles=100)
        westbound = line_route([[-74.0, 40.0], [-74.5, 40.0], [-75.0, 40.0]], distance_miles=100)
        self.usable("west", latitude=40.001, longitude=-74.9, price="3.00")
        self.usable("east", latitude=40.001, longitude=-74.1, price="3.00")
        east_order = [item.source_id for item in find_candidate_stations(eastbound, 5)]
        west_order = [item.source_id for item in find_candidate_stations(westbound, 5)]
        self.assertEqual(east_order, ["west", "east"])
        self.assertEqual(west_order, ["east", "west"])

    def test_equal_position_candidates_stable_by_price_then_id(self):
        route = line_route([[-75.0, 40.0], [-74.0, 40.0]], distance_miles=50)
        self.usable("b", latitude=40.001, longitude=-74.5, price="3.20")
        self.usable("a", latitude=40.001, longitude=-74.5, price="3.20")
        self.usable("c", latitude=40.001, longitude=-74.5, price="3.00")
        ordered = find_candidate_stations(route, 5)
        self.assertEqual([item.source_id for item in ordered], ["c", "a", "b"])
        distances = {item.route_distance_miles for item in ordered}
        self.assertEqual(len(distances), 1)

    def test_loop_assigns_nearest_occurrence_not_unrelated_pass(self):
        route = line_route([
            [-75.00, 40.00],
            [-74.90, 40.00],
            [-74.80, 40.05],
            [-74.90, 40.10],
            [-75.00, 40.10],
            [-75.00, 40.00],
        ], distance_miles=60)
        self.usable("start_side", latitude=40.001, longitude=-74.995, price="3.00")
        self.usable("far_loop", latitude=40.099, longitude=-74.995, price="3.00")
        candidates = {item.source_id: item for item in find_candidate_stations(route, 5)}
        self.assertIn("start_side", candidates)
        self.assertIn("far_loop", candidates)
        self.assertLess(candidates["start_side"].route_distance_miles, 10)
        self.assertGreater(candidates["far_loop"].route_distance_miles, 40)
        self.assertLess(
            candidates["start_side"].offset_miles,
            candidates["start_side"].route_distance_miles,
        )

    def test_driving_distance_scaling_not_straight_line_mileage(self):
        coordinates = [[-75.0, 40.0], [-74.5, 40.0], [-74.0, 40.0]]
        route = line_route(coordinates, distance_miles=80, legs=(80,), indices=(0, 2))
        self.usable("mid", latitude=40.0, longitude=-74.5, price="3.00")
        candidate = find_candidate_stations(route, 1)[0]
        self.assertAlmostEqual(candidate.route_distance_miles, 40.0, places=2)
        self.assertNotAlmostEqual(candidate.route_distance_miles, 0.5, places=2)

    def test_leg_segment_data_scales_each_provider_leg(self):
        route = line_route(
            [[-75.0, 40.0], [-74.5, 40.0], [-74.0, 40.0], [-73.5, 40.0]],
            distance_miles=90,
            legs=(30, 60),
            indices=(0, 1, 3),
        )
        self.usable("on_first", latitude=40.0, longitude=-74.5, price="3.00")
        self.usable("on_second", latitude=40.0, longitude=-74.0, price="3.00")
        self.usable("late_second", latitude=40.0, longitude=-73.75, price="3.00")
        candidates = {item.source_id: item for item in find_candidate_stations(route, 1)}
        self.assertAlmostEqual(candidates["on_first"].route_distance_miles, 30.0, places=2)
        self.assertAlmostEqual(candidates["on_second"].route_distance_miles, 60.0, places=2)
        self.assertAlmostEqual(candidates["late_second"].route_distance_miles, 75.0, places=2)

    def test_unresolved_stations_excluded(self):
        route = line_route([[-75.0, 40.0], [-74.0, 40.0]], distance_miles=10)
        self.usable("ok", latitude=40.001, longitude=-74.5)
        FuelStation.objects.create(
            dataset=self.dataset, source_station_id="pending", name="Pending",
            address="1 Main", city="Town", state="TX", rack_id="1",
            price_usd_per_gallon=Decimal("2.00"), price_source_text="2.00",
            latitude=Decimal("40.001"), longitude=Decimal("-74.5"),
            geocoding_status="review", geocoding_key="",
        )
        self.assertEqual(usable_stations().count(), 1)
        self.assertEqual([item.source_id for item in find_candidate_stations(route, 5)], ["ok"])

    def test_narrow_corridor_empty_is_not_coverage(self):
        route = line_route([[-75.0, 40.0], [-74.0, 40.0]], distance_miles=10)
        self.usable("side", latitude=40.02, longitude=-74.5, price="2.50")
        wide = find_candidate_stations(route, corridor_miles=5)
        narrow = find_candidate_stations(route, corridor_miles=0.1)
        self.assertTrue(corridor_has_candidates(wide))
        self.assertFalse(corridor_has_candidates(narrow))
        self.assertEqual(narrow, ())

    @override_settings(STATION_CORRIDOR_MILES=2)
    def test_default_corridor_setting_and_lookup_protocol(self):
        route = line_route([[-75.0, 40.0], [-74.0, 40.0]], distance_miles=10)
        self.usable("in", latitude=40.005, longitude=-74.5)
        self.usable("out", latitude=40.05, longitude=-74.5)
        lookup = CorridorStationLookup()
        by_default = find_candidate_stations(route)
        by_lookup = lookup.candidates(route, 2)
        self.assertEqual([item.source_id for item in by_default], ["in"])
        self.assertEqual([item.source_id for item in by_lookup], ["in"])

    def test_estimated_detour_is_screening_only_label(self):
        route = line_route([[-75.0, 40.0], [-74.0, 40.0]], distance_miles=20)
        self.usable("side", latitude=40.01, longitude=-74.5)
        candidate = find_candidate_stations(route, 5)[0]
        self.assertGreater(candidate.estimated_detour_miles, 0)
        self.assertAlmostEqual(
            candidate.estimated_detour_miles, 2 * candidate.offset_miles, places=9,
        )
        self.assertNotEqual(candidate.estimated_detour_miles, candidate.route_distance_miles)

    def test_postgis_corridor_query_uses_spatial_index(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostGIS spatial-index integration requires PostgreSQL")
        route = line_route([[-102.0, 32.0], [-101.0, 32.0]], distance_miles=60)
        self.usable("tx", latitude=32.01, longitude=-101.5, state="TX")
        geojson = json.dumps(route.geometry)
        corridor_meters = 5 * METERS_PER_MILE
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL enable_seqscan = off")
            cursor.execute(
                """
                EXPLAIN SELECT id FROM routes_fuelstation
                WHERE geocoding_status = 'resolved'
                  AND location_verified_at IS NOT NULL
                  AND geocoding_key <> ''
                  AND latitude BETWEEN -90 AND 90
                  AND longitude BETWEEN -180 AND 180
                  AND ST_DWithin(
                      ST_SetSRID(ST_MakePoint(longitude::double precision,
                                              latitude::double precision), 4326)::geography,
                      ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)::geography,
                      %s
                  )
                """,
                [geojson, corridor_meters],
            )
            plan = " ".join(row[0] for row in cursor.fetchall())
        self.assertIn("fuelstation_valid_location_gist", plan)
        candidates = find_candidate_stations(route, 5)
        self.assertEqual([item.source_id for item in candidates], ["tx"])
