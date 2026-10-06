import csv
import hashlib
import io
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.db import transaction

from routes.models import FuelPriceDataset, FuelPriceSourceRow, FuelStation


FIELDS = (
    "OPIS Truckstop ID",
    "Truckstop Name",
    "Address",
    "City",
    "State",
    "Rack ID",
    "Retail Price",
)


@dataclass(frozen=True)
class AuditedRow:
    row_number: int
    physical_line_number: int
    raw: dict[str, str | list[str] | None]
    normalized: dict[str, str]
    price: Decimal | None
    issue: str = ""


@dataclass(frozen=True)
class FuelPriceAudit:
    sha256: str
    source_filename: str
    rows: tuple[AuditedRow, ...]
    report: dict


def _normalized_text(value: str | None) -> str:
    return " ".join((value or "").split())


def _row_identity(row: AuditedRow) -> tuple:
    values = row.normalized
    return (
        values["Address"].casefold(),
        values["City"].casefold(),
        values["State"],
        values["Rack ID"].casefold(),
        row.price,
    )


def audit_fuel_price_file(path: Path) -> FuelPriceAudit:
    raw_bytes = path.read_bytes()
    sha256 = hashlib.sha256(raw_bytes).hexdigest()
    reader = csv.DictReader(io.StringIO(raw_bytes.decode("utf-8-sig"), newline=""), strict=True)
    if tuple(reader.fieldnames or ()) != FIELDS:
        raise ValueError(f"Expected CSV columns: {', '.join(FIELDS)}")

    rows = []
    missing_counts = {field: 0 for field in FIELDS}
    invalid_price_count = 0
    groups = defaultdict(list)
    for row_number, source in enumerate(reader, start=2):
        raw = {field: source.get(field) for field in FIELDS}
        if None in source:
            raw["__extra_columns__"] = source[None]
        normalized = {field: _normalized_text(source.get(field)) for field in FIELDS}
        normalized["State"] = normalized["State"].upper()
        missing = [field for field in FIELDS if not normalized[field]]
        for field in missing:
            missing_counts[field] += 1
        issue = ""
        if None in source or missing:
            issue = "malformed_or_missing_field"
        if normalized["State"] and (len(normalized["State"]) != 2 or not normalized["State"].isalpha()):
            issue = "invalid_state_code"
        try:
            price = Decimal(normalized["Retail Price"])
            if (
                not price.is_finite()
                or price <= 0
                or max(price.adjusted() + 1, 0) > 8
                or -price.as_tuple().exponent > 16
            ):
                raise InvalidOperation
        except InvalidOperation:
            price = None
            invalid_price_count += 1
            issue = "invalid_price"
        audited = AuditedRow(row_number, reader.line_num, raw, normalized, price, issue)
        rows.append(audited)
        if not issue:
            groups[normalized["OPIS Truckstop ID"]].append(audited)

    conflicting_ids = set()
    conflicts = []
    for station_id, group in groups.items():
        if len({_row_identity(row) for row in group}) > 1:
            conflicting_ids.add(station_id)
            fields = [
                field
                for field in ("Address", "City", "State", "Rack ID", "Retail Price")
                if len({row.price if field == "Retail Price" else row.normalized[field].casefold() for row in group}) > 1
            ]
            conflicts.append({
                "station_id": station_id,
                "row_numbers": [row.row_number for row in group],
                "fields": fields,
                "variants": [
                    {"row_number": row.row_number, **{field: row.normalized[field] for field in FIELDS}}
                    for row in group
                ],
            })

    accepted_groups = {station_id: group for station_id, group in groups.items() if station_id not in conflicting_ids}
    accepted_rows = len(accepted_groups)
    collapsed_rows = sum(len(group) - 1 for group in accepted_groups.values())
    conflict_rows = sum(len(groups[station_id]) for station_id in conflicting_ids)
    rejected_rows = sum(bool(row.issue) for row in rows)
    report = {
        "source_sha256": sha256,
        "source_rows": len(rows),
        "unique_station_ids": len({row.normalized["OPIS Truckstop ID"] for row in rows if row.normalized["OPIS Truckstop ID"]}),
        "duplicate_station_id_groups": sum(len(group) > 1 for group in groups.values()),
        "missing_fields": missing_counts,
        "invalid_prices": invalid_price_count,
        "imported_stations": accepted_rows,
        "collapsed_rows": collapsed_rows,
        "conflicting_station_ids": len(conflicting_ids),
        "conflict_rows": conflict_rows,
        "rejected_rows": rejected_rows,
        "reconciled": accepted_rows + collapsed_rows + conflict_rows + rejected_rows == len(rows),
        "conflicts": sorted(conflicts, key=lambda item: item["station_id"]),
    }
    return FuelPriceAudit(sha256, path.name, tuple(rows), report)


@transaction.atomic
def import_fuel_price_file(audit: FuelPriceAudit) -> tuple[FuelPriceDataset, bool]:
    dataset, created = FuelPriceDataset.objects.get_or_create(
        sha256=audit.sha256,
        defaults={
            "source_filename": audit.source_filename,
            "source_row_count": len(audit.rows),
        },
    )
    if not created:
        return dataset, False
    groups = defaultdict(list)
    for row in audit.rows:
        if not row.issue:
            groups[row.normalized["OPIS Truckstop ID"]].append(row)
    conflicting_ids = {item["station_id"] for item in audit.report["conflicts"]}
    stations = []
    for station_id, group in groups.items():
        if station_id in conflicting_ids:
            continue
        first = group[0]
        values = first.normalized
        stations.append(FuelStation(
            dataset=dataset,
            source_station_id=station_id,
            name=values["Truckstop Name"],
            address=values["Address"],
            city=values["City"],
            state=values["State"],
            rack_id=values["Rack ID"],
            price_usd_per_gallon=first.price,
            price_source_text=values["Retail Price"],
            source_row_numbers=[row.row_number for row in group],
            source_names=list(dict.fromkeys(row.normalized["Truckstop Name"] for row in group)),
        ))
    FuelStation.objects.bulk_create(stations, batch_size=1000)
    station_by_id = {
        station.source_station_id: station
        for station in FuelStation.objects.filter(dataset=dataset)
    }
    first_row_by_id = {station_id: group[0].row_number for station_id, group in groups.items()}
    source_rows = []
    for row in audit.rows:
        station_id = row.normalized["OPIS Truckstop ID"]
        if row.issue:
            status, issue, station = "rejected", row.issue, None
        elif station_id in conflicting_ids:
            status, issue, station = "conflict", "conflicting_station_id", None
        else:
            status = "imported" if row.row_number == first_row_by_id[station_id] else "collapsed"
            issue, station = "", station_by_id[station_id]
        source_rows.append(FuelPriceSourceRow(
            dataset=dataset,
            row_number=row.row_number,
            physical_line_number=row.physical_line_number,
            raw_values=row.raw,
            normalized_values=row.normalized,
            status=status,
            issue=issue,
            station=station,
        ))
    FuelPriceSourceRow.objects.bulk_create(source_rows, batch_size=1000)
    return dataset, True
