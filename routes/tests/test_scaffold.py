import json

from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from routes.checks import required_configuration


class ScaffoldTests(SimpleTestCase):
    def test_health(self):
        response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_map_scaffold_renders(self):
        response = self.client.get(reverse("map"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "routes/map.js")

    def test_missing_provider_key_has_useful_check(self):
        with override_settings(ROUTING_PROVIDER_API_KEY=""):
            errors = required_configuration(None)
        self.assertIn("routes.E003", [error.id for error in errors])

    def test_valid_request_reports_planning_pending(self):
        payload = {"start": {"latitude": 40, "longitude": -75}, "finish": {"latitude": 41, "longitude": -74}}
        response = self.client.post(reverse("plan-route"), data=json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, 501)
        self.assertEqual(response.json()["error"]["code"], "not_implemented")

    def test_invalid_coordinate_rejected(self):
        payload = {"start": {"latitude": "40", "longitude": -75}, "finish": {"latitude": 41, "longitude": -74}}
        response = self.client.post(reverse("plan-route"), data=json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_input")
