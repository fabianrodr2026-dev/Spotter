from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

TANK_CAPACITY_GALLONS = Decimal("50")
MILES_PER_GALLON = Decimal("10")
MAX_RANGE_MILES = float(TANK_CAPACITY_GALLONS * MILES_PER_GALLON)
FUEL_TOLERANCE_GALLONS = Decimal("0.000000001")
DISTANCE_TOLERANCE_MILES = 1e-9
ZERO = Decimal("0")


@dataclass(frozen=True)
class FuelStop:
    station_id: str
    price_usd_per_gallon: Decimal


@dataclass(frozen=True)
class FuelPurchase:
    station_id: str
    gallons_purchased: Decimal
    price_usd_per_gallon: Decimal
    cost_usd: Decimal


@dataclass(frozen=True)
class TankSample:
    label: str
    fuel_gallons: Decimal


@dataclass(frozen=True)
class FuelPlan:
    purchases: tuple[FuelPurchase, ...]
    fuel_consumed_gallons: Decimal
    fuel_purchased_gallons: Decimal
    fuel_remaining_gallons: Decimal
    total_fuel_cost_usd: Decimal
    initial_fuel_gallons: Decimal
    is_estimate: bool
    tank_samples: tuple[TankSample, ...] = ()


class FuelOptimizationError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _as_fuel(value: Decimal | float | int | str) -> Decimal:
    if isinstance(value, bool):
        raise FuelOptimizationError("invalid_fuel")
    if isinstance(value, Decimal):
        fuel = value
    elif isinstance(value, int):
        fuel = Decimal(value)
    elif isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise FuelOptimizationError("invalid_fuel")
        fuel = Decimal(str(value))
    elif isinstance(value, str):
        fuel = Decimal(value)
    else:
        raise FuelOptimizationError("invalid_fuel")
    if fuel < -FUEL_TOLERANCE_GALLONS:
        raise FuelOptimizationError("invalid_fuel")
    if fuel < ZERO:
        return ZERO
    return fuel


def _as_miles(value: float | int | Decimal) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise FuelOptimizationError("invalid_distance")
    miles = float(value)
    if miles != miles or miles in (float("inf"), float("-inf")) or miles < -DISTANCE_TOLERANCE_MILES:
        raise FuelOptimizationError("invalid_distance")
    if miles < 0:
        return 0.0
    return miles


def _gallons_for_miles(miles: float) -> Decimal:
    return Decimal(str(_as_miles(miles))) / MILES_PER_GALLON


def _clamp_fuel(fuel: Decimal) -> Decimal:
    if fuel < -FUEL_TOLERANCE_GALLONS:
        raise FuelOptimizationError("infeasible_fuel")
    if fuel < ZERO:
        return ZERO
    if fuel > TANK_CAPACITY_GALLONS + FUEL_TOLERANCE_GALLONS:
        raise FuelOptimizationError("infeasible_fuel")
    if fuel > TANK_CAPACITY_GALLONS:
        return TANK_CAPACITY_GALLONS
    return fuel


def _is_strictly_cheaper(candidate: FuelStop, current: FuelStop) -> bool:
    if candidate.price_usd_per_gallon < current.price_usd_per_gallon:
        return True
    if candidate.price_usd_per_gallon > current.price_usd_per_gallon:
        return False
    return candidate.station_id < current.station_id


def optimize_fuel_purchases(
    stops: Sequence[FuelStop],
    leg_distances_miles: Sequence[float],
    initial_fuel_gallons: Decimal | float | int | str,
    *,
    is_estimate: bool = True,
) -> FuelPlan:
    stops = tuple(stops)
    legs = tuple(_as_miles(leg) for leg in leg_distances_miles)
    if len(legs) != len(stops) + 1:
        raise FuelOptimizationError("invalid_legs")
    for stop in stops:
        if not isinstance(stop.station_id, str) or not stop.station_id:
            raise FuelOptimizationError("invalid_stop")
        if stop.price_usd_per_gallon <= ZERO:
            raise FuelOptimizationError("invalid_price")

    initial = _as_fuel(initial_fuel_gallons)
    if initial > TANK_CAPACITY_GALLONS + FUEL_TOLERANCE_GALLONS:
        raise FuelOptimizationError("invalid_fuel")
    initial = min(initial, TANK_CAPACITY_GALLONS)

    positions = [0.0]
    for leg in legs:
        positions.append(positions[-1] + leg)
    destination_miles = positions[-1]

    fuel = initial
    purchased_total = ZERO
    consumed_total = ZERO
    cost_total = ZERO
    purchases: list[FuelPurchase] = []
    samples: list[TankSample] = [TankSample("departure", fuel)]

    for index, stop in enumerate(stops):
        travel = _gallons_for_miles(legs[index])
        fuel = _clamp_fuel(fuel - travel)
        consumed_total += travel
        samples.append(TankSample(f"arrive:{stop.station_id}", fuel))

        current_pos = positions[index + 1]
        full_reach = current_pos + MAX_RANGE_MILES

        target_index: int | None = None
        for ahead in range(index + 1, len(stops)):
            ahead_pos = positions[ahead + 1]
            if ahead_pos > full_reach + DISTANCE_TOLERANCE_MILES:
                break
            if _is_strictly_cheaper(stops[ahead], stop):
                target_index = ahead
                break

        if target_index is not None:
            need = _gallons_for_miles(positions[target_index + 1] - current_pos)
        else:
            need = min(
                TANK_CAPACITY_GALLONS,
                _gallons_for_miles(destination_miles - current_pos),
            )

        buy = need - fuel
        if buy <= FUEL_TOLERANCE_GALLONS:
            buy = ZERO
        capacity_room = TANK_CAPACITY_GALLONS - fuel
        if buy > capacity_room + FUEL_TOLERANCE_GALLONS:
            raise FuelOptimizationError("infeasible_fuel")
        buy = min(buy, capacity_room)
        if buy > ZERO:
            cost = buy * stop.price_usd_per_gallon
            purchases.append(FuelPurchase(
                station_id=stop.station_id,
                gallons_purchased=buy,
                price_usd_per_gallon=stop.price_usd_per_gallon,
                cost_usd=cost,
            ))
            fuel = _clamp_fuel(fuel + buy)
            purchased_total += buy
            cost_total += cost
        samples.append(TankSample(f"depart:{stop.station_id}", fuel))

        next_miles = legs[index + 1]
        if fuel * MILES_PER_GALLON + Decimal(str(DISTANCE_TOLERANCE_MILES)) < Decimal(str(next_miles)):
            raise FuelOptimizationError("infeasible_fuel")

    final_leg = _gallons_for_miles(legs[-1])
    if fuel + FUEL_TOLERANCE_GALLONS < final_leg:
        raise FuelOptimizationError("infeasible_fuel")
    fuel = _clamp_fuel(fuel - final_leg)
    consumed_total += final_leg
    samples.append(TankSample("arrival", fuel))

    remaining = fuel
    balance = initial + purchased_total - consumed_total
    if abs(balance - remaining) > FUEL_TOLERANCE_GALLONS * Decimal("10"):
        raise FuelOptimizationError("infeasible_fuel")

    return FuelPlan(
        purchases=tuple(purchases),
        fuel_consumed_gallons=consumed_total,
        fuel_purchased_gallons=purchased_total,
        fuel_remaining_gallons=remaining,
        total_fuel_cost_usd=cost_total,
        initial_fuel_gallons=initial,
        is_estimate=is_estimate,
        tank_samples=tuple(samples),
    )


def legs_from_route_distances(
    stop_distances_miles: Sequence[float],
    total_distance_miles: float,
) -> tuple[float, ...]:
    stops = [_as_miles(distance) for distance in stop_distances_miles]
    total = _as_miles(total_distance_miles)
    previous = 0.0
    legs: list[float] = []
    for distance in stops:
        if distance + DISTANCE_TOLERANCE_MILES < previous:
            raise FuelOptimizationError("invalid_distance")
        legs.append(max(0.0, distance - previous))
        previous = max(previous, distance)
    if total + DISTANCE_TOLERANCE_MILES < previous:
        raise FuelOptimizationError("invalid_distance")
    legs.append(max(0.0, total - previous))
    return tuple(legs)
