from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from django.db.models import QuerySet
from .routing import DrivingRoute


@dataclass(frozen=True)
class CandidateStation:
    source_id: str
    latitude: float
    longitude: float
    price_usd_per_gallon: Decimal


class StationLookup(Protocol):
    def candidates(self, route: DrivingRoute, corridor_miles: float) -> tuple[CandidateStation, ...]: ...


def usable_stations(stations: QuerySet | None = None) -> QuerySet:
    """Mandatory quality gate for future spatial candidate lookups."""
    from routes.models import FuelStation
    from .state_boundaries import CONTIGUOUS_STATES

    if stations is None:
        stations = FuelStation.objects.all()
    return stations.filter(
        geocoding_status="resolved", location_verified_at__isnull=False,
        state__in=CONTIGUOUS_STATES,
        latitude__gte=-90, latitude__lte=90, longitude__gte=-180, longitude__lte=180,
    ).exclude(geocoding_key="")
