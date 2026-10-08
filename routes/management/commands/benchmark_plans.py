from __future__ import annotations

import gzip
import json
import math
import platform
import statistics
import time
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from routes.models import FuelPriceDataset
from routes.services.plan_api import PlanRequestError, plan_route_payload
from routes.services.planner import RoutePlanner
from routes.services.routing import OpenRouteService, RoutingBudget, RoutingSession, post_once
from routes.services.station_search import CorridorStationLookup, usable_stations


SCENARIOS = {
    "short": (40.7128, -74.0060, 39.9526, -75.1652),
    "regional": (40.7128, -74.0060, 40.4406, -79.9959),
    "long_multi_stop": (40.7128, -74.0060, 25.7617, -80.1918),
    "cross_country": (40.7128, -74.0060, 34.0522, -118.2437),
}


def request_for(points):
    lat1, lon1, lat2, lon2 = points
    return {"start": {"latitude": lat1, "longitude": lon1},
            "finish": {"latitude": lat2, "longitude": lon2}, "initial_fuel_gallons": 50}


def timing_summary(values):
    ordered = sorted(values)
    return {"samples": len(values), "median_ms": round(statistics.median(values), 3),
            "p95_ms": round(ordered[math.ceil(.95 * len(ordered)) - 1], 3)}


class Command(BaseCommand):
    help = "Benchmark real database lookup and planning using recorded provider responses; optionally capture live responses."

    def add_arguments(self, parser):
        parser.add_argument("--samples", type=int, default=11)
        parser.add_argument("--capture-live", action="store_true")
        parser.add_argument("--replay-file", type=Path, default=Path("artifacts/routing-benchmark.json.gz"))
        parser.add_argument("--output", type=Path, default=Path("artifacts/PERFORMANCE.md"))

    def handle(self, *args, **options):
        if options["samples"] < 3:
            raise CommandError("Use at least three samples")
        dataset = FuelPriceDataset.objects.order_by("-imported_at").first()
        if dataset is None:
            raise CommandError("Import and enrich the supplied dataset first")
        lookup = CorridorStationLookup(dataset_sha=dataset.pk)
        recording = {"dataset_sha256": dataset.pk,
                     "attribution": "© openrouteservice by HeiGIT | Data from OpenStreetMap; CC-BY-SA 4.0",
                     "scenarios": {}}

        def run(request, transport):
            def factory():
                def session(a, b):
                    return RoutingSession(OpenRouteService(
                        api_key=settings.ROUTING_PROVIDER_API_KEY, cache=cache,
                        budget=RoutingBudget(settings.ROUTING_TOTAL_DEADLINE_SECONDS),
                        transport=transport,
                    ), a, b)
                return RoutePlanner(session, lookup)
            try:
                payload, _ = plan_route_payload(json.dumps(request).encode(), planner_factory=factory)
                return {"outcome": "success", "payload": payload}
            except PlanRequestError as exc:
                return {"outcome": exc.code}

        if options["capture_live"]:
            for name, coordinates in SCENARIOS.items():
                cache.clear()
                request = request_for(coordinates)
                records = []
                def transport(provider_request, deadline):
                    started = time.perf_counter()
                    response = post_once(provider_request, deadline)
                    records.append({"coordinates": provider_request["body"]["coordinates"],
                                    "response": response,
                                    "provider_ms": (time.perf_counter() - started) * 1000})
                    return response
                started = time.perf_counter()
                result = run(request, transport)
                recording["scenarios"][name] = {
                    "request": request, "records": records, "outcome": result["outcome"],
                    "live_total_ms": (time.perf_counter() - started) * 1000,
                }
                self.stdout.write(f"{name}: live {result['outcome']}, {len(records)} routing attempts")
                if name == "long_multi_stop" and result["outcome"] == "success":
                    Path("artifacts/demo-plan.json").write_text(json.dumps(result["payload"], indent=2), encoding="utf-8")
                    Path("artifacts/demo-request.json").write_text(json.dumps(request, indent=2), encoding="utf-8")
            options["replay_file"].parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(options["replay_file"], "wt", encoding="utf-8") as stream:
                json.dump(recording, stream)
        else:
            try:
                with gzip.open(options["replay_file"], "rt", encoding="utf-8") as stream:
                    recording = json.load(stream)
            except OSError as exc:
                raise CommandError("Capture responses with --capture-live first") from exc
        if recording["dataset_sha256"] != dataset.pk:
            raise CommandError("Replay dataset does not match the imported dataset")

        report = {"environment": {"python": platform.python_version(), "platform": platform.platform(),
                                  "database": connection.vendor, "cache": settings.CACHES["default"]["BACKEND"]},
                  "dataset_sha256": dataset.pk, "stations": dataset.stations.count(),
                  "usable_stations": usable_stations(dataset.stations.all()).count(), "scenarios": {}}
        for name, scenario in recording["scenarios"].items():
            def replay(provider_request, deadline):
                for record in scenario["records"]:
                    if record["coordinates"] == provider_request["body"]["coordinates"]:
                        return record["response"]
                raise CommandError(f"{name}: missing response for selected waypoints; recapture after station changes")
            cold, warm = [], []
            result = None
            for _ in range(options["samples"]):
                cache.clear()
                started = time.perf_counter()
                result = run(scenario["request"], replay)
                cold.append((time.perf_counter() - started) * 1000)
                if result["outcome"] != scenario["outcome"]:
                    raise CommandError(f"{name}: recorded outcome changed")
                if result["outcome"] == "success":
                    started = time.perf_counter()
                    cached = run(scenario["request"], replay)
                    warm.append((time.perf_counter() - started) * 1000)
                    if cached["payload"]["routing_attempts"] != 0:
                        raise CommandError("Warm plan unexpectedly performed routing")
            payload = result.get("payload", {})
            row = {"outcome": result["outcome"], "cold_local": timing_summary(cold),
                   "warm_cached": timing_summary(warm) if warm else None,
                   "distance_miles": payload.get("route", {}).get("distance_miles"),
                   "stop_count": len(payload.get("stops", [])),
                   "candidate_count": payload.get("diagnostics", {}).get("candidate_count"),
                   "live_total_ms": round(scenario["live_total_ms"], 3),
                   "live_provider_ms": round(sum(r["provider_ms"] for r in scenario["records"]), 3)}
            report["scenarios"][name] = row
            self.stdout.write(f"{name}: {row['outcome']}, cold {row['cold_local']}, warm {row['warm_cached']}")
        lines = ["# Performance measurements", "",
                 "Real database corridor queries, full recorded road geometry, projection, optimization, serialization and caching are measured. Only provider HTTP is replayed for repeated local samples. Live timings are one observation per scenario, not latency guarantees.", "",
                 "Cold clears route and plan caches; boundary loading occurs once per process. Failed plans are not cached, so warm success latency is unavailable for failures. Targets: local planning below 1,000 ms; cached responses below 300 ms.", "",
                 "The cross-country outcome is reported honestly; incomplete station coverage must never be replaced by invented stops.", "", "```json", json.dumps(report, indent=2), "```", ""]
        options["output"].write_text("\n".join(lines), encoding="utf-8")
