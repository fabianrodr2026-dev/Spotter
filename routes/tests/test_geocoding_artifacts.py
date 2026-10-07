from django.conf import settings
from django.test import SimpleTestCase

from routes.services.location_sources import read_json
from routes.services.state_boundaries import StateBoundaries


class CachedGeographyTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        document, cls.boundary_hash = read_json(settings.BASE_DIR / "artifacts/us-states-2024.geojson.gz")
        cls.boundaries = StateBoundaries(document)

    def test_real_polygons_check_known_us_cities_and_foreign_locations(self):
        for state, lon, lat in (("NY", -74.006, 40.7128), ("CA", -118.2437, 34.0522), ("OK", -97.5164, 35.4676)):
            with self.subTest(state=state):
                self.assertEqual(self.boundaries.containing_states(lon, lat), [state])
        for lon, lat in ((-79.3832, 43.6532), (-99.1332, 19.4326), (0, 0)):
            self.assertEqual(self.boundaries.containing_states(lon, lat), [])

    def test_same_name_osm_points_are_in_distinct_expected_states(self):
        document, _ = read_json(settings.BASE_DIR / "artifacts/osm-fuel-sample.json")
        elements = {element["id"]: element for element in document["elements"]}
        for osm_id, state in ((11445778092, "OK"), (13132068836, "TX")):
            element = elements[osm_id]
            self.assertEqual(element["tags"]["name"], "QuikTrip")
            self.assertEqual(self.boundaries.containing_states(element["lon"], element["lat"]), [state])
            self.assertFalse(self.boundaries.contains("WI", element["lon"], element["lat"]))
