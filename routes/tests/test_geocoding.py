import io
import json
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase, SimpleTestCase
from django.urls import reverse

from routes.models import FuelPriceDataset, FuelStation, StationGeocodeCache
from routes.services.geocoding import OsmStationResolver, coverage_report, enrich_stations
from routes.services.state_boundaries import StateBoundaries
from routes.services.station_search import usable_stations


def boundary_document():
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"STUSAB": state}, "geometry": {
            "type": "Polygon", "coordinates": [[[left, 30], [left + 5, 30], [left + 5, 35], [left, 35], [left, 30]]],
        }} for state, left in (("TX", -105), ("OK", -100))
    ]}


def poi(osm_id=1, lon=-102, name="Example Fuel", city="Town", number="123", street="Main Street", **tags):
    return {"type": "node", "id": osm_id, "lat": 32, "lon": lon, "tags": {
        "amenity": "fuel", "name": name, "addr:city": city,
        "addr:housenumber": number, "addr:street": street, **tags,
    }}


def resolver(elements=None, source_hash="source"):
    return OsmStationResolver({"elements": elements if elements is not None else [poi()]},
                              StateBoundaries(boundary_document()), source_hash, "boundaries")


class BoundaryTests(SimpleTestCase):
    def test_polygons_holes_islands_and_non_usa_points(self):
        document = boundary_document()
        polygon = document["features"][0]["geometry"]["coordinates"]
        polygon.append([[-104, 31], [-103, 31], [-103, 32], [-104, 32], [-104, 31]])
        document["features"][0]["geometry"] = {"type": "MultiPolygon", "coordinates": [polygon, [
            [[-110, 31], [-109, 31], [-109, 32], [-110, 32], [-110, 31]],
        ]]}
        boundaries = StateBoundaries(document)
        self.assertTrue(boundaries.contains("TX", -102, 32))
        self.assertFalse(boundaries.contains("OK", -102, 32))
        self.assertFalse(boundaries.contains("TX", -103.5, 31.5))
        self.assertTrue(boundaries.contains("TX", -109.5, 31.5))
        self.assertEqual(boundaries.containing_states(0, 0), [])
        self.assertFalse(boundaries.contains("TX", float("nan"), 32))

    def test_rejects_non_polygon_or_unclosed_ring(self):
        document = boundary_document()
        document["features"][0]["geometry"]["coordinates"][0].pop()
        with self.assertRaises(ValueError):
            StateBoundaries(document)


class GeocodingTests(TestCase):
    def setUp(self):
        self.dataset = FuelPriceDataset.objects.create(sha256="a" * 64, source_filename="fixture.csv", source_row_count=1)

    def station(self, source_id="1", **changes):
        values = dict(dataset=self.dataset, source_station_id=source_id, name="Example Fuel",
                      address="123 Main St", city="Town", state="TX", rack_id="9",
                      price_usd_per_gallon=Decimal("3.12345678"), price_source_text="3.12345678")
        values.update(changes)
        return FuelStation.objects.create(**values)

    def test_unique_precise_match_and_duplicate_names_in_other_states(self):
        texas = self.station()
        oklahoma = self.station("2", state="OK")
        source = resolver([poi(1, -102), poi(2, -97)])
        enrich_stations(FuelStation.objects.all(), source)
        texas.refresh_from_db()
        oklahoma.refresh_from_db()
        self.assertEqual(texas.longitude, Decimal("-102"))
        self.assertEqual(oklahoma.longitude, Decimal("-97"))
        self.assertEqual(usable_stations().count(), 2)
        self.assertEqual(texas.geocoding_details["osm_id"], "node/1")
        self.assertEqual(texas.price_usd_per_gallon, Decimal("3.12345678"))

    def test_highway_city_only_wrong_state_and_ambiguous_matches_are_excluded(self):
        for source_id, address, elements, reason in [
            ("1", "I-40 Exit 10", [poi()], "ambiguous_highway_exit_or_intersection"),
            ("2", "123 Main St", [poi(number="", street="")], "insufficient_or_conflicting_location_evidence"),
            ("3", "123 Main St", [poi(lon=-97)], "insufficient_or_conflicting_location_evidence"),
            ("4", "123 Main St", [poi(), poi(2)], "multiple_matches"),
            ("5", "123 Main St", [poi(**{"addr:country": "CA"})], "insufficient_or_conflicting_location_evidence"),
            ("6", "123 Main St", [poi(**{"addr:state": "OK"})], "insufficient_or_conflicting_location_evidence"),
            ("7", "123 Main St", [poi(lon=0)], "insufficient_or_conflicting_location_evidence"),
            ("8", "123 Main St", [poi(**{"addr:state": "Oklahoma"})], "insufficient_or_conflicting_location_evidence"),
        ]:
            with self.subTest(source_id=source_id):
                station = self.station(source_id, address=address)
                enrich_stations(FuelStation.objects.filter(pk=station.pk), resolver(elements, source_id))
                station.refresh_from_db()
                self.assertEqual(station.geocoding_status, "review")
                self.assertEqual(station.geocoding_details["reason"], reason)
                self.assertIsNone(station.latitude)
                self.assertIsNone(station.location_verified_at)
        self.assertFalse(usable_stations().exists())

    def test_highway_exit_requires_matching_store_number(self):
        station = self.station(name="Example Fuel #99", address="I-40 Exit 10")
        other_city = poi(2, lon=-102.1, city="Other", name="Example Fuel")
        enrich_stations(FuelStation.objects.filter(pk=station.pk), resolver([poi(name="Example Fuel"), other_city]))
        station.refresh_from_db()
        self.assertEqual(station.geocoding_status, "review")
        self.assertIsNone(station.longitude)
        self.assertEqual(station.geocoding_details["reason"], "ambiguous_highway_exit_or_intersection")
        enrich_stations(FuelStation.objects.filter(pk=station.pk), resolver([poi(name="Example Fuel #99"), other_city], "numbered"))
        station.refresh_from_db()
        self.assertEqual(station.geocoding_status, "resolved")
        self.assertEqual(station.longitude, Decimal("-102"))
        self.assertEqual(station.geocoding_details["reason"], "unique_brand_store_number_city_state_match")
        self.assertNotEqual(station.latitude, Decimal("0"))
        ambiguous = self.station("9", name="Example Fuel", address="I-40 Exit 11")
        enrich_stations(FuelStation.objects.filter(pk=ambiguous.pk), resolver([poi(name="Example Fuel")], "hwy"))
        ambiguous.refresh_from_db()
        self.assertEqual(ambiguous.geocoding_status, "review")
        self.assertIsNone(ambiguous.latitude)

    def test_contested_brand_city_changes_cache_key(self):
        station = self.station(name="Example Fuel #99", address="I-40 Exit 10")
        source = resolver([poi(name="Example Fuel")])
        first_key = source.key(station)
        from routes.services.geocoding import contested_brand_cities
        self.station("2", name="Example Fuel #100", address="I-40 Exit 11")
        contested = OsmStationResolver(
            {"elements": [poi(name="Example Fuel")]},
            StateBoundaries(boundary_document()), "source", "boundaries",
            contested_brands=contested_brand_cities(FuelStation.objects.all()),
        )
        self.assertNotEqual(first_key, contested.key(station))
        enrich_stations(FuelStation.objects.filter(pk=station.pk), contested)
        station.refresh_from_db()
        self.assertEqual(station.geocoding_details["reason"], "multiple_same_brand_stations_in_city")

    def test_matching_osm_store_ref_resolves_highway_but_wrong_ref_does_not(self):
        station = self.station(name="Example Fuel #99", address="I-40 Exit 10")
        enrich_stations(FuelStation.objects.filter(pk=station.pk), resolver([poi(name="Example Fuel", ref="98")]))
        station.refresh_from_db()
        self.assertEqual(station.geocoding_status, "review")
        enrich_stations(FuelStation.objects.filter(pk=station.pk), resolver([poi(name="Example Fuel", ref="99")], "matching-ref"))
        station.refresh_from_db()
        self.assertEqual(station.geocoding_status, "resolved")
        self.assertEqual(station.geocoding_details["osm_id"], "node/1")
        self.assertEqual(station.geocoding_details["reason"], "unique_brand_store_number_city_state_match")

    def test_contested_same_brand_city_does_not_share_one_poi(self):
        first = self.station("1", name="Example Fuel #1", address="I-40 Exit 1")
        second = self.station("2", name="Example Fuel #2", address="I-40 Exit 2")
        from routes.services.geocoding import contested_brand_cities
        source = OsmStationResolver(
            {"elements": [poi(name="Example Fuel")]},
            StateBoundaries(boundary_document()),
            "contested",
            "boundaries",
            contested_brands=contested_brand_cities(FuelStation.objects.all()),
        )
        enrich_stations(FuelStation.objects.all(), source)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.geocoding_status, "review")
        self.assertEqual(second.geocoding_status, "review")
        self.assertEqual(first.geocoding_details["reason"], "multiple_same_brand_stations_in_city")
        self.assertIsNone(first.latitude)
        self.assertFalse(usable_stations().exists())

    def test_non_fuel_invalid_coordinates_and_unsupported_states(self):
        station = self.station()
        source = resolver([poi(1, float("inf")), poi(2, amenity="townhall")])
        self.assertEqual(source.resolve(station)["status"], "not_found")
        self.assertEqual(source.ignored_elements, 2)
        for state in ("ON", "AK", "HI"):
            station.state = state
            self.assertEqual(source.resolve(station)["status"], "unsupported")

    def test_feature_center_is_not_an_automatic_station_location(self):
        station = self.station()
        element = poi()
        element.update(type="way", center={"lat": 32, "lon": -102})
        result = resolver([element]).resolve(station)
        self.assertEqual(result["status"], "review")
        self.assertEqual(result["candidates"][0]["precision"], "osm_feature_bbox_center")
        self.assertIsNone(result["latitude"])

    def test_second_run_reuses_results_and_cross_dataset_cache(self):
        self.station()
        source = resolver()
        first = enrich_stations(FuelStation.objects.all(), source)
        with patch.object(source, "resolve", side_effect=AssertionError("must reuse cache")):
            second = enrich_stations(FuelStation.objects.all(), source)
            other = FuelPriceDataset.objects.create(sha256="b" * 64, source_filename="other.csv", source_row_count=1)
            self.station("2", dataset=other)
            third = enrich_stations(FuelStation.objects.filter(dataset=other), source)
        self.assertEqual(first["processed"], 1)
        self.assertEqual(second["skipped"], 1)
        self.assertEqual(third["cache_hits"], 1)
        self.assertEqual(StationGeocodeCache.objects.count(), 1)

    def test_not_found_is_cached_but_source_and_input_changes_recompute(self):
        station = self.station()
        empty = resolver([])
        enrich_stations(FuelStation.objects.all(), empty)
        with patch.object(empty, "resolve", side_effect=AssertionError("negative cache")):
            self.assertEqual(enrich_stations(FuelStation.objects.all(), empty)["skipped"], 1)
        enrich_stations(FuelStation.objects.all(), resolver(source_hash="new-source"))
        self.assertEqual(usable_stations().count(), 1)
        station.address = "987 Other Road"
        station.save(update_fields=["address"])
        enrich_stations(FuelStation.objects.all(), resolver(source_hash="new-source"))
        self.assertFalse(usable_stations().exists())

    def test_interruption_retains_completed_batches_and_resume_finishes(self):
        self.station()
        self.station("2", address="999 Main St")
        source = resolver()
        original = source.resolve
        def interrupt(station):
            if station.source_station_id == "2":
                raise RuntimeError("interrupted")
            return original(station)
        with patch.object(source, "resolve", side_effect=interrupt):
            with self.assertRaises(RuntimeError):
                enrich_stations(FuelStation.objects.all(), source, batch_size=1)
        self.assertEqual(FuelStation.objects.filter(geocoding_status="resolved").count(), 1)
        self.assertEqual(FuelStation.objects.filter(geocoding_status="pending").count(), 1)
        report = enrich_stations(FuelStation.objects.all(), source, batch_size=1)
        self.assertEqual(report["skipped"], 1)
        self.assertEqual(report["processed"], 1)
        self.assertEqual(StationGeocodeCache.objects.count(), 2)

    def test_limited_runs_advance_past_completed_stations(self):
        self.station()
        self.station("2", address="999 Main St")
        source = resolver()
        self.assertEqual(enrich_stations(FuelStation.objects.all(), source, limit=1)["processed"], 1)
        second = enrich_stations(FuelStation.objects.all(), source, limit=1)
        self.assertEqual(second["skipped"], 1)
        self.assertEqual(second["processed"], 1)
        self.assertFalse(FuelStation.objects.filter(geocoding_status="pending").exists())

    def test_command_limit_coverage_export_and_offline_second_run(self):
        self.station()
        self.station("2", state="ON")
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            (directory / "osm-fuel.json").write_text(json.dumps({"elements": [poi()]}))
            (directory / "us-states.geojson").write_text(json.dumps(boundary_document()))
            output = io.StringIO()
            call_command("geocode_stations", cache_dir=directory, limit=1, stdout=output,
                         export_osm=directory / "export.json")
            self.assertEqual(json.loads(output.getvalue())["run"]["processed"], 1)
            exported = json.loads((directory / "export.json").read_text(encoding="utf-8"))
            self.assertIn("ODbL", exported["attribution"])
            self.assertEqual(exported["elements"], [poi()])
            with patch("routes.services.location_sources.urlopen", side_effect=AssertionError("no network")):
                call_command("geocode_stations", cache_dir=directory, download=True, stdout=io.StringIO(), stderr=io.StringIO())
                call_command("geocode_stations", coverage_only=True, stdout=io.StringIO())
        coverage = coverage_report(FuelStation.objects.all())
        self.assertEqual(coverage["total_stations"], 2)
        self.assertEqual(coverage["usable_stations"], 1)
        self.assertEqual(coverage["excluded_stations"], 1)
        self.assertEqual(coverage["usable_percent"], 50)

    def test_missing_boundary_fails_before_any_updates(self):
        self.station(state="CA")
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            (directory / "osm-fuel.json").write_text(json.dumps({"elements": [poi()]}))
            (directory / "us-states.geojson").write_text(json.dumps(boundary_document()))
            with self.assertRaisesMessage(CommandError, "Missing Census state polygons: CA"):
                call_command("geocode_stations", cache_dir=directory)
        self.assertEqual(StationGeocodeCache.objects.count(), 0)

    def test_route_requests_never_invoke_enrichment_or_network(self):
        from routes.services.routing import RoutingError

        with patch("routes.services.geocoding.enrich_stations", side_effect=AssertionError("bulk geocoding")), \
             patch("routes.services.location_sources.urlopen", side_effect=AssertionError("network")), \
             patch(
                 "routes.services.plan_api.create_routing_session",
                 side_effect=RoutingError("provider_unavailable"),
             ):
            response = self.client.post(reverse("plan-route"), data=json.dumps({
                "start": {"latitude": 40, "longitude": -75}, "finish": {"latitude": 41, "longitude": -74},
            }), content_type="application/json")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "provider_unavailable")
        self.assertEqual(StationGeocodeCache.objects.count(), 0)

    def test_postgis_partial_spatial_index_exists_and_is_usable(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostGIS spatial-index integration requires PostgreSQL")
        self.station()
        enrich_stations(FuelStation.objects.all(), resolver())
        with connection.cursor() as cursor:
            cursor.execute("SELECT indexdef FROM pg_indexes WHERE indexname = 'fuelstation_valid_location_gist'")
            definition = cursor.fetchone()[0]
            self.assertIn("USING gist", definition)
            self.assertIn("resolved", definition)
            cursor.execute("SET LOCAL enable_seqscan = off")
            cursor.execute("""EXPLAIN SELECT id FROM routes_fuelstation
                WHERE geocoding_status = 'resolved' AND location_verified_at IS NOT NULL AND geocoding_key <> ''
                AND latitude BETWEEN -90 AND 90 AND longitude BETWEEN -180 AND 180
                AND ST_DWithin(ST_SetSRID(ST_MakePoint(longitude::double precision,
                    latitude::double precision), 4326)::geography,
                    ST_SetSRID(ST_MakePoint(-102, 32), 4326)::geography, 1000)""")
            self.assertIn("fuelstation_valid_location_gist", " ".join(row[0] for row in cursor.fetchall()))
