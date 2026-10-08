import io
import json
import os
import subprocess
import tempfile
import time
from contextlib import redirect_stdout
from pathlib import Path
from unittest import TestCase, skipUnless
from unittest.mock import Mock, patch

from django.core.cache.backends.locmem import LocMemCache

from routes.services.routing import (
    OpenRouteService, RoutingBudget, RoutingError, RoutingSession, post_once,
)
from routes.services.routing_http import main as http_main
from routes.validation import Coordinate


START = Coordinate(40.7484, -73.9857)
FINISH = Coordinate(40.7308, -73.9973)
STOP = Coordinate(40.74, -73.99)


def response(waypoints=2):
    return {
        "type": "FeatureCollection",
        "features": [{
            "geometry": {"type": "LineString", "coordinates": [
                [-73.9857 + i * .001, 40.7484] for i in range(waypoints)
            ]},
            "properties": {
                "summary": {"distance": 1609.344 * (waypoints - 1), "duration": 120},
                "segments": [{"distance": 1609.344} for _ in range(waypoints - 1)],
                "way_points": list(range(waypoints)),
            },
        }],
        "metadata": {"engine": {"version": "9.10.0"},
                     "attribution": "openrouteservice.org | OpenStreetMap contributors"},
    }


def http_response(waypoints=2):
    return {"status": 200, "body": json.dumps(response(waypoints))}


class RoutingTests(TestCase):
    def setUp(self):
        self.cache = LocMemCache(self.id(), {})
        self.cache.clear()
        self.transport = Mock(return_value=http_response())
        self.provider = self.provider_with()

    def provider_with(self, **kwargs):
        return OpenRouteService(
            api_key="test-key", cache=self.cache,
            budget=kwargs.pop("budget", RoutingBudget(30)),
            transport=self.transport, **kwargs,
        )

    def test_success_units_geometry_and_cache(self):
        result = self.provider.route((START, FINISH))
        self.assertEqual(result.distance_miles, 1)
        self.assertEqual(result.leg_distances_miles, (1,))
        self.assertEqual(result.duration_seconds, 120)
        self.assertEqual(result.waypoint_indices, (0, 1))
        self.assertEqual(result.provider_version, "9.10.0")
        self.assertIn("HeiGIT", result.attribution)
        self.assertEqual(result.geometry, response()["features"][0]["geometry"])
        request, deadline = self.transport.call_args.args
        self.assertFalse(request["body"]["geometry_simplify"])
        self.assertTrue(request["body"]["instructions"])
        self.assertEqual(request["body"]["options"], {})
        self.assertEqual(request["body"]["coordinates"][0], [-73.9857, 40.7484])
        self.assertEqual(request["connect_timeout"], 3)
        self.assertEqual(request["read_timeout"], 15)
        self.assertLessEqual(deadline, 30)
        result.geometry["coordinates"].clear()
        other = self.provider_with()
        self.assertEqual(other.route((START, FINISH)).distance_miles, 1)
        self.assertTrue(other.route((START, FINISH)).geometry["coordinates"])
        self.assertEqual(other.budget.attempts, 0)
        self.assertEqual(self.transport.call_count, 1)

    def test_cache_separates_all_material_inputs(self):
        self.provider.route((START, FINISH))
        self.provider.route((FINISH, START))
        self.provider_with(profile="driving-hgv").route((START, FINISH))
        self.provider_with(options={"avoid_borders": "all"}).route((START, FINISH))
        self.provider_with(version="v2-adapter-2").route((START, FINISH))
        self.assertEqual(self.transport.call_count, 5)

    def test_two_normal_calls_and_one_repair(self):
        self.transport.side_effect = [http_response(), http_response(3), http_response(3)]
        session = RoutingSession(self.provider, START, FINISH)
        session.initial()
        session.initial()
        selected = session.through_stops((STOP,))
        self.assertEqual(selected.leg_distances_miles, (1, 1))
        session.through_stops((Coordinate(40.735, -73.995),), repair=True)
        with self.assertRaisesRegex(RoutingError, "repair_not_available"):
            session.through_stops((STOP,), repair=True)
        self.assertEqual(self.transport.call_count, 3)
        self.assertEqual(self.provider.budget.attempts, 3)

    def test_empty_stops_reuses_initial_route(self):
        session = RoutingSession(self.provider, START, FINISH)
        session.initial()
        session.through_stops(())
        self.assertEqual(self.transport.call_count, 1)

    def test_failure_paths_never_exceed_three_attempts(self):
        cases = [
            ({"status": 429, "body": ""}, "rate_limited"),
            ({"status": 503, "body": ""}, "provider_unavailable"),
            ({"status": 404, "body": '{"error":{"code":2009}}'}, "no_route"),
            ({"status": 404, "body": '{"error":{"code":2010}}'}, "no_route"),
            ({"status": 400, "body": '{"error":{"code":2004}}'}, "route_length_limit"),
            ({"status": 401, "body": ""}, "provider_authentication"),
            ({"status": 302, "body": "{}"}, "provider_rejected_request"),
            ({"status": 200, "body": "bad json"}, "invalid_response"),
            (RoutingError("timeout"), "timeout"),
        ]
        for result, code in cases:
            with self.subTest(code=code):
                transport = Mock()
                if isinstance(result, Exception):
                    transport.side_effect = result
                else:
                    transport.return_value = result
                provider = self.provider_with()
                provider.transport = transport
                for _ in range(3):
                    with self.assertRaisesRegex(RoutingError, code):
                        provider.route((START, FINISH))
                with self.assertRaisesRegex(RoutingError, "routing_budget_exhausted"):
                    provider.route((START, FINISH))
                self.assertEqual(transport.call_count, 3)
                self.assertEqual(provider.budget.attempts, 3)

    def test_malformed_routes_are_not_cached(self):
        variants = []
        for field, value in (("type", "Polygon"), ("coordinates", []),
                             ("coordinates", [[0, 91], [0, 0]]),
                             ("coordinates", [[0, float("nan")], [0, 0]])):
            payload = response()
            payload["features"][0]["geometry"][field] = value
            variants.append(payload)
        for field, value in (("segments", []), ("way_points", [1, 0]),
                             ("summary", {"distance": -1, "duration": 1}),
                             ("summary", {"distance": 999, "duration": 1}),
                             ("summary", {"distance": 6_000_001, "duration": 1})):
            payload = response()
            payload["features"][0]["properties"][field] = value
            variants.append(payload)
        variants.extend([{}, {"type": "FeatureCollection", "features": []}, None])
        for payload in variants:
            with self.subTest(payload=payload):
                provider = self.provider_with()
                self.transport.return_value = {"status": 200, "body": json.dumps(payload)}
                with self.assertRaisesRegex(RoutingError, "invalid_response"):
                    provider.route((START, FINISH))
                self.assertEqual(provider.budget.attempts, 1)

    def test_preflight_limits_make_no_requests(self):
        for points, error in (
            ((START,), "invalid_waypoints"), ((START,) * 51, "invalid_waypoints"),
            ((START, Coordinate(float("nan"), 0)), "invalid_waypoints"),
            ((Coordinate(0, 0), Coordinate(0, 100)), "route_length_limit"),
            ((START, Coordinate(True, 0)), "invalid_waypoints"),
        ):
            with self.assertRaisesRegex(RoutingError, error):
                self.provider.route(points)
        self.assertEqual(self.transport.call_count, 0)
        self.assertEqual(self.provider.budget.attempts, 0)
        with self.assertRaises(ValueError):
            self.provider_with(options={"avoid_polygons": {}})

    def test_deadline_shared_across_stages_and_cache(self):
        clock = Mock(return_value=0)
        budget = RoutingBudget(10, clock)
        provider = self.provider_with(budget=budget)
        session = RoutingSession(provider, START, FINISH)
        session.initial()
        clock.return_value = 11
        for operation in (session.initial, lambda: session.through_stops((STOP,)),
                          lambda: provider.route((START, FINISH))):
            with self.assertRaisesRegex(RoutingError, "planning_deadline_exceeded"):
                operation()
        self.assertEqual(self.transport.call_count, 1)

    def test_late_response_not_cached(self):
        clock = Mock(return_value=0)
        provider = self.provider_with(budget=RoutingBudget(10, clock))
        def slow_response(*args):
            clock.return_value = 11
            return http_response()
        self.transport.side_effect = slow_response
        with self.assertRaisesRegex(RoutingError, "planning_deadline_exceeded"):
            provider.route((START, FINISH))
        self.transport.side_effect = None
        self.provider_with().route((START, FINISH))
        self.assertEqual(self.transport.call_count, 2)

    def test_failed_selected_route_leaves_only_third_attempt(self):
        self.transport.side_effect = [http_response(), RoutingError("timeout"), http_response(3)]
        session = RoutingSession(self.provider, START, FINISH)
        session.initial()
        with self.assertRaisesRegex(RoutingError, "timeout"):
            session.through_stops((STOP,))
        session.through_stops((STOP,), repair=True)
        self.assertEqual(self.transport.call_count, 3)

    def test_initial_retry_reduces_remaining_stop_budget(self):
        self.transport.side_effect = [RoutingError("timeout"), http_response(), http_response(3)]
        session = RoutingSession(self.provider, START, FINISH)
        with self.assertRaisesRegex(RoutingError, "timeout"):
            session.initial()
        session.initial()
        session.through_stops((STOP,))
        with self.assertRaisesRegex(RoutingError, "routing_budget_exhausted"):
            session.through_stops((Coordinate(40.73, -73.99),), repair=True)
        self.assertEqual(self.transport.call_count, 3)

    def test_fifty_waypoints_accepted(self):
        self.transport.return_value = http_response(50)
        result = self.provider.route((START,) * 50)
        self.assertEqual(len(result.leg_distances_miles), 49)
        self.assertEqual(self.transport.call_count, 1)

    def test_cache_expiry_causes_one_new_attempt(self):
        with patch("django.core.cache.backends.base.time.time", return_value=100):
            self.provider.route((START, FINISH))
        with patch("django.core.cache.backends.base.time.time", return_value=3701):
            self.provider.route((START, FINISH))
        self.assertEqual(self.transport.call_count, 2)


class HTTPWorkerTests(TestCase):
    def test_real_worker_is_terminated_at_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = Path(directory) / "slow_worker.py"
            worker.write_text("import time\ntime.sleep(10)\n", encoding="utf-8")
            with patch("routes.services.routing.Path") as path:
                path.return_value.with_name.return_value = worker
                started = time.monotonic()
                with self.assertRaisesRegex(RoutingError, "planning_deadline_exceeded"):
                    post_once({}, 0.15)
                self.assertLess(time.monotonic() - started, 3)

    def test_actual_http_request_count_no_redirect_or_retry(self):
        for status in (200, 302, 429, 500):
            with self.subTest(status=status):
                connection = Mock()
                connection.getresponse.return_value.status = status
                connection.getresponse.return_value.read.return_value = b'{}'
                output = io.StringIO()
                with patch("routes.services.routing_http.http.client.HTTPSConnection", return_value=connection), patch(
                    "sys.stdin", io.StringIO(json.dumps({
                        "connect_timeout": 3, "read_timeout": 5,
                        "path": "/test", "key": "test-key", "body": {},
                    }))
                ), redirect_stdout(output):
                    http_main()
                self.assertEqual(connection.request.call_count, 1)
                self.assertEqual(json.loads(output.getvalue())["status"], status)
                connection.sock.settimeout.assert_called_once_with(5)
                connection.close.assert_called_once()

    def test_worker_timeout_has_no_retry(self):
        connection = Mock()
        connection.getresponse.side_effect = TimeoutError
        with patch("routes.services.routing_http.http.client.HTTPSConnection", return_value=connection), patch(
            "sys.stdin", io.StringIO(json.dumps({
                "connect_timeout": 3, "read_timeout": 5,
                "path": "/test", "key": "test-key", "body": {},
            }))
        ), redirect_stdout(io.StringIO()) as output:
            http_main()
        self.assertEqual(connection.request.call_count, 1)
        self.assertEqual(json.loads(output.getvalue()), {"error": "timeout"})

    def test_parent_uses_hard_timeout_and_no_secret_in_arguments(self):
        with patch("routes.services.routing.subprocess.run", side_effect=subprocess.TimeoutExpired("worker", 1)) as run:
            with self.assertRaisesRegex(RoutingError, "planning_deadline_exceeded"):
                post_once({"key": "private-test-key"}, 1)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.kwargs["timeout"], 1)
        self.assertNotIn("private-test-key", str(run.call_args.args))


@skipUnless(os.getenv("SPOTTER_LIVE_ROUTING") == "1", "Opt-in live routing disabled")
class LiveRoutingTests(TestCase):
    def test_short_route_and_warm_cache(self):
        from dotenv import dotenv_values
        key = os.getenv("ROUTING_PROVIDER_API_KEY") or dotenv_values(".env").get("ROUTING_PROVIDER_API_KEY")
        self.assertTrue(key, "Set ROUTING_PROVIDER_API_KEY before opting in")
        cache = LocMemCache("live-routing-check", {})
        cache.clear()
        provider = OpenRouteService(api_key=key, cache=cache, budget=RoutingBudget(30))
        result = provider.route((START, FINISH))
        self.assertGreater(result.distance_miles, 0)
        self.assertGreater(result.duration_seconds, 0)
        self.assertEqual(len(result.leg_distances_miles), 1)
        self.assertEqual(provider.route((START, FINISH)), result)
        self.assertEqual(provider.budget.attempts, 1)

    def test_initial_and_selected_stop_share_budget(self):
        from dotenv import dotenv_values

        key = os.getenv("ROUTING_PROVIDER_API_KEY") or dotenv_values(".env").get("ROUTING_PROVIDER_API_KEY")
        self.assertTrue(key, "Set ROUTING_PROVIDER_API_KEY before opting in")
        cache = LocMemCache("live-routing-stop-check", {})
        cache.clear()
        provider = OpenRouteService(api_key=key, cache=cache, budget=RoutingBudget(30))
        session = RoutingSession(provider, START, FINISH)
        initial = session.initial()
        selected = session.through_stops((STOP,))
        self.assertEqual(len(initial.leg_distances_miles), 1)
        self.assertEqual(len(selected.leg_distances_miles), 2)
        self.assertAlmostEqual(sum(selected.leg_distances_miles), selected.distance_miles, places=3)
        self.assertEqual(selected.waypoint_indices[0], 0)
        self.assertEqual(selected.waypoint_indices[-1], len(selected.geometry["coordinates"]) - 1)
        self.assertEqual(provider.budget.attempts, 2)
        self.assertEqual(provider.route((START, STOP, FINISH)), selected)
        self.assertEqual(provider.budget.attempts, 2)
