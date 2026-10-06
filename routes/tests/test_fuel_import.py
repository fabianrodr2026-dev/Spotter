import csv
import io
import json
import tempfile
from decimal import Decimal
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase

from routes.models import FuelPriceDataset, FuelPriceSourceRow, FuelStation
from routes.services.fuel_import import FIELDS, audit_fuel_price_file


def write_fixture(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def row(station_id="1", name="One", address="Main St", city="Town", state="ok", rack="9", price="3.00733333"):
    return dict(zip(FIELDS, (station_id, name, address, city, state, rack, price)))


class FuelImportTests(TestCase):
    def test_audit_reconciles_duplicate_conflict_and_rejected_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fixture.csv"
            rows = [
                row(),
                row(name="Another Name", address="  Main   St  ", state="OK", price="3.007333330"),
                row(station_id="2", price="4.00"),
                row(station_id="2", price="3.99"),
                row(station_id="3", address="A"),
                row(station_id="3", address="B"),
                row(station_id="4", price=""),
                row(station_id="5", price="not a price"),
            ]
            write_fixture(path, rows)
            audit = audit_fuel_price_file(path)

        report = audit.report
        self.assertEqual(report["source_rows"], 8)
        self.assertEqual(report["imported_stations"], 1)
        self.assertEqual(report["collapsed_rows"], 1)
        self.assertEqual(report["conflicting_station_ids"], 2)
        self.assertEqual(report["conflict_rows"], 4)
        self.assertEqual(report["rejected_rows"], 2)
        self.assertEqual(report["missing_fields"]["Retail Price"], 1)
        self.assertEqual(report["invalid_prices"], 2)
        self.assertTrue(report["reconciled"])
        self.assertEqual({item["station_id"] for item in report["conflicts"]}, {"2", "3"})
        self.assertEqual(next(item for item in report["conflicts"] if item["station_id"] == "2")["fields"], ["Retail Price"])
        self.assertEqual(audit.rows[0].normalized["State"], "OK")
        self.assertEqual(audit.rows[1].normalized["Address"], "Main St")
        self.assertEqual(audit.rows[1].raw["Address"], "  Main   St  ")

    def test_import_twice_preserves_decimal_and_provenance(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fixture.csv"
            write_fixture(path, [row(), row(name="Alias"), row(station_id="2", price="4"), row(station_id="2", price="5")])
            first_output, second_output = io.StringIO(), io.StringIO()
            call_command("import_fuel_prices", path, stdout=first_output)
            call_command("import_fuel_prices", path, stdout=second_output)

        self.assertEqual(json.loads(first_output.getvalue())["database_import"], "created")
        self.assertEqual(json.loads(second_output.getvalue())["database_import"], "already_present")
        self.assertEqual(FuelPriceDataset.objects.count(), 1)
        self.assertEqual(FuelStation.objects.count(), 1)
        self.assertEqual(FuelPriceSourceRow.objects.count(), 4)
        station = FuelStation.objects.get()
        self.assertEqual(station.price_usd_per_gallon, Decimal("3.00733333"))
        self.assertEqual(station.price_source_text, "3.00733333")
        self.assertEqual(station.source_row_numbers, [2, 3])
        self.assertEqual(station.source_names, ["One", "Alias"])
        self.assertIsNone(station.latitude)
        self.assertEqual(station.geocoding_status, "pending")
        self.assertEqual(list(FuelPriceSourceRow.objects.order_by("row_number").values_list("status", flat=True)),
                         ["imported", "collapsed", "conflict", "conflict"])

    def test_malformed_extra_column_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "malformed.csv"
            write_fixture(path, [row()])
            with path.open("a", encoding="utf-8") as output:
                output.write("6,Name,Street,Town,OK,9,3.50,EXTRA\n")
            audit = audit_fuel_price_file(path)
        self.assertEqual(audit.report["rejected_rows"], 1)
        self.assertEqual(audit.report["imported_stations"], 1)
        self.assertEqual(audit.rows[1].raw["__extra_columns__"], ["EXTRA"])

    def test_price_outside_decimal_field_range_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "price.csv"
            write_fixture(path, [row(price="1E100"), row(station_id="2", price="0")])
            audit = audit_fuel_price_file(path)
        self.assertEqual(audit.report["invalid_prices"], 2)
        self.assertEqual(audit.report["rejected_rows"], 2)

    def test_audit_only_does_not_write_database(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fixture.csv"
            write_fixture(path, [row()])
            output = io.StringIO()
            call_command("import_fuel_prices", path, "--audit-only", stdout=output)
        self.assertEqual(json.loads(output.getvalue())["database_import"], "skipped")
        self.assertFalse(FuelPriceDataset.objects.exists())

    def test_report_file_lists_conflicting_values(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fixture.csv"
            report_path = Path(folder) / "report.json"
            write_fixture(path, [row(price="3.00"), row(price="4.00")])
            call_command("import_fuel_prices", path, "--audit-only", "--report-file", report_path, stdout=io.StringIO())
            report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["conflicts"][0]["fields"], ["Retail Price"])
        self.assertEqual([variant["Retail Price"] for variant in report["conflicts"][0]["variants"]], ["3.00", "4.00"])
