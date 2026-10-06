from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class FuelPurchase:
    station_id: str
    gallons_purchased: Decimal
    cost_usd: Decimal


@dataclass(frozen=True)
class FuelPlan:
    purchases: tuple[FuelPurchase, ...]
    fuel_consumed_gallons: Decimal
    fuel_purchased_gallons: Decimal
    fuel_remaining_gallons: Decimal
    total_fuel_cost_usd: Decimal
