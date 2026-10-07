from decimal import Decimal, ROUND_CEILING
from unittest import TestCase

from routes.services.optimizer import (
    FUEL_TOLERANCE_GALLONS,
    FuelOptimizationError,
    FuelStop,
    TANK_CAPACITY_GALLONS,
    legs_from_route_distances,
    optimize_fuel_purchases,
)


def stop(station_id: str, price: str) -> FuelStop:
    return FuelStop(station_id=station_id, price_usd_per_gallon=Decimal(price))


def assert_conservation(test: TestCase, plan) -> None:
    balance = plan.initial_fuel_gallons + plan.fuel_purchased_gallons - plan.fuel_consumed_gallons
    test.assertLessEqual(abs(balance - plan.fuel_remaining_gallons), FUEL_TOLERANCE_GALLONS * Decimal("10"))
    purchase_sum = sum((item.gallons_purchased for item in plan.purchases), Decimal("0"))
    cost_sum = sum((item.cost_usd for item in plan.purchases), Decimal("0"))
    test.assertEqual(purchase_sum, plan.fuel_purchased_gallons)
    test.assertEqual(cost_sum, plan.total_fuel_cost_usd)
    for sample in plan.tank_samples:
        test.assertGreaterEqual(sample.fuel_gallons, Decimal("0") - FUEL_TOLERANCE_GALLONS)
        test.assertLessEqual(sample.fuel_gallons, TANK_CAPACITY_GALLONS + FUEL_TOLERANCE_GALLONS)


def discrete_min_cost(
    prices: list[Decimal],
    legs_miles: list[float],
    initial_gallons: Decimal,
    capacity: Decimal = TANK_CAPACITY_GALLONS,
    step: Decimal = Decimal("1"),
) -> Decimal | None:
    """Independent DP over discrete gallon units for fixed legs; not greedy logic."""
    if len(legs_miles) != len(prices) + 1:
        raise ValueError("legs must be one longer than prices")
    max_units = int(capacity / step)
    initial_units = int(initial_gallons / step)
    travel_units = [
        int((Decimal(str(leg)) / Decimal("10") / step).to_integral_value(rounding=ROUND_CEILING))
        for leg in legs_miles
    ]
    at_node: list[dict[int, Decimal]] = [{} for _ in range(len(prices) + 2)]
    at_node[0][initial_units] = Decimal("0")

    for index, travel in enumerate(travel_units):
        for fuel_units, cost in at_node[index].items():
            if index == 0:
                purchase_options = (0,)
                price = Decimal("0")
            else:
                price = prices[index - 1]
                purchase_options = range(0, max_units - fuel_units + 1)
            for buy in purchase_options:
                fuel_after = fuel_units + buy
                if fuel_after > max_units:
                    continue
                next_fuel = fuel_after - travel
                if next_fuel < 0:
                    continue
                next_cost = cost + (price * step * buy)
                previous = at_node[index + 1].get(next_fuel)
                if previous is None or next_cost < previous:
                    at_node[index + 1][next_fuel] = next_cost

    if not at_node[-1]:
        return None
    return min(at_node[-1].values())


class OptimizerTests(TestCase):
    def test_trip_shorter_than_range_needs_no_purchase(self):
        plan = optimize_fuel_purchases((), (100.0,), 50, is_estimate=True)
        self.assertTrue(plan.is_estimate)
        self.assertEqual(plan.purchases, ())
        self.assertEqual(plan.fuel_consumed_gallons, Decimal("10"))
        self.assertEqual(plan.fuel_purchased_gallons, Decimal("0"))
        self.assertEqual(plan.fuel_remaining_gallons, Decimal("40"))
        self.assertEqual(plan.total_fuel_cost_usd, Decimal("0"))
        assert_conservation(self, plan)

    def test_trip_exactly_at_range(self):
        plan = optimize_fuel_purchases((), (500.0,), 50)
        self.assertEqual(plan.fuel_consumed_gallons, Decimal("50"))
        self.assertEqual(plan.fuel_remaining_gallons, Decimal("0"))
        self.assertEqual(plan.purchases, ())
        assert_conservation(self, plan)

    def test_trip_just_beyond_range_without_station_is_infeasible(self):
        with self.assertRaisesRegex(FuelOptimizationError, "infeasible_fuel"):
            optimize_fuel_purchases((), (500.1,), 50)

    def test_contract_over_one_tank_with_station(self):
        plan = optimize_fuel_purchases(
            (stop("A", "4.00"),),
            (450.0, 150.0),
            50,
            is_estimate=False,
        )
        self.assertFalse(plan.is_estimate)
        self.assertEqual(len(plan.purchases), 1)
        self.assertEqual(plan.purchases[0].gallons_purchased, Decimal("10"))
        self.assertEqual(plan.purchases[0].cost_usd, Decimal("40.00"))
        self.assertEqual(plan.fuel_consumed_gallons, Decimal("60"))
        self.assertEqual(plan.fuel_purchased_gallons, Decimal("10"))
        self.assertEqual(plan.fuel_remaining_gallons, Decimal("0"))
        assert_conservation(self, plan)

    def test_cheaper_station_ahead_buys_only_enough(self):
        plan = optimize_fuel_purchases(
            (stop("A", "5.00"), stop("B", "3.00")),
            (100.0, 100.0, 100.0),
            10,
        )
        self.assertEqual(plan.purchases[0].station_id, "A")
        self.assertEqual(plan.purchases[0].gallons_purchased, Decimal("10"))
        self.assertEqual(plan.purchases[1].station_id, "B")
        self.assertEqual(plan.purchases[1].gallons_purchased, Decimal("10"))
        self.assertEqual(plan.total_fuel_cost_usd, Decimal("80.00"))
        assert_conservation(self, plan)

    def test_increasing_prices_fill_when_needed(self):
        plan = optimize_fuel_purchases(
            (stop("A", "3.00"), stop("B", "4.00"), stop("C", "5.00")),
            (100.0, 100.0, 100.0, 100.0),
            15,
        )
        self.assertEqual(plan.purchases[0].station_id, "A")
        self.assertGreater(plan.purchases[0].gallons_purchased, Decimal("0"))
        self.assertEqual(plan.fuel_remaining_gallons, Decimal("0"))
        assert_conservation(self, plan)

    def test_equal_prices_tie_break_by_station_id(self):
        plan_ab = optimize_fuel_purchases(
            (stop("b", "4.00"), stop("a", "4.00")),
            (0.0, 50.0, 50.0),
            0,
        )
        plan_ba = optimize_fuel_purchases(
            (stop("a", "4.00"), stop("b", "4.00")),
            (0.0, 50.0, 50.0),
            0,
        )
        self.assertNotEqual(
            tuple((p.station_id, p.gallons_purchased) for p in plan_ab.purchases),
            tuple((p.station_id, p.gallons_purchased) for p in plan_ba.purchases),
        )
        assert_conservation(self, plan_ab)
        assert_conservation(self, plan_ba)

    def test_multiple_stops_and_sparse_coverage(self):
        plan = optimize_fuel_purchases(
            (stop("A", "4.50"), stop("B", "4.10"), stop("C", "3.90")),
            (400.0, 400.0, 400.0, 50.0),
            50,
        )
        self.assertGreaterEqual(len(plan.purchases), 2)
        assert_conservation(self, plan)
        with self.assertRaisesRegex(FuelOptimizationError, "infeasible_fuel"):
            optimize_fuel_purchases(
                (stop("A", "4.00"),),
                (100.0, 600.0),
                5,
            )

    def test_initial_fuel_zero_partial_and_full(self):
        with self.assertRaisesRegex(FuelOptimizationError, "infeasible_fuel"):
            optimize_fuel_purchases((stop("A", "4.00"),), (10.0, 10.0), 0)
        at_origin = optimize_fuel_purchases((stop("A", "4.00"),), (0.0, 100.0), 0)
        self.assertEqual(at_origin.purchases[0].gallons_purchased, Decimal("10"))
        partial = optimize_fuel_purchases((stop("A", "4.00"),), (40.0, 60.0), 5)
        self.assertEqual(partial.purchases[0].gallons_purchased, Decimal("5"))
        full = optimize_fuel_purchases((stop("A", "4.00"),), (40.0, 60.0), 50)
        self.assertEqual(full.fuel_purchased_gallons, Decimal("0"))
        assert_conservation(self, at_origin)
        assert_conservation(self, partial)
        assert_conservation(self, full)

    def test_legs_from_route_distances(self):
        self.assertEqual(legs_from_route_distances((100.0, 250.0), 300.0), (100.0, 150.0, 50.0))

    def test_matches_independent_discrete_solver_on_small_cases(self):
        cases = [
            ([Decimal("4.00")], [200.0, 100.0], Decimal("20")),
            ([Decimal("5.00"), Decimal("3.00")], [100.0, 100.0, 100.0], Decimal("10")),
            ([Decimal("3.00"), Decimal("4.00"), Decimal("5.00")], [100.0, 100.0, 100.0, 100.0], Decimal("15")),
            ([Decimal("4.00"), Decimal("4.00")], [0.0, 100.0, 50.0], Decimal("0")),
        ]
        for prices, legs, initial in cases:
            stops = tuple(stop(chr(65 + i), str(price)) for i, price in enumerate(prices))
            plan = optimize_fuel_purchases(stops, legs, initial, is_estimate=False)
            assert_conservation(self, plan)
            discrete = discrete_min_cost(prices, legs, initial, step=Decimal("1"))
            self.assertIsNotNone(discrete, msg=f"legs={legs}")
            self.assertEqual(plan.total_fuel_cost_usd, discrete)
