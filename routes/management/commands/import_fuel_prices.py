import csv
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from routes.services.fuel_import import audit_fuel_price_file, import_fuel_price_file


class Command(BaseCommand):
    help = "Audit and import a versioned fuel-price CSV without silently resolving conflicting stations."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("path", nargs="?", type=Path, default=settings.BASE_DIR / "fuel-prices-for-be-assessment.csv")
        parser.add_argument("--audit-only", action="store_true", help="Read and report the CSV without database writes.")
        parser.add_argument("--report-file", type=Path, help="Write the full JSON report, including every conflicting ID.")

    def handle(self, *args, **options):
        try:
            audit = audit_fuel_price_file(options["path"])
        except (OSError, UnicodeError, csv.Error, ValueError) as exc:
            raise CommandError(f"Cannot audit fuel-price CSV: {exc}") from exc

        report = dict(audit.report)
        if options["audit_only"]:
            report["database_import"] = "skipped"
        else:
            _, created = import_fuel_price_file(audit)
            report["database_import"] = "created" if created else "already_present"
        report["source_filename"] = audit.source_filename

        if options["report_file"]:
            options["report_file"].write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        summary = {key: value for key, value in report.items() if key != "conflicts"}
        summary["conflict_report"] = str(options["report_file"]) if options["report_file"] else "use --report-file for all conflicting IDs"
        self.stdout.write(json.dumps(summary, indent=2))
