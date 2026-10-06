from dataclasses import dataclass

from routes.validation import PlanInput

from .optimizer import FuelPlan
from .routing import DrivingRoute, RoutingProvider
from .station_search import StationLookup


@dataclass(frozen=True)
class PlannedRoute:
    route: DrivingRoute
    fuel: FuelPlan


class RoutePlanner:
    def __init__(self, routing_provider: RoutingProvider, station_lookup: StationLookup) -> None:
        self.routing_provider = routing_provider
        self.station_lookup = station_lookup

    def plan(self, request: PlanInput) -> PlannedRoute:
        raise NotImplementedError("Route planning is implemented in later steps")
