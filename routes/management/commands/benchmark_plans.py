from __future__ import annotations

import json
import statistics
import time
from decimal import Decimal
from typing import Callable

from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.utils import timezone

from routes.models import FuelPriceDataset, FuelStation
from routes.services.plan_api import plan_route_payload
from routes.services.planner import RoutePlanner
from routes.services.routing import DrivingRoute, OpenRouteService, RoutingBudget, RoutingSession
from routes.services.station_search import CandidateStation
from routes.services.usa import load_contiguous_boundaries
from routes.validation import Coordinate


SCENARIOS = {
    "short": {
        "start": {"latitude": 40.7128, "longitude": -74.0060},
        "finish": {"latitude": 39.9526, "longitude": -75.1652},
        "miles": 90.0,
        "stops": (),
    },
    "regional": {
        "start": {"latitude": 40.7128, "longitude": -74.0060},
        "finish": {"latitude": 38.9072, "longitude": -77.0369},
        "miles": 230.0,
        "stops": (
            ("R1", 40.2, -75.5, "3.40", 100.0),
            ("R2", 39.5, -76.5, "3.20", 180.0),
        ),
    },
    "cross_country": {
        "start": {"latitude": 40.7128, "longitude": -74.0060},
        "finish": {"latitude": 34.0522, "longitude": -118.2437},
        "miles": 2400.0,
        "stops": (
            ("C1", 40.0, -80.0, "3.50", 400.0),
            ("C2", 39.0, -90.0, "3.30", 800.0),
            ("C3", 38.0, -100.0, "3.10", 1200.0),
            ("C4", 36.0, -110.0, "3.40", 1600.0),
            ("C5", 35.0, -115.0, "3.20", 2000.0),
        ),
    },
}


def _route(waypoints: tuple[Coordinate, ...], leg_miles: tuple[float, ...]) -> DrivingRoute:
    return DrivingRoute(
        geometry={
            "type": "LineString",
            "coordinates": [[point.longitude, point.latitude] for point in waypoints],
        },
        distance_miles=sum(leg_miles),
        duration_seconds=100.0 * len(leg_miles),
        leg_distances_miles=leg_miles,
        waypoint_indices=tuple(range(len(waypoints))),
        provider_version="benchmark",
    )


def _candidate(source_id: str, lat: float, lon: float, price: str, route_miles: float) -> CandidateStation:
    return CandidateStation(
        source_id=source_id,
        latitude=lat,
        longitude=lon,
        price_usd_per_gallon=Decimal(price),
        route_distance_miles=route_miles,
        offset_miles=0.1,
        estimated_detour_miles=0.2,
        geocoding_confidence=Decimal("1.0"),
        geocoding_source="benchmark",
        location_quality="resolved_verified",
    )


class Command(BaseCommand):
    help = "Benchmark local planning latency with mocked routing (cold and warm caches)."

    def add_arguments(self, parser):
        parser.add_argument("--samples", type=int, default=21)
        parser.add_argument("--output", type=str, default="artifacts/PERFORMANCE.md")

    def handle(self, *args, **options):
        samples = max(3, int(options["samples"]))
        load_contiguous_boundaries.cache_clear()
        cache.clear()
        dataset, _ = FuelPriceDataset.objects.get_or_create(
            sha256="b" * 64,
            defaults={"source_filename": "benchmark.csv", "source_row_count": 1},
        )
        FuelStation.objects.get_or_create(
            dataset=dataset,
            source_station_id="BENCH",
            defaults={
                "name": "Benchmark Station",
                "address": "1 Bench",
                "city": "Town",
                "state": "NY",
                "rack_id": "1",
                "price_usd_per_gallon": Decimal("3.00"),
                "price_source_text": "3.00",
                "source_row_numbers": [1],
                "source_names": ["Benchmark Station"],
                "latitude": Decimal("40.5"),
                "longitude": Decimal("-74.5"),
                "geocoding_status": "resolved",
                "geocoding_source": "benchmark",
                "geocoding_key": "bench",
                "location_verified_at": timezone.now(),
            },
        )

        report = {
            "environment": "development machine, mocked routing transport, LocMem plan cache",
            "samples_per_cell": samples,
            "goals": {
                "local_planning_ms": 1000,
                "cached_response_ms": 300,
            },
            "scenarios": {},
        }

        for name, scenario in SCENARIOS.items():
            start = Coordinate(scenario["start"]["latitude"], scenario["start"]["longitude"])
            finish = Coordinate(scenario["finish"]["latitude"], scenario["finish"]["longitude"])
            stops = tuple(_candidate(*item) for item in scenario["stops"])
            if stops:
                waypoints = (start, *(Coordinate(s.latitude, s.longitude) for s in stops), finish)
                spacing = scenario["miles"] / (len(stops) + 1)
                legs = tuple(spacing for _ in range(len(stops) + 1))
                initial = _route((start, finish), (scenario["miles"],))
                verified = _route(waypoints, legs)
            else:
                initial = verified = _route((start, finish), (scenario["miles"],))

            routes = {
                ((start.latitude, start.longitude), (finish.latitude, finish.longitude)): initial,
            }
            if stops:
                key = (
                    (start.latitude, start.longitude),
                    *((s.latitude, s.longitude) for s in stops),
                    (finish.latitude, finish.longitude),
                )
                routes[key] = verified

            transport_calls = {"count": 0}

            def transport(request, deadline, _routes=routes, _calls=transport_calls):
                _calls["count"] += 1
                coords = tuple((latlon[1], latlon[0]) for latlon in request["body"]["coordinates"])
                route = _routes[coords]
                body = {
                    "type": "FeatureCollection",
                    "features": [{
                        "geometry": route.geometry,
                        "properties": {
                            "summary": {
                                "distance": route.distance_miles * 1609.344,
                                "duration": route.duration_seconds,
                            },
                            "segments": [
                                {"distance": leg * 1609.344} for leg in route.leg_distances_miles
                            ],
                            "way_points": list(route.waypoint_indices),
                        },
                    }],
                    "metadata": {"engine": {"version": route.provider_version}},
                }
                return {"status": 200, "body": json.dumps(body)}

            class Lookup:
                def candidates(self, route, corridor_miles):
                    return stops

            def session_factory(a: Coordinate, b: Coordinate) -> RoutingSession:
                provider = OpenRouteService(
                    api_key="benchmark-key",
                    cache=cache,
                    budget=RoutingBudget(30),
                    transport=transport,
                )
                return RoutingSession(provider, a, b)

            def planner_factory() -> RoutePlanner:
                return RoutePlanner(session_factory, Lookup(), corridor_miles=5, snap_tolerance_miles=0.5)

            body = {
                "start": scenario["start"],
                "finish": scenario["finish"],
                "initial_fuel_gallons": 50,
            }
            encoded = json.dumps(body).encode("utf-8")

            cold = self._measure(
                samples,
                lambda: (
                    cache.clear(),
                    plan_route_payload(encoded, planner_factory=planner_factory),
                )[1],
            )
            plan_route_payload(encoded, planner_factory=planner_factory)
            warm = self._measure(
                samples,
                lambda: plan_route_payload(encoded, planner_factory=planner_factory),
            )
            report["scenarios"][name] = {
                "distance_miles": scenario["miles"],
                "stop_count": len(stops),
                "cold_local_ms": cold,
                "warm_cache_ms": warm,
                "notes": "Local processing only; provider latency excluded by mocked transport.",
            }
            self.stdout.write(
                f"{name}: cold median={cold['median_ms']} ms p95={cold['p95_ms']} ms; "
                f"warm median={warm['median_ms']} ms p95={warm['p95_ms']} ms"
            )

        self._write_markdown(options["output"], report)

    def _measure(self, samples: int, fn: Callable):
        timings = []
        for _ in range(samples):
            started = time.perf_counter()
            fn()
            timings.append((time.perf_counter() - started) * 1000)
        timings.sort()
        p95_index = min(len(timings) - 1, max(0, int(round(0.95 * (len(timings) - 1)))))
        return {
            "sample_size": samples,
            "median_ms": round(statistics.median(timings), 3),
            "p95_ms": round(timings[p95_index], 3),
            "min_ms": round(timings[0], 3),
            "max_ms": round(timings[-1], 3),
        }

    def _write_markdown(self, path: str, report: dict) -> None:
        lines = [
            "# Performance measurements (Section 10)",
            "",
            f"Environment: {report['environment']}",
            f"Samples per cell: {report['samples_per_cell']}",
            "",
            "Provisional goals (development machine, local processing): under 1000 ms cold local planning; "
            "under 300 ms warm cached responses. External provider latency is excluded from these mocked runs "
            "and is not guaranteed.",
            "",
            "| Scenario | Miles | Stops | Cold median (ms) | Cold p95 (ms) | Warm median (ms) | Warm p95 (ms) |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for name, row in report["scenarios"].items():
            cold = row["cold_local_ms"]
            warm = row["warm_cache_ms"]
            lines.append(
                f"| {name} | {row['distance_miles']} | {row['stop_count']} | "
                f"{cold['median_ms']} | {cold['p95_ms']} | {warm['median_ms']} | {warm['p95_ms']} |"
            )
        lines.extend([
            "",
            "Cache keys include endpoints, initial fuel, vehicle assumptions, corridor and snap settings, "
            "dataset SHA-256, boundary hash, and routing profile/options/adapter version.",
            "",
            "Raw JSON:",
            "",
            "```json",
            json.dumps(report, indent=2),
            "```",
            "",
        ])
        from pathlib import Path

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines), encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(f"Wrote {target}"))
