from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from .routing import DrivingRoute


@dataclass(frozen=True)
class CandidateStation:
    source_id: str
    latitude: float
    longitude: float
    price_usd_per_gallon: Decimal


class StationLookup(Protocol):
    def candidates(self, route: DrivingRoute, corridor_miles: float) -> tuple[CandidateStation, ...]: ...
