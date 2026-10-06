from dataclasses import dataclass
from typing import Protocol

from routes.validation import Coordinate


@dataclass(frozen=True)
class DrivingRoute:
    geometry: dict
    distance_miles: float
    duration_seconds: float
    leg_distances_miles: tuple[float, ...]


class RoutingProvider(Protocol):
    def route(self, waypoints: tuple[Coordinate, ...]) -> DrivingRoute: ...
