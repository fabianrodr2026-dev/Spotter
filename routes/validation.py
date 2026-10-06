import json
import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Coordinate:
    latitude: float
    longitude: float


@dataclass(frozen=True)
class PlanInput:
    start: Coordinate
    finish: Coordinate
    initial_fuel_gallons: float


class InvalidPlanInput(ValueError):
    pass


def _number(value: Any, name: str, lower: float, upper: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise InvalidPlanInput(f"{name} must be a finite number")
    if not lower <= value <= upper:
        raise InvalidPlanInput(f"{name} must be between {lower} and {upper}")
    return float(value)


def _coordinate(value: Any, name: str) -> Coordinate:
    if not isinstance(value, dict):
        raise InvalidPlanInput(f"{name} must be a coordinate object")
    if "latitude" not in value or "longitude" not in value:
        raise InvalidPlanInput(f"{name} needs latitude and longitude")
    return Coordinate(
        latitude=_number(value["latitude"], f"{name}.latitude", -90, 90),
        longitude=_number(value["longitude"], f"{name}.longitude", -180, 180),
    )


def parse_plan_input(body: bytes) -> PlanInput:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidPlanInput("Request body must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise InvalidPlanInput("Request body must be a JSON object")
    if "start" not in payload or "finish" not in payload:
        raise InvalidPlanInput("start and finish are required")
    return PlanInput(
        start=_coordinate(payload["start"], "start"),
        finish=_coordinate(payload["finish"], "finish"),
        initial_fuel_gallons=_number(payload.get("initial_fuel_gallons", 50), "initial_fuel_gallons", 0, 50),
    )
