import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from routes.models import FuelPriceDataset, FuelStation
from routes.services.geocoding import (
    ATTRIBUTION, OsmStationResolver, contested_brand_cities, coverage_report, enrich_stations,
)
from routes.services.location_sources import download_osm_snapshot, download_source, read_json, validate_source, write_json
from routes.services.state_boundaries import CONTIGUOUS_STATES, StateBoundaries


class Command(BaseCommand):
    help = "Resolve station coordinates offline from cached OSM fuel POIs and Census state polygons."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--dataset", help="Imported CSV SHA-256 (required when multiple datasets exist).")
        parser.add_argument("--download", action="store_true", help="Fetch missing setup sources once; existing files are reused.")
        parser.add_argument("--cache-dir", type=Path, default=settings.BASE_DIR / "data" / "geocoding")
        parser.add_argument("--osm-file", type=Path)
        parser.add_argument("--boundaries-file", type=Path)
        parser.add_argument("--limit", type=int, help="Maximum new or changed stations processed; completed stations are skipped.")
        parser.add_argument("--report-file", type=Path)
        parser.add_argument("--review-report", type=Path, help="Write station decisions and candidate evidence for review.")
        parser.add_argument("--export-osm", type=Path, help="Export relevant OSM objects with ODbL attribution, without fuel prices.")
        parser.add_argument("--coverage-only", action="store_true")

    def handle(self, *args, **options):
        datasets = FuelPriceDataset.objects.all()
        if options["dataset"]:
            datasets = datasets.filter(pk=options["dataset"])
        if datasets.count() != 1:
            raise CommandError("Import one dataset first, or select an existing dataset with --dataset SHA256.")
        dataset = datasets.get()
        stations = FuelStation.objects.filter(dataset=dataset)
        if options["limit"] is not None and options["limit"] <= 0:
            raise CommandError("--limit must be positive")
        report = {"dataset_sha256": dataset.pk, "attribution": ATTRIBUTION}
        try:
            if not options["coverage_only"]:
                directory = options["cache_dir"]
                paths = {}
                for kind, option, filename in (("osm", "osm_file", "osm-fuel.json"),
                                                ("boundaries", "boundaries_file", "us-states.geojson")):
                    paths[kind] = options[option] or directory / filename
                    if options["download"] and not options[option]:
                        self.stderr.write(f"Loading cached {kind} source (downloading only if absent)...")
                        if kind == "osm":
                            paths[kind] = download_osm_snapshot(directory, settings.GEOCODING_OVERPASS_URL)
                        else:
                            paths[kind] = download_source(directory, kind)
                osm, osm_hash = read_json(paths["osm"])
                boundaries_doc, boundary_hash = read_json(paths["boundaries"])
                validate_source(osm, "osm")
                validate_source(boundaries_doc, "boundaries")
                boundaries = StateBoundaries(boundaries_doc)
                required = set(stations.values_list("state", flat=True)) & CONTIGUOUS_STATES
                if missing := required - boundaries.states.keys():
                    raise ValueError(f"Missing Census state polygons: {', '.join(sorted(missing))}")
                resolver = OsmStationResolver(
                    osm, boundaries, osm_hash, boundary_hash,
                    contested_brands=contested_brand_cities(stations),
                )
                report["run"] = enrich_stations(stations, resolver, limit=options["limit"])
                report.update(source_sha256=osm_hash, boundary_sha256=boundary_hash,
                              source_objects=len(resolver.elements), ignored_source_objects=resolver.ignored_elements,
                              source_scope=osm.get("scope", {"type": "unspecified"}))
                if options["export_osm"]:
                    ids = {candidate["osm_id"] for details in stations.values_list("geocoding_details", flat=True)
                           for candidate in details.get("candidates", [])}
                    write_json(options["export_osm"], {
                        "version": 0.6, "osm3s": osm.get("osm3s", {}), "attribution": ATTRIBUTION,
                        "parent_source_sha256": osm_hash,
                        "elements": [resolver.elements[key] for key in sorted(ids) if key in resolver.elements],
                    })
            report["coverage"] = coverage_report(stations)
            if options["review_report"]:
                write_json(options["review_report"], {
                    "dataset_sha256": dataset.pk, "attribution": ATTRIBUTION,
                    "stations": [{"source_station_id": station.source_station_id,
                                  "name": station.name, "address": station.address,
                                  "city": station.city, "state": station.state,
                                  "status": station.geocoding_status,
                                  "details": station.geocoding_details}
                                 for station in stations.order_by("source_station_id")],
                })
            if options["report_file"]:
                write_json(options["report_file"], report)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise CommandError(f"Cannot enrich stations: {exc}") from exc
        self.stdout.write(json.dumps(report, indent=2))
